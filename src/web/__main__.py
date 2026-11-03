"""Start the local matching web app: python -m src.web --dotenv [--port 8000] [--cloud]

--cloud connects to the cloud database (DATABASE_URL): the "New cases by day" page shows the
scheduled cloud checks, and saved CVs and daily checks run here are saved there too.
"""
import argparse
import os
import sys

from src.core.config import ConfigError
from src.matching.run import ROOT
from src.store.db import StoreError
from src.web.app import create_app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dotenv", action="store_true", help="Load LLM_* settings from the local .env")
    parser.add_argument("--cloud", action="store_true", help="Show and save daily checks in the cloud database")
    args = parser.parse_args(argv)
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    # 127.0.0.1 only: the app spends the LLM budget and stores CVs, so it must not be reachable from the network.
    cloud = None
    if args.cloud:
        from src.matching.cloud import CloudRuns, open_store
        try:
            open_store(os.environ).close()  # Fail at start, not on the first page, if it is unreachable.
        except (ConfigError, StoreError) as exc:
            sys.exit(f"Database: {exc}")
        cloud = CloudRuns(connect=lambda: open_store(os.environ))
    create_app(os.environ, cloud=cloud).run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
