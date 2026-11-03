"""Daily check from the command line, for every saved CV (or the ones named):

python -m src.web.daily --dotenv [--days 1] [--cv Mark_C_Nicely.ExpertWitness.pdf]
python -m src.web.daily --cloud [--days 2]          (the scheduled GitHub Actions job)

Same runs as the web app's "Check new cases" (same run folders, so they show on the Today page),
one CV after another. About 3 CourtListener requests per CV; the local cap still applies.

With --cloud the saved CVs and earlier checks come from the cloud database (src.matching.cloud),
each new check is saved back to it, and CourtListener requests count against the docket
pipeline's ledger there instead of the local one. --sqlite PATH stands in for the cloud database.
"""
import argparse
import os
import sys

from src.core.config import ConfigError
from src.llm.config import LLMSettings
from src.matching.run import ROOT
from src.store.db import StoreError
from src.web.jobs import Jobs


def main(argv=None, data_dir=ROOT / "data" / "matching"):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=1, help="Complaints filed in the last N days")
    parser.add_argument("--cv", action="append", help="Saved CV file name in data/matching/cvs (repeatable)")
    parser.add_argument("--dotenv", action="store_true", help="Load settings from the local .env")
    parser.add_argument("--cloud", action="store_true", help="CVs, earlier checks and results in the cloud database")
    parser.add_argument("--sqlite", help="With --cloud: a local SQLite file instead of DATABASE_URL")
    args = parser.parse_args(argv)
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    cloud = store = None
    if args.cloud:
        from src.matching.cloud import CloudRuns, open_store
        try:
            store = open_store(os.environ, args.sqlite)
            cloud = CloudRuns(store)
            # Earlier checks first: they decide which cases this check leaves out as already shown.
            print(f"Cloud: {len(cloud.write_cvs(data_dir / 'cvs'))} saved CVs, "
                  f"{cloud.pull(data_dir / 'runs')} earlier checks copied.")
        except (ConfigError, StoreError) as exc:
            print(f"Database: {exc}")
            return 1
    try:
        return check(args, data_dir, cloud, store)
    finally:
        if store is not None:
            store.close()


def check(args, data_dir, cloud, store):
    saved = sorted(p.name for p in (data_dir / "cvs").glob("*.pdf"))
    names = args.cv or saved
    missing = [n for n in names if n not in saved]
    if missing or not names:
        where = "the cloud database" if cloud else "data/matching/cvs"
        print(f"No saved CV named: {', '.join(missing)}" if missing else f"No saved CV in {where}")
        return 1
    try:
        settings = LLMSettings.from_env(os.environ)
    except ConfigError as exc:
        print(f"LLM settings: {exc}")
        return 1
    jobs = Jobs(data_dir / "runs", settings, [], ROOT, 160000, data_dir, os.environ, cloud=cloud, ledger=store)
    failed = 0
    for name in names:
        run_id = jobs.start(data_dir / "cvs" / name, name, days=args.days, wait=True)
        status = jobs.status(run_id)
        problem = status["error"] or (f"not saved to the cloud: {status['cloud_error']}" if status.get("cloud_error") else "")
        print(f"{name}: {status['state']} (run {run_id}){' - ' + problem if problem else ''}")
        failed += status["state"] != "done" or bool(status.get("cloud_error"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
