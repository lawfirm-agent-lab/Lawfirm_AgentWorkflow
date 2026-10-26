"""Try the full-document briefing on one local document and preview the digest entry.

python eval/run_document.py --dotenv --pdf path/to/order.pdf --entry-number 39 --description "ORDER ..."
python eval/run_document.py --dotenv --entries data/73132799_entries.json --entry-number 39
python eval/run_document.py --dotenv --entries ... --entry-number 39 --extra-body '{"thinking": {"type": "enabled"}}'
python eval/run_document.py --dotenv --pdf ... --entry-number 39 --profile openrouter --model qwen/qwen3-235b-a22b

With --pdf the text comes from src.ingest.pdf (text layer, OCR for scanned pages); with
--entries it comes from the CourtListener plain_text already saved in that file. Nothing
is downloaded and no CourtListener request is made. LLM calls are cached in data/eval.db.
"""
import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analyzers.registry import create_document_analyzer  # noqa: E402
from src.analyzers.rules import RulesAnalyzer  # noqa: E402
from src.core.config import load_settings  # noqa: E402
from src.core.models import Event  # noqa: E402
from src.ingest.pdf import extract_pdf  # noqa: E402
from src.llm.config import PROFILES, profile_env  # noqa: E402
from src.llm.document import pages_from_extraction, pages_from_plain_text  # noqa: E402
from src.notify.render import render_digest  # noqa: E402
from src.store.pipeline import PipelineStore  # noqa: E402


def saved_entry(path, number):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("results") or []
    entry = next((row for row in rows if row.get("entry_number") == number), None)
    if entry is None:
        raise SystemExit(f"Entry {number} is not in {path}")
    return entry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pdf", type=Path, help="Local PDF to extract")
    source.add_argument("--entries", type=Path, help="Saved CourtListener entries JSON with plain_text")
    parser.add_argument("--entry-number", type=int, required=True)
    parser.add_argument("--description", default="", help="Docket entry description (with --pdf)")
    parser.add_argument("--docket-id", type=int, default=0)
    parser.add_argument("--case-name", default="")
    parser.add_argument("--extra-body", help="Override LLM_EXTRA_BODY for this run (JSON)")
    parser.add_argument("--profile", choices=sorted(PROFILES), help="Use another provider (its key from e.g. OPENROUTER_API_KEY)")
    parser.add_argument("--model", help="Model name as the provider spells it")
    parser.add_argument("--max-output-tokens", type=int, help="Override llm.max_document_output_tokens (thinking needs more)")
    parser.add_argument("--force-ocr", action="store_true")
    parser.add_argument("--db", default=str(ROOT / "data" / "eval.db"))
    parser.add_argument("--dotenv", action="store_true", help="Load LLM_* settings from the local .env")
    args = parser.parse_args(argv)
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    env = profile_env(os.environ, args.profile, args.model, args.extra_body)
    if args.pdf:
        pages = pages_from_extraction(extract_pdf(args.pdf, force_ocr=args.force_ocr, layout=False))
        payload = {"entry_number": args.entry_number, "description": args.description, "recap_documents": []}
    else:
        payload = saved_entry(args.entries, args.entry_number)
        text = next((d.get("plain_text") for d in payload.get("recap_documents") or [] if (d.get("plain_text") or "").strip()), "")
        if not text:
            raise SystemExit(f"Entry {args.entry_number} has no document text in {args.entries}")
        pages = pages_from_plain_text(text)
    event = Event(f"document-eval-{args.entry_number}", "courtlistener", args.docket_id or int(payload.get("docket_id") or 0),
                  int(payload.get("id") or args.entry_number), "added", payload)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = PipelineStore(args.db)
    try:
        settings = load_settings()
        if args.max_output_tokens:
            settings = replace(settings, llm=replace(settings.llm, max_document_output_tokens=args.max_output_tokens))
        analyzer = create_document_analyzer(env, store=store, settings=settings)
        result = analyzer.analyze(event, pages)
    finally:
        store.close()
    base = RulesAnalyzer().analyze(event)
    analysis = replace(base, importance="medium" if base.importance == "low" else base.importance,
                       details={**base.details, "document": result})
    infos = {event.docket_id: {"case_name": args.case_name}} if args.case_name else {}
    subject, body, rich = render_digest([(event, analysis)], infos)
    out = ROOT / "data" / "eval_reports"
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"document_{args.entry_number}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    stem.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    stem.with_suffix(".txt").write_text(subject + "\n\n" + body, encoding="utf-8")
    stem.with_suffix(".html").write_text(rich, encoding="utf-8")
    stats = analyzer.stats
    print(body)
    print(f"\nmodel {analyzer.llm.model} · status {result['status']} · pages read {result['pages_read']}/{result['pages']} · "
          f"tokens in {stats['input_tokens']} out {stats['output_tokens']} · cached {stats['cached']} · "
          f"dropped {stats['dropped_claims']} · pages corrected {stats['corrected_pages']}")
    for item in result.get("dropped") or []:
        print(f"  dropped {item['field']} (p. {item['page']}, {item['reason']}): {item['quote'][:100]}")
    print(f"Saved {stem}.json/.txt/.html")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
