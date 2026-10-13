"""Run one check, or import existing snapshots as initial baselines."""
import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
import requests
import yaml

from src.ingest.courtlistener import fetch_entries
from src.store.db import Store, StoreError
from src.notify.digest import build_digest
from src.notify.email import deliver, settings
from src.notify.queue import send_pending_changes

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--import-baseline", action="store_true",
                        help="Import saved JSON for uninitialized dockets; do not call the API")
    parser.add_argument("--email-demo", action="store_true", help="Select up to five recent important historical entries")
    parser.add_argument("--send-email", action="store_true", help="Send digests via configured SMTP; otherwise save previews only")
    parser.add_argument("--cloud-check", action="store_true",
                        help="Fetch to required PostgreSQL using environment only; no email or local exports")
    parser.add_argument("--cloud-email", action="store_true",
                        help="Explicit cloud fetch plus pending-change email delivery; no .env or exports")
    args = parser.parse_args()
    if args.cloud_email:
        if args.cloud_check or args.import_baseline or args.email_demo or args.send_email:
            parser.error("--cloud-email must be used on its own")
        args.cloud_check = True
    if args.cloud_check and (args.import_baseline or args.email_demo or args.send_email):
        parser.error("--cloud-check cannot be combined with baseline import or email options")
    if args.import_baseline and (args.email_demo or args.send_email):
        parser.error("Email options cannot be combined with --import-baseline")
    if not args.cloud_check:
        load_dotenv(ROOT / ".env")
    database_url = os.environ.get("DATABASE_URL", "").strip() or None
    if args.cloud_check:
        if not database_url:
            print("Missing DATABASE_URL secret; cloud checks cannot use SQLite.")
            return 1
        try:
            from psycopg.conninfo import conninfo_to_dict
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            if not database_url.startswith(("postgresql://", "postgres://")):
                raise ValueError("Invalid database URI")
            config = conninfo_to_dict(database_url)
            mode = config.get("sslmode", "require")
            if mode not in ("require", "verify-ca", "verify-full"):
                raise ValueError("TLS required")
            parts = urlsplit(database_url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["sslmode"] = mode
            query["options"] = "-c statement_timeout=30000 -c lock_timeout=10000 -c search_path=public"
            database_url = urlunsplit(parts._replace(query=urlencode(query)))
        except Exception:
            print("Invalid cloud database configuration; use a PostgreSQL URI with TLS and install requirements.txt.")
            return 1
    token = os.environ.get("COURTLISTENER_TOKEN", "").strip()
    if not args.import_baseline and not token:
        print("Missing COURTLISTENER_TOKEN; configure the repository secret." if args.cloud_check
              else "Missing COURTLISTENER_TOKEN; fill .env locally.")
        return 1
    try:
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        dockets = config.get("dockets") if isinstance(config, dict) else None
        if not isinstance(dockets, list) or not dockets or any(type(d) is not int or d <= 0 for d in dockets):
            raise ValueError("config.yaml requires positive integer docket IDs")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Configuration error: {exc}")
        return 1
    if not args.cloud_check:
        (ROOT / "data").mkdir(exist_ok=True)
    try:
        email_config = settings() if (args.send_email or args.cloud_email) else None
    except ValueError as exc:
        print(exc)
        return 1
    try:
        store = Store(ROOT / "data/docket.db",
                      database_url=database_url)
    except StoreError as exc:
        print(exc)
        return 1
    failed = False
    try:
        for docket_id in dict.fromkeys(dockets):
            if args.import_baseline and store.initialized(docket_id):
                print(f"Docket {docket_id}: baseline already exists; skipped.")
                continue
            run_id = store.start(docket_id, "saved_snapshot" if args.import_baseline else "api")
            output = ROOT / "data" / f"{docket_id}_entries.json"
            try:
                rows = json.loads(output.read_text(encoding="utf-8")) if args.import_baseline else fetch_entries(docket_id, token)
                result = store.apply(run_id, rows)
            except (requests.RequestException, ValueError, OSError, StoreError) as exc:
                failed = True
                # Avoid persisting credentials if a downstream exception includes them.
                error = str(exc).replace(token, "[REDACTED]") if token else str(exc)
                if args.cloud_check:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    error = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
                store.fail(run_id, error)
                print(f"Docket {docket_id}: check failed: {error}. Baseline retained.")
                continue
            label = "baseline established" if result["baseline"] else "check successful"
            print(f"Docket {docket_id}: {label}; fetched={result['fetched']}, added={result['added']}, modified={result['modified']}, missing={result['missing']}; run={run_id}")
            if result["missing"]:
                print("Warning: previously seen entries are missing from this response; stored entries retained.")
            if not args.import_baseline and not args.cloud_check:
                try:
                    temporary = output.with_suffix(".json.tmp")
                    temporary.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(output)
                except OSError as exc:
                    print(f"JSON export warning: {exc}. Database check results are already saved.")
            if not args.import_baseline and not args.cloud_check:
                # Historical demo is deliberately separate from the change notification queue.
                if args.email_demo:
                    batches = [(f"demo-v1:{docket_id}", rows)]
                else:
                    batches = [(f"change:{change['id']}", [json.loads(change["after_json"])])
                               for change in store.list_changes(docket_id)]
                matched = False
                for event_key, candidates in batches:
                    digest = build_digest(docket_id, candidates, demo=args.email_demo, detected_change=not args.email_demo)
                    if not digest:
                        continue
                    matched = True
                    preview = ROOT / "data" / f"email_{event_key.replace(':', '_')}.txt"
                    preview.write_text(digest[1], encoding="utf-8")
                    print(f"Email preview: {preview}")
                    if args.send_email:
                        try:
                            print(deliver(store, docket_id, digest, event_key, email_config))
                        except RuntimeError as exc:
                            failed = True
                            print(exc)
                if not matched:
                    print("No important updates selected; no email sent.")
        if args.cloud_email:
            # Drain persisted changes even when today's API fetch failed.
            for docket_id in dict.fromkeys(dockets):
                if send_pending_changes(store, docket_id, email_config):
                    failed = True
    except StoreError as exc:
        failed = True
        print(f"Check stopped: {exc}. Inspect run and email status before retrying.")
    finally:
        store.close()
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
