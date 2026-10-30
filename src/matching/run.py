"""Match expert CVs against a set of cases and write a ranked HTML report.

python -m src.matching.run --dotenv --cv expert.pdf [--cv other.pdf] --cases eval/matching_cases.json

The cases file lists each case with its metadata and one document source: "entries" (a saved
CourtListener entries JSON with plain_text) plus "entry_number", or a local "pdf". A case
without a readable document is reported as insufficient information.

With --find-cases, each CV also gets new cases found on CourtListener (src.matching.discover):
3 search requests per CV, capped by a local ledger; complaint PDFs come from public storage.
Without it nothing is downloaded and no CourtListener request is made. LLM answers are cached
in data/matching/cache, so a re-run only calls the model for inputs or prompts that changed.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.core.config import ConfigError, Limits
from src.ingest.pdf import ExtractionError, extract_pdf
from src.llm.client import LLMError, create_client
from src.llm.config import PROFILES, LLMSettings, profile_env
from src.llm.document import MARKER, pages_from_extraction, pages_from_plain_text
from src.llm.output import InvalidOutput
from src.matching import discover, profiles
from src.matching import match as matching
from src.matching.report import render

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "src" / "llm" / "prompts"
STORAGE = "https://storage.courtlistener.com/"
MIN_CASE_CHARS = 2000
CASE_PAGES = 150
LOCAL_LIMITS = Limits(per_day=30, headroom_per_day=0, headroom_per_hour=0, max_requests_per_run=6)


class Model:
    """JSON calls with a file cache, one retry on a transient error and one on invalid output.

    trace keeps what the run read and every model call's exact input and raw output (cached
    ones too), so each step can be reviewed by a person (web app inspection pages)."""

    def __init__(self, settings, cache_dir):
        self.settings, self.client = settings, create_client(settings)
        self.cache_dir = cache_dir
        self.stats = {"calls": 0, "cached": 0, "input_tokens": 0, "output_tokens": 0}
        self.trace = []
        cache_dir.mkdir(parents=True, exist_ok=True)

    def record(self, step, label, **data):
        self.trace.append({"step": step, "label": label, **data})

    def ask(self, prompt_name, user, parse, max_tokens, label=""):
        prompt = (PROMPTS / f"{prompt_name}.md").read_text(encoding="utf-8")
        key = hashlib.sha256(json.dumps([self.settings.provider, self.settings.model, self.settings.extra_body,
                                         prompt_name, prompt, user]).encode()).hexdigest()
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            self.stats["cached"] += 1
            raw = json.loads(path.read_text(encoding="utf-8"))["text"]
            self.record(prompt_name, label, input=user, output=raw, cached=True)
            return parse(raw)
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
            self.record(prompt_name, label, input=user, output=reply.text, cached=False,
                        tokens=[reply.input_tokens, reply.output_tokens])
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


def expert_profile(model, path, name=None):
    extracted = extract_pdf(path, layout=False)
    pages = profiles.clean(pages_from_extraction(extracted))
    model.record("extract_cv", name or Path(path).name, pages=[
        {"page": p["page"], "method": p["method"], "warnings": p["warnings"], "text": text}
        for p, text in zip(extracted["pages"], pages)])
    body, _ = marked(pages, 60000)
    parsed = model.ask("expert_cv_v1", f"<cv>\n{body}</cv>", profiles.parse_expert, 6000, label="CV")
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
        # The model reads at most --max-case-chars (about 100-150 pages); later pages are usually exhibits.
        extracted = extract_pdf(base / case["pdf"], layout=False, first_pages=CASE_PAGES)
        case["total_pages"] = extracted["total_pages"]
        return profiles.clean(pages_from_extraction(extracted)), case.get("document_url")
    return None, None


def case_profile(model, case, pages, limit):
    header = "\n".join(f"{k}: {case[k]}" for k in ("name", "court", "docket_number", "filed", "document_label") if case.get(k))
    body, used = marked(pages, limit)
    parsed = model.ask("case_profile_v1", f"{header}\n\n<document>\n{body}</document>", profiles.parse_case, 6000,
                       label=case["name"])
    profile, dropped = profiles.verify_case(parsed, pages)
    return {**profile, "pages": case.get("total_pages") or len(pages), "pages_read": used, "dropped": dropped}


def match_input(expert, case, profile):
    case_view = {"name": case["name"], "court": case.get("court"), **{k: profile[k] for k in (
                     "plaintiffs", "defendants", "case_type", "domains", "summary", "technology")},
                 "issues": [{k: i[k] for k in ("id", "issue", "technical", "quote")} for i in profile["issues"] if i["technical"]],
                 "expertise": [{k: x[k] for k in ("id", "area", "basis", "why")} for x in profile["expertise"]],
                 "counsel": [{k: c.get(k) for k in ("name", "firm", "party")} for c in profile["counsel"]]}
    return (f"<expert>\n{expert_view(expert)}\n</expert>\n"
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


def step(log, stage, label, model, before, detail, **extra):
    log({"stage": stage, "label": label, "detail": detail, "cached": model.stats["cached"] > before, **extra})


def expert_view(expert, items=True):
    view = {"name": expert["name"], "headline": expert["headline"], "domains": expert["domains"]}
    if items:
        view["items"] = [{k: i[k] for k in ("id", "kind", "summary")} for i in expert["items"]]
    return json.dumps(view, ensure_ascii=False, indent=1)


def find_cases(model, search, expert, keep, pdf_dir, log, per_search=8):
    """Search, download and screen. Returns (cases to profile, discovery record for the report)."""
    before = model.stats["cached"]
    searches = model.ask("case_search_v1", f"<expert>\n{expert_view(expert)}\n</expert>", discover.parse_searches, 1000,
                         label="Search terms")
    step(log, "plan", "Search terms", model, before, "; ".join(s["label"] for s in searches))
    found, seen = [], set()
    for s in searches:
        try:
            rows, cached = search.search(s["terms"])
        except discover.DiscoveryError as exc:
            # Keep what earlier searches found; the report shows which search did not run.
            s.update(results=0, taken=0, error=str(exc))
            log({"stage": "search", "label": s["label"], "detail": f"not run: {exc}", "cached": False})
            continue
        fresh = [r for r in rows if r["docket_id"] not in seen][:per_search]
        seen.update(r["docket_id"] for r in fresh)
        found += [{**r, "found_by": s["label"]} for r in fresh]
        s.update(results=len(rows), taken=len(fresh))
        model.record("courtlistener_search", s["label"], query=discover.query(s["terms"]), cached=cached,
                     results=[{k: r.get(k) for k in ("name", "court", "filed", "url", "description")} for r in rows],
                     taken=[r["url"] for r in fresh])
        log({"stage": "search", "label": s["label"], "detail": f"{len(rows)} complaints found, {len(fresh)} new taken",
             "cached": cached})
    candidates = []
    for case in found:
        try:
            case["pdf"] = str(search.download(case, pdf_dir))
        except discover.DiscoveryError as exc:
            case["download_error"] = str(exc)
            continue
        case["excerpt"] = discover.first_pages(case["pdf"])
        case["docket_number"] = discover.case_number(case["excerpt"])
        candidates.append(case)
    listing = [{"id": f"C{n}", "name": c["name"], "court": c["court"], "filed": c["filed"], "description": c["description"],
                "first_pages": c["excerpt"]} for n, c in enumerate(candidates, 1)]
    before = model.stats["cached"]
    screened = model.ask("case_screen_v1", f"<expert>\n{expert_view(expert, items=False)}\n</expert>\n<candidates>\n"
                         f"{json.dumps(listing, ensure_ascii=False, indent=1)}\n</candidates>",
                         discover.parse_screen, 6000, label="Screening") if listing else {}
    for n, case in enumerate(candidates, 1):
        verdict = screened.get(f"C{n}") or {"relevant": False, "domains": [], "case_type": None,
                                            "reason": "Not assessed by the screening step."}
        case["screen"] = verdict
    relevant = [c for c in candidates if c["screen"]["relevant"]]
    kept = discover.diverse(relevant, keep)
    step(log, "screen", "Screening", model, before,
         f"{len(relevant)} of {len(candidates)} candidates look relevant, {len(kept)} kept for full reading",
         total_hint=len(kept))
    for case in candidates:
        case["kept"] = case in kept
    record = {"searches": searches, "days": search.days,
              "candidates": [{k: c.get(k) for k in ("name", "court", "filed", "url", "found_by", "screen", "kept",
                                                    "download_error", "docket_number")} for c in found]}
    cases = [{k: c[k] for k in ("name", "court", "filed", "url", "document_label", "document_url", "pdf", "docket_number",
                                "found_by")} for c in kept]
    return cases, record


def load_case(model, case, base, limit, log):
    case = {"document_label": "Complaint", "document_short": "Complaint", **case}
    before = model.stats["cached"]
    try:
        pages, url = case_document(case, base)
    except ExtractionError as exc:
        pages, url, case["note"] = None, None, f"The document could not be read: {exc}"
    case["document_url"] = case.get("document_url") or url
    if not pages or sum(len(p) for p in pages) < MIN_CASE_CHARS:
        case["note"] = case.get("note") or "No readable case document on CourtListener."
        step(log, "case", case["name"], model, before, "no readable document")
        return case, None
    profile = case_profile(model, case, pages, limit)
    step(log, "case", case["name"], model, before,
         f"{len(profile['issues'])} issues, {len(profile['expertise'])} expertise areas, "
         f"{profile['pages_read']}/{profile['pages']} pages read, {len(profile['dropped'])} unverified claims dropped")
    return case, profile


def run(model, cv_paths, cases, base, limit, log=print, search=None, keep=6, pdf_dir=None, cv_name=None):
    """log receives one dict per finished step: stage, label, detail, cached (and total_hint after screening).

    With a search (CourtListenerSearch), new cases are found for each expert and added to `cases`."""
    experts = []
    for path in cv_paths:
        before = model.stats["cached"]
        expert = expert_profile(model, path, cv_name)
        step(log, "expert", expert["name"], model, before,
             f"{len(expert['items'])} experience items verified, {len(expert['dropped'])} dropped")
        pool, discovery = list(cases), None
        if search:
            found, discovery = find_cases(model, search, expert, keep, pdf_dir, log)
            known = {c.get("url") for c in pool}
            pool += [c for c in found if c["url"] not in known]
        results = []
        for case in pool:
            case, profile = load_case(model, case, base, limit, log)
            before = model.stats["cached"]
            if profile is None:
                result = matching.insufficient("No readable case document, so the fit cannot be judged.")
            elif not profile["issues"]:
                result = matching.insufficient("No issue in the case document could be verified.")
            else:
                parsed = model.ask("case_match_v1", match_input(expert, case, profile), matching.parse_match, 4000,
                                   label=case["name"])
                result = matching.enforce(parsed, expert, profile)
            results.append({"case": case, "profile": profile, "match": result,
                            "score": matching.score(result, profile, case)})
            step(log, "match", case["name"], model, before,
                 f"{matching.LABELS[result['tier']]}, {len(result['matches'])} verified reasons")
        experts.append({"profile": expert, "results": matching.rank(results), "discovery": discovery})
    return experts


def make_search(env, data_dir, days=365):
    """A CourtListener search client with its own request ledger. Local runs are capped at
    LOCAL_LIMITS because the cloud pipeline uses the same account (125 requests a day)."""
    import requests

    from src.core.ratelimit import RequestBudget
    from src.store.pipeline import PipelineStore

    token = (env.get("COURTLISTENER_TOKEN") or "").strip()
    if not token:
        raise ConfigError("Missing COURTLISTENER_TOKEN")
    session = requests.Session()
    session.headers.update({"Authorization": "Token " + token, "Accept": "application/json"})
    data_dir.mkdir(parents=True, exist_ok=True)
    budget = RequestBudget(PipelineStore(str(data_dir / "courtlistener.db")), LOCAL_LIMITS)
    return discover.CourtListenerSearch(session, budget, data_dir / "search", days)


def print_step(item):
    print(f"{item['stage']:6} {item['label']}: {item['detail']}{' (cached)' if item['cached'] else ''}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cv", type=Path, action="append", required=True, help="Expert CV PDF (repeatable)")
    parser.add_argument("--cases", type=Path, required=True, help="JSON list of cases")
    parser.add_argument("--find-cases", action="store_true", help="Also search CourtListener for new cases (3 requests per CV)")
    parser.add_argument("--keep", type=int, default=6, help="Found cases to read in full")
    parser.add_argument("--days", type=int, default=365, help="Search complaints filed in the last N days")
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
        search = make_search(os.environ, args.out, args.days) if args.find_cases else None
        experts = run(model, args.cv, cases, ROOT, args.max_case_chars, print_step, search, args.keep, args.out / "pdfs")
    except (ConfigError, LLMError, InvalidOutput, ExtractionError, discover.DiscoveryError, OSError, ValueError) as exc:
        print(f"Matching failed: {type(exc).__name__}: {exc}")
        return 1
    stamp = datetime.now(timezone.utc)
    stem = args.out / f"report_{stamp:%Y%m%dT%H%M%S}"
    stem.with_suffix(".json").write_text(json.dumps(experts, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(f"{stem}_trace.json").write_text(json.dumps(model.trace, ensure_ascii=False), encoding="utf-8")
    stem.with_suffix(".html").write_text(render(experts, f"{stamp:%Y-%m-%d %H:%M} UTC", settings.model), encoding="utf-8")
    s = model.stats
    print(f"model {settings.model} · calls {s['calls']} · cached {s['cached']} · tokens in {s['input_tokens']} out {s['output_tokens']}")
    print(f"Saved {stem}.html and .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
