"""Match expert CVs against a set of cases and write a ranked HTML report.

python -m src.matching.run --dotenv --cv expert.pdf [--cv other.pdf] --cases eval/matching_cases.json

The cases file lists each case with its metadata and one document source: "entries" (a saved
CourtListener entries JSON with plain_text) plus "entry_number", or a local "pdf". A case
without a readable document is reported as insufficient information. Nothing is downloaded
and no CourtListener request is made. LLM answers are cached in data/matching/cache, so a
re-run only calls the model for inputs or prompts that changed.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.core.config import ConfigError
from src.ingest.pdf import ExtractionError, extract_pdf
from src.llm.client import LLMError, create_client
from src.llm.config import PROFILES, LLMSettings, profile_env
from src.llm.document import MARKER, pages_from_extraction, pages_from_plain_text
from src.llm.output import InvalidOutput
from src.matching import match as matching
from src.matching import profiles
from src.matching.report import render

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "src" / "llm" / "prompts"
STORAGE = "https://storage.courtlistener.com/"
MIN_CASE_CHARS = 2000


class Model:
    """JSON calls with a file cache, one retry on a transient error and one on invalid output."""

    def __init__(self, settings, cache_dir):
        self.settings, self.client = settings, create_client(settings)
        self.cache_dir = cache_dir
        self.stats = {"calls": 0, "cached": 0, "input_tokens": 0, "output_tokens": 0}
        cache_dir.mkdir(parents=True, exist_ok=True)

    def ask(self, prompt_name, user, parse, max_tokens):
        prompt = (PROMPTS / f"{prompt_name}.md").read_text(encoding="utf-8")
        key = hashlib.sha256(json.dumps([self.settings.provider, self.settings.model, self.settings.extra_body,
                                         prompt_name, prompt, user]).encode()).hexdigest()
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            self.stats["cached"] += 1
            return parse(json.loads(path.read_text(encoding="utf-8"))["text"])
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": user}]
        for attempt in range(2):
            try:
                reply = self.client.complete(messages, json_mode=True, max_tokens=max_tokens)
            except LLMError as exc:
                if exc.transient and not attempt:
                    continue
                raise
            self.stats["calls"] += 1
            self.stats["input_tokens"] += reply.input_tokens
            self.stats["output_tokens"] += reply.output_tokens
            try:
                result = parse(reply.text)
            except InvalidOutput:
                if attempt:
                    raise
                continue
            path.write_text(json.dumps({"prompt": prompt_name, "text": reply.text}), encoding="utf-8")
            return result


def marked(pages, limit):
    """Page-marked text, whole pages up to limit. Returns (text, pages included)."""
    result, used = "", 0
    for number, page in enumerate(pages, 1):
        block = f"[Page {number}]\n{MARKER.sub('[page]', page)}\n"
        if len(result) + len(block) > limit and used:
            break
        result, used = result + block[:max(0, limit - len(result))], number
    if used < len(pages):
        result += f"[Text truncated after page {used} of {len(pages)}]\n"
    return result, used


def expert_profile(model, path):
    pages = profiles.clean(pages_from_extraction(extract_pdf(path, layout=False)))
    body, _ = marked(pages, 60000)
    parsed = model.ask("expert_cv_v1", f"<cv>\n{body}</cv>", profiles.parse_expert, 6000)
    profile, dropped = profiles.verify_expert(parsed, pages)
    return {**profile, "file": Path(path).name, "pages": len(pages), "dropped": dropped}


def case_document(case, base):
    """(pages, document URL) for a case, or (None, None) when it has no readable document."""
    if case.get("entries"):
        rows = json.loads((base / case["entries"]).read_text(encoding="utf-8"))
        entry = next((r for r in rows if r.get("entry_number") == case["entry_number"]), None)
        docs = [d for d in (entry or {}).get("recap_documents") or [] if (d.get("plain_text") or "").strip()]
        if docs:
            doc = next((d for d in docs if d.get("attachment_number") is None), docs[0])
            url = STORAGE + doc["filepath_local"] if doc.get("filepath_local") else None
            return profiles.clean(pages_from_plain_text(doc["plain_text"])), url
    elif case.get("pdf"):
        return profiles.clean(pages_from_extraction(extract_pdf(base / case["pdf"], layout=False))), case.get("document_url")
    return None, None


def case_profile(model, case, pages, limit):
    header = "\n".join(f"{k}: {case[k]}" for k in ("name", "court", "docket_number", "filed", "document_label") if case.get(k))
    body, used = marked(pages, limit)
    parsed = model.ask("case_profile_v1", f"{header}\n\n<document>\n{body}</document>", profiles.parse_case, 6000)
    profile, dropped = profiles.verify_case(parsed, pages)
    return {**profile, "pages": len(pages), "pages_read": used, "dropped": dropped}


def match_input(expert, case, profile):
    expert_view = {"name": expert["name"], "headline": expert["headline"], "domains": expert["domains"],
                   "items": [{k: i[k] for k in ("id", "kind", "summary")} for i in expert["items"]]}
    case_view = {"name": case["name"], "court": case.get("court"), **{k: profile[k] for k in (
                     "plaintiffs", "defendants", "case_type", "domains", "summary", "technology")},
                 "issues": [{k: i[k] for k in ("id", "issue", "technical", "quote")} for i in profile["issues"] if i["technical"]],
                 "expertise": [{k: x[k] for k in ("id", "area", "basis", "why")} for x in profile["expertise"]],
                 "counsel": [{k: c.get(k) for k in ("name", "firm", "party")} for c in profile["counsel"]]}
    return (f"<expert>\n{json.dumps(expert_view, ensure_ascii=False, indent=1)}\n</expert>\n"
            f"<case>\n{json.dumps(case_view, ensure_ascii=False, indent=1)}\n</case>")


def case_status(case, base):
    """Whether a case has a document to read, without extracting it (for listings)."""
    if case.get("entries"):
        path = base / case["entries"]
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
            entry = next((r for r in rows if r.get("entry_number") == case.get("entry_number")), None)
            if any((d.get("plain_text") or "").strip() for d in (entry or {}).get("recap_documents") or []):
                return "readable"
    elif case.get("pdf") and (base / case["pdf"]).exists():
        return "readable"
    return "no document"


def step(log, stage, label, model, before, detail):
    log({"stage": stage, "label": label, "detail": detail, "cached": model.stats["cached"] > before})


def run(model, cv_paths, cases, base, limit, log=print):
    """log receives one dict per finished step: stage ("case", "expert", "match"), label, detail, cached."""
    loaded = []
    for case in cases:
        case = {"document_label": "Complaint", "document_short": "Complaint", **case}
        pages, url = case_document(case, base)
        case["document_url"] = case.get("document_url") or url
        before = model.stats["cached"]
        if not pages or sum(len(p) for p in pages) < MIN_CASE_CHARS:
            case["note"] = case.get("note") or "No readable case document on CourtListener."
            loaded.append((case, None))
            step(log, "case", case["name"], model, before, "no readable document")
            continue
        profile = case_profile(model, case, pages, limit)
        step(log, "case", case["name"], model, before,
             f"{len(profile['issues'])} issues, {len(profile['expertise'])} expertise areas, "
             f"{profile['pages_read']}/{profile['pages']} pages read, {len(profile['dropped'])} unverified claims dropped")
        loaded.append((case, profile))
    experts = []
    for path in cv_paths:
        before = model.stats["cached"]
        expert = expert_profile(model, path)
        step(log, "expert", expert["name"], model, before,
             f"{len(expert['items'])} experience items verified, {len(expert['dropped'])} dropped")
        results = []
        for case, profile in loaded:
            before = model.stats["cached"]
            if profile is None:
                result = matching.insufficient("No readable case document, so the fit cannot be judged.")
            elif not profile["issues"]:
                result = matching.insufficient("No issue in the case document could be verified.")
            else:
                parsed = model.ask("case_match_v1", match_input(expert, case, profile), matching.parse_match, 4000)
                result = matching.enforce(parsed, expert, profile)
            results.append({"case": case, "profile": profile, "match": result})
            step(log, "match", case["name"], model, before,
                 f"{matching.LABELS[result['tier']]}, {len(result['matches'])} verified reasons")
        experts.append({"profile": expert, "results": matching.rank(results)})
    return experts


def print_step(item):
    print(f"{item['stage']:6} {item['label']}: {item['detail']}{' (cached)' if item['cached'] else ''}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cv", type=Path, action="append", required=True, help="Expert CV PDF (repeatable)")
    parser.add_argument("--cases", type=Path, required=True, help="JSON list of cases")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "matching")
    parser.add_argument("--max-case-chars", type=int, default=160000, help="Case document text sent to the model")
    parser.add_argument("--profile", choices=sorted(PROFILES))
    parser.add_argument("--model")
    parser.add_argument("--extra-body")
    parser.add_argument("--dotenv", action="store_true", help="Load LLM_* settings from the local .env")
    args = parser.parse_args(argv)
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    try:
        settings = LLMSettings.from_env(profile_env(os.environ, args.profile, args.model, args.extra_body))
        model = Model(settings, args.out / "cache")
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        experts = run(model, args.cv, cases, ROOT, args.max_case_chars, print_step)
    except (ConfigError, LLMError, InvalidOutput, ExtractionError, OSError, ValueError) as exc:
        print(f"Matching failed: {type(exc).__name__}: {exc}")
        return 1
    stamp = datetime.now(timezone.utc)
    stem = args.out / f"report_{stamp:%Y%m%dT%H%M%S}"
    stem.with_suffix(".json").write_text(json.dumps(experts, ensure_ascii=False, indent=2), encoding="utf-8")
    stem.with_suffix(".html").write_text(render(experts, f"{stamp:%Y-%m-%d %H:%M} UTC", settings.model), encoding="utf-8")
    s = model.stats
    print(f"model {settings.model} · calls {s['calls']} · cached {s['cached']} · tokens in {s['input_tokens']} out {s['output_tokens']}")
    print(f"Saved {stem}.html and .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
