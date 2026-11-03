"""Run the matching for one CV uploaded on the results website (a request, migration 008):

python -m src.web.request --id REQUEST_ID          (.github/workflows/matching-request.yml)

Reads the request's CV from the cloud database, searches CourtListener for complaints filed in the
last year (no fixed case pool), and saves the run like a daily check so the website shows it. The
request's state follows along: running (with progress after every step), then done with the
run id, or failed with the error. The CV copy in the request is cleared at the end unless it was
also saved for daily checks. The run belongs to whoever uploaded the CV (the request's owner).
--sqlite PATH stands in for the cloud database.
"""
import argparse
import base64
import os
import sys
import tempfile
from pathlib import Path

from src.core.config import ConfigError
from src.llm.config import LLMSettings
from src.matching.run import ROOT
from src.store.db import StoreError
from src.web.jobs import Jobs


def progress(status):
    steps = status["steps"]
    last = f": {steps[-1]['label']}" if steps else ""
    return f"Step {len(steps)} of about {max(status['total'], len(steps))}{last}"


def main(argv=None, data_dir=ROOT / "data" / "matching"):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", required=True, help="Request id from the website")
    parser.add_argument("--dotenv", action="store_true", help="Load settings from the local .env")
    parser.add_argument("--sqlite", help="A local SQLite file instead of DATABASE_URL")
    parser.add_argument("--mark-failed", metavar="REASON", help="Only mark an unfinished request failed (workflow cleanup)")
    args = parser.parse_args(argv)
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    from src.matching.cloud import CloudRuns, open_store
    try:
        store = open_store(os.environ, args.sqlite)
    except (ConfigError, StoreError) as exc:
        print(f"Database: {exc}")
        return 1
    cloud = CloudRuns(store)
    try:
        if args.mark_failed:
            request = cloud.request(args.id)
            if request and request["state"] in ("queued", "running"):
                cloud.update_request(args.id, state="failed", error=args.mark_failed, progress="Stopped",
                                     **({} if request["save"] else {"pdf_base64": None}))
            return 0
        return run(cloud, store, args.id, data_dir)
    finally:
        store.close()


def run(cloud, store, request_id, data_dir):
    request = cloud.request(request_id)
    if request is None or request["state"] != "queued" or not request["pdf_base64"]:
        print(f"Request {request_id}: not found or not waiting to run.")
        return 1
    try:
        settings = LLMSettings.from_env(os.environ)
    except ConfigError as exc:
        cloud.update_request(request_id, state="failed", error=f"LLM settings: {exc}")
        print(f"LLM settings: {exc}")
        return 1
    cloud.update_request(request_id, state="running", progress="Starting", error=None)
    jobs = Jobs(data_dir / "runs", settings, [], ROOT, 160000, data_dir, os.environ, ledger=store,
                on_step=lambda status: cloud.update_request(request_id, progress=progress(status)))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / request["cv_name"]
        path.write_bytes(base64.b64decode(request["pdf_base64"]))
        run_id = jobs.start(path, request["cv_name"], find=True, wait=True)
    status = jobs.status(run_id)
    try:
        cloud.save_run(jobs.folder(run_id), owner=request["owner"])
    except StoreError as exc:
        status = {**status, "state": "failed", "error": f"Results not saved: {exc}"}
    done = status["state"] == "done"
    cloud.update_request(request_id, state="done" if done else "failed", run_id=run_id if done else None,
                         error=None if done else status["error"], progress="Finished" if done else "Stopped",
                         **({} if request["save"] else {"pdf_base64": None}))
    print(f"{request['cv_name']}: {status['state']} (run {run_id}){' - ' + status['error'] if status['error'] else ''}")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
