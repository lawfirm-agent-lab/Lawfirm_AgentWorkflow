"""Daily check from the command line, for every saved CV (or the ones named):

python -m src.web.daily --dotenv [--days 1] [--cv Mark_C_Nicely.ExpertWitness.pdf]

Same runs as the web app's "Check new cases" (same run folders, so they show on the Today page),
one CV after another. About 3 CourtListener requests per CV; the local cap still applies.
"""
import argparse
import os
import sys

from src.core.config import ConfigError
from src.llm.config import LLMSettings
from src.matching.run import ROOT
from src.web.jobs import Jobs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=1, help="Complaints filed in the last N days")
    parser.add_argument("--cv", action="append", help="Saved CV file name in data/matching/cvs (repeatable)")
    parser.add_argument("--dotenv", action="store_true", help="Load settings from the local .env")
    args = parser.parse_args(argv)
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    data_dir = ROOT / "data" / "matching"
    saved = sorted(p.name for p in (data_dir / "cvs").glob("*.pdf"))
    names = args.cv or saved
    missing = [n for n in names if n not in saved]
    if missing or not names:
        print(f"No saved CV named: {', '.join(missing)}" if missing else "No saved CV in data/matching/cvs")
        return 1
    try:
        settings = LLMSettings.from_env(os.environ)
    except ConfigError as exc:
        print(f"LLM settings: {exc}")
        return 1
    jobs = Jobs(data_dir / "runs", settings, [], ROOT, 160000, data_dir, os.environ)
    failed = 0
    for name in names:
        run_id = jobs.start(data_dir / "cvs" / name, name, days=args.days, wait=True)
        status = jobs.status(run_id)
        print(f"{name}: {status['state']} (run {run_id}){' - ' + status['error'] if status['error'] else ''}")
        failed += status["state"] != "done"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
