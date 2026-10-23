"""Run pipeline stages in order: detect -> analyze -> route -> deliver.

Cloud:  python -m src.pipeline.runner             (environment variables only)
Local:  python -m src.pipeline.runner --dotenv --dry-run
Dev:    python -m src.pipeline.runner --sqlite data/pipeline.db --stages analyze,route,deliver --dry-run

A failed stage is reported and later stages still run, so pending work drains even
when CourtListener is unavailable. Exit code 1 means something needs attention.
"""
import argparse
import json
import os
import uuid
from datetime import datetime, timezone

import requests

from src.analyzers.registry import create_analyzer
from src.core.config import ROOT, ConfigError, load_settings, secure_database_url
from src.core.ratelimit import RequestBudget
from src.notifiers.email import EmailNotifier
from src.notify.email import settings as email_settings
from src.pipeline.analyze import analyze
from src.pipeline.deliver import deliver
from src.pipeline.detect import detect
from src.pipeline.route import route
from src.sources.courtlistener import CourtListenerSource
from src.store.db import StoreError
from src.store.pipeline import PipelineStore

STAGES = ("detect", "analyze", "route", "deliver")
LOCK_KEY = 7313279902
SAFE_ERRORS = (ConfigError, StoreError)


def open_store(sqlite_path):
    if sqlite_path:
        return PipelineStore(sqlite_path)
    return PipelineStore(database_url=secure_database_url(os.environ.get("DATABASE_URL")))


def parse_stages(text):
    stages = [s.strip() for s in text.split(",") if s.strip()]
    unknown = set(stages) - set(STAGES)
    if unknown or not stages:
        raise argparse.ArgumentTypeError(f"stages must be a comma-separated subset of {','.join(STAGES)}")
    return [s for s in STAGES if s in stages]


def run(store, stages, settings, *, dry_run=False, session=None, notifier=None, analyzer=None,
        now=None, budget=None, preview_dir=None):
    now = now or datetime.now(timezone.utc)
    report = {"run_id": uuid.uuid4().hex, "started_at": now.isoformat(), "stages": {}}
    with store.transaction():
        store.execute("INSERT INTO pipeline_runs VALUES (?,?,NULL,'running',NULL)", (report["run_id"], now.isoformat()))
    for stage in stages:
        try:
            if stage == "detect":
                budget = budget or RequestBudget(store, settings.limits)
                source = CourtListenerSource(session, budget, settings.limits.max_pages_per_docket)
                result = detect(store, source, settings, now)
                result["requests"] = budget.used
            elif stage == "analyze":
                result = analyze(store, analyzer)
            elif stage == "route":
                result = route(store)
            else:
                result = deliver(store, notifier, settings, dry_run=dry_run, preview_dir=preview_dir)
            result["status"] = "failed" if result.get("failed") else "ok"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}" if isinstance(exc, SAFE_ERRORS) else type(exc).__name__
            result = {"status": "failed", "error": error}
        report["stages"][stage] = result
    needs_review = report["stages"].get("deliver", {}).get("needs_review", 0)
    report["status"] = "failed" if any(r["status"] == "failed" for r in report["stages"].values()) or needs_review else "ok"
    with store.transaction():
        store.execute("UPDATE pipeline_runs SET finished_at=?,status=?,report_json=? WHERE run_id=?",
                      (datetime.now(timezone.utc).isoformat(), report["status"], json.dumps(report), report["run_id"]))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stages", type=parse_stages, default=list(STAGES), help="Default: all stages")
    parser.add_argument("--dry-run", action="store_true", help="Render digests without sending email")
    parser.add_argument("--dotenv", action="store_true", help="Load the local .env file (never used in the cloud)")
    parser.add_argument("--sqlite", help="Use a local SQLite file instead of DATABASE_URL (development only)")
    args = parser.parse_args(argv)
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    try:
        settings = load_settings()
        token = os.environ.get("COURTLISTENER_TOKEN", "").strip()
        if "detect" in args.stages and not token:
            raise ConfigError("COURTLISTENER_TOKEN is missing or empty")
        analyzer = create_analyzer(settings.analyzer) if "analyze" in args.stages else None
        email_cfg = email_settings(require_recipient=False) if "deliver" in args.stages and not args.dry_run else None
    except ValueError as exc:
        # ConfigError and email settings errors carry safe, secret-free messages.
        print(f"FAIL: configuration: {exc}")
        return 1
    try:
        store = open_store(args.sqlite)
    except (ConfigError, StoreError) as exc:
        print(f"FAIL: database: {exc}")
        return 1
    try:
        if store.backend == "postgres" and not store.execute("SELECT pg_try_advisory_lock(?) AS ok", (LOCK_KEY,)).fetchone()["ok"]:
            print("FAIL: another pipeline run holds the lock; try again after it finishes.")
            return 1
        with requests.Session() as session:
            session.headers.update({"Authorization": "Token " + token, "Accept": "application/json"})
            notifier = EmailNotifier(store, email_cfg) if email_cfg else None
            report = run(store, args.stages, settings, dry_run=args.dry_run, session=session, notifier=notifier,
                         analyzer=analyzer, preview_dir=None if os.environ.get("GITHUB_ACTIONS") else ROOT / "data" / "previews")
    except StoreError as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        store.close()
    output = json.dumps(report, indent=2)
    print(output)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("## Pipeline run\n```json\n" + output + "\n```\n")
    return int(report["status"] != "ok")


if __name__ == "__main__":
    raise SystemExit(main())
