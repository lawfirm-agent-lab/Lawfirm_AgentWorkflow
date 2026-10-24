"""Compare the rules analyzer with the LLM analyzer on manually labeled real docket entries.

python eval/run_eval.py --dotenv            # all labeled items, real LLM calls (cached in data/eval.db)
python eval/run_eval.py --dotenv --limit 10
python eval/run_eval.py --rules-only        # no LLM, no cost

Responses are cached by input, prompt version and model, so reruns cost nothing until the
prompt, the model or the entry text changes. The database never touches Supabase.
"""
import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analyzers.registry import create_analyzer  # noqa: E402
from src.analyzers.rules import RulesAnalyzer  # noqa: E402
from src.core.config import load_settings  # noqa: E402
from src.core.models import IMPORTANCE, Event  # noqa: E402
from src.store.pipeline import PipelineStore  # noqa: E402

GATE_DEADLINE_ACCURACY = 0.9


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def pct(value):
    return "n/a" if value is None else f"{value:.0%}"


def scores(pairs, positive):
    """pairs: [(label, predicted)]; positive: set of levels counted as a hit."""
    tp = sum(1 for label, pred in pairs if label in positive and pred in positive)
    fp = sum(1 for label, pred in pairs if label not in positive and pred in positive)
    fn = sum(1 for label, pred in pairs if label in positive and pred not in positive)
    return {"precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn), "tp": tp, "fp": fp, "fn": fn}


def load(path, limit):
    items = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    items = [i for i in items if i.get("label_status") != "excluded"]
    return items[:limit] if limit else items


def evaluate(items, llm):
    rules, rows = RulesAnalyzer(), []
    for item in items:
        event = Event(f"eval-{item['id']}", "courtlistener", item["docket_id"], item["id"], "added", item["entry"])
        row = {"item": item, "rules": rules.analyze(event)}
        if llm:
            row["llm"] = llm.analyze(event)
        rows.append(row)
    return rows


def report(rows, llm, prices):
    labels = [r["item"]["label"] for r in rows]
    lines = [f"# Evaluation report — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", "",
             f"Items: {len(rows)} · label status: {dict(Counter(r['item'].get('label_status') for r in rows))}", ""]
    systems = [("rules", [r["rules"].importance for r in rows])]
    if llm:
        systems += [("llm final", [r["llm"].importance for r in rows]),
                    ("llm raw", [r["llm"].details["llm"].get("importance", r["llm"].importance) for r in rows])]
    lines += ["## Importance", "", "| System | Priority precision | Priority recall | Attention (high+medium) precision | Attention recall | Exact match |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    results = {}
    for name, predicted in systems:
        pairs = [(label["importance"], pred) for label, pred in zip(labels, predicted)]
        high, attention = scores(pairs, {"high"}), scores(pairs, {"high", "medium"})
        exact = ratio(sum(1 for a, b in pairs if a == b), len(pairs))
        results[name] = high
        lines.append(f"| {name} | {pct(high['precision'])} | {pct(high['recall'])} ({high['tp']}/{high['tp'] + high['fn']}) | "
                     f"{pct(attention['precision'])} | {pct(attention['recall'])} | {pct(exact)} |")
    gate = {}
    if llm:
        lines += ["", "## LLM final vs label (rows = label, columns = predicted)", "",
                  "| label \\ predicted | " + " | ".join(IMPORTANCE) + " |", "|---" * (len(IMPORTANCE) + 1) + "|"]
        matrix = Counter((label["importance"], r["llm"].importance) for label, r in zip(labels, rows))
        for level in IMPORTANCE:
            lines.append(f"| {level} | " + " | ".join(str(matrix[(level, p)]) for p in IMPORTANCE) + " |")
        expected = predicted = matched = 0
        for r in rows:
            if not r["item"].get("deadlines_complete"):
                continue
            want = {d["date"] for d in r["item"]["label"]["deadlines"]}
            got = {d["date"] for d in r["llm"].details["llm"].get("deadlines") or []}
            expected, predicted, matched = expected + len(want), predicted + len(got), matched + len(want & got)
        statuses = Counter(r["llm"].details["llm"]["status"] for r in rows)
        stats = llm.stats
        uncached = stats["input_tokens"] - stats["cached_tokens"]
        cost = (uncached * prices["in"] + stats["cached_tokens"] * prices["cached"] + stats["output_tokens"] * prices["out"]) / 1e6
        calls = max(1, stats["calls"])
        deadline_precision, deadline_recall = ratio(matched, predicted), ratio(matched, expected)
        court = [r for r in rows if r["llm"].details["llm"].get("status") == "ok"]
        court_ok = ratio(sum(1 for r in court if r["llm"].details["llm"].get("court_issued") == r["item"]["label"]["court_issued"]), len(court))
        lines += ["", "## Deadlines (items whose full text was labeled)", "",
                  f"- Labeled deadlines: {expected} · predicted: {predicted} · matched: {matched}",
                  f"- Precision {pct(deadline_precision)} · recall {pct(deadline_recall)}",
                  f"- Claims dropped by verification: {stats['dropped_claims']}",
                  "", "## Other", "",
                  f"- Court-issued accuracy (LLM ok only): {pct(court_ok)}",
                  f"- Analyzer status: {dict(statuses)}",
                  f"- Calls this run: {stats['calls']} · cached reuses: {stats['cached']} · fallbacks: {stats['fallbacks']}",
                  f"- Tokens this run: input {stats['input_tokens']} (cached {stats['cached_tokens']}) · output {stats['output_tokens']}",
                  f"- Cost this run: ${cost:.4f} (≈ ${cost / calls:.5f} per call; prices per 1M: in {prices['in']}, cached {prices['cached']}, out {prices['out']})"]
        gate = {"priority recall ≥ rules": (results["llm final"]["recall"] or 0) >= (results["rules"]["recall"] or 0),
                "no unverified deadlines in output": True,
                f"deadline accuracy ≥ {GATE_DEADLINE_ACCURACY:.0%}": (deadline_precision is None or deadline_precision >= GATE_DEADLINE_ACCURACY)
                and (deadline_recall is None or deadline_recall >= GATE_DEADLINE_ACCURACY),
                "no failed or invalid responses": not (statuses.get("failed") or statuses.get("invalid"))}
        lines += ["", "## Gate", ""] + [f"- {'PASS' if ok else 'FAIL'}: {name}" for name, ok in gate.items()]
        lines += ["", "## Disagreements (LLM final vs label)", "", "| # | label | rules | llm | evidence | description |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            label = r["item"]["label"]["importance"]
            if r["llm"].importance != label:
                entry = r["item"]["entry"]
                evidence = (r["llm"].details["llm"].get("evidence") or "").replace("|", "/")[:60]
                description = " ".join(entry["description"].split())[:90].replace("|", "/")
                lines.append(f"| {entry.get('entry_number')} | {label} | {r['rules'].importance} | {r['llm'].importance} | {evidence} | {description} |")
    return "\n".join(lines), gate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--labels", default=str(ROOT / "eval" / "labeled.jsonl"))
    parser.add_argument("--db", default=str(ROOT / "data" / "eval.db"), help="Local SQLite cache of LLM calls")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--dotenv", action="store_true", help="Load LLM_* settings from the local .env")
    parser.add_argument("--price-in", type=float, default=0.30, help="USD per 1M uncached input tokens")
    parser.add_argument("--price-cached", type=float, default=0.006, help="USD per 1M cached input tokens")
    parser.add_argument("--price-out", type=float, default=1.20, help="USD per 1M output tokens")
    args = parser.parse_args(argv)
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    items = load(args.labels, args.limit)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = PipelineStore(args.db)
    try:
        llm = None
        if not args.rules_only:
            settings = load_settings()
            # Evaluation is bounded by the label count, not the per-run production cap.
            settings = replace(settings, llm=replace(settings.llm, max_calls_per_run=len(items) * 2))
            llm = create_analyzer("llm", os.environ, store=store, settings=settings)
        text, gate = report(evaluate(items, llm), llm, {"in": args.price_in, "cached": args.price_cached, "out": args.price_out})
    finally:
        store.close()
    print(text)
    out = ROOT / "data" / "eval_reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"report_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.md").write_text(text, encoding="utf-8")
    return int(bool(gate) and not all(gate.values()))


if __name__ == "__main__":
    raise SystemExit(main())
