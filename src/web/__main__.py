"""Start the local matching web app: python -m src.web --dotenv [--port 8000]"""
import argparse
import os

from src.matching.run import ROOT
from src.web.app import create_app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dotenv", action="store_true", help="Load LLM_* settings from the local .env")
    args = parser.parse_args(argv)
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    # 127.0.0.1 only: the app spends the LLM budget and stores CVs, so it must not be reachable from the network.
    create_app(os.environ).run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
