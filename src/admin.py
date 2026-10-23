"""Manage recipients and docket subscriptions. Run locally; addresses stay in the database.

python -m src.admin --dotenv add-recipient lawyer@example.com --name "A. Lawyer"
python -m src.admin --dotenv subscribe lawyer@example.com 73132799 --min-importance medium
python -m src.admin --dotenv list
"""
import argparse
import sys
import uuid

from src.core.config import ROOT, ConfigError
from src.core.models import IMPORTANCE
from src.notify.email import validate_address
from src.pipeline.runner import open_store
from src.store.db import StoreError, now


def recipient_id(store, email):
    row = store.execute("SELECT recipient_id FROM recipients WHERE email=?", (email,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown recipient {email}; add it first")
    return row["recipient_id"]


def add_recipient(store, email, name=None):
    email = validate_address(email.strip().lower())
    with store.transaction():
        store.execute("INSERT INTO recipients VALUES (?,?,?,1,?) ON CONFLICT(email) DO UPDATE SET active=1,name=COALESCE(excluded.name,recipients.name)",
                      (uuid.uuid4().hex, email, name, now()))
    return recipient_id(store, email)


def subscribe(store, email, docket_id, min_importance="medium"):
    if docket_id <= 0:
        raise ValueError("Docket ID must be a positive integer")
    if min_importance not in IMPORTANCE:
        raise ValueError(f"min_importance must be one of {', '.join(IMPORTANCE)}")
    with store.transaction():
        store.execute("INSERT INTO subscriptions VALUES (?,?,?,1,?) ON CONFLICT(recipient_id,docket_id) "
                      "DO UPDATE SET active=1,min_importance=excluded.min_importance",
                      (recipient_id(store, email.strip().lower()), docket_id, min_importance, now()))


def set_active(store, email, docket_id, active):
    rid = recipient_id(store, email.strip().lower())
    with store.transaction():
        if docket_id is None:
            store.execute("UPDATE recipients SET active=? WHERE recipient_id=?", (int(active), rid))
        else:
            store.execute("UPDATE subscriptions SET active=? WHERE recipient_id=? AND docket_id=?", (int(active), rid, docket_id))


def listing(store):
    lines = []
    for r in store.execute("SELECT * FROM recipients ORDER BY email").fetchall():
        lines.append(f"{r['email']} ({r['name'] or '-'}) {'active' if r['active'] else 'inactive'}")
        for s in store.execute("SELECT * FROM subscriptions WHERE recipient_id=? ORDER BY docket_id", (r["recipient_id"],)).fetchall():
            lines.append(f"  docket {s['docket_id']} min_importance={s['min_importance']} {'active' if s['active'] else 'inactive'}")
    for w in store.execute("SELECT * FROM docket_watch ORDER BY docket_id").fetchall():
        lines.append(f"watch {w['docket_id']} cursor={w['cursor_at']} last_success={w['last_success_at']} error={w['last_error'] or '-'}")
    return "\n".join(lines) or "No recipients."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dotenv", action="store_true", help="Load DATABASE_URL from the local .env file")
    parser.add_argument("--sqlite", help="Use a local SQLite file (development only)")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add-recipient")
    add.add_argument("email")
    add.add_argument("--name")
    for name in ("subscribe", "unsubscribe"):
        sub = commands.add_parser(name)
        sub.add_argument("email")
        sub.add_argument("docket_id", type=int)
        if name == "subscribe":
            sub.add_argument("--min-importance", default="medium", choices=IMPORTANCE)
    for name in ("deactivate-recipient", "activate-recipient"):
        commands.add_parser(name).add_argument("email")
    commands.add_parser("list")
    args = parser.parse_args(argv)
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    try:
        store = open_store(args.sqlite)
    except (ConfigError, StoreError) as exc:
        print(f"FAIL: database: {exc}", file=sys.stderr)
        return 1
    try:
        if args.command == "add-recipient":
            add_recipient(store, args.email, args.name)
        elif args.command == "subscribe":
            subscribe(store, args.email, args.docket_id, args.min_importance)
        elif args.command == "unsubscribe":
            set_active(store, args.email, args.docket_id, False)
        elif args.command in ("deactivate-recipient", "activate-recipient"):
            set_active(store, args.email, None, args.command == "activate-recipient")
        print(listing(store))
        return 0
    except (ValueError, StoreError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
