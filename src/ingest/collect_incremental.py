"""Manual, bounded metadata collection. No email, PDF download or dotenv loading."""
import argparse
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import requests
from src.ingest.probe_incremental import BASE, check_next, timestamp
from src.store.discovery import DiscoveryStore


def utcnow():
    return datetime.now(timezone.utc)


def parameters(stream, start, end):
    relation = "docket" if stream == "docket-entries" else "cluster"
    return {"date_modified__gte": start, "date_modified__lt": end,
            "order_by": "date_modified,id", "fields": f"id,date_created,date_modified,{relation}"}


def validate(data, state, end, params):
    if not isinstance(data, dict) or not isinstance(data.get("results"), list) or "next" not in data:
        raise ValueError("Invalid envelope")
    nxt = data["next"]
    if nxt is not None:
        if not isinstance(nxt, str) or not nxt or nxt == state["next_url"]:
            raise ValueError("Invalid pagination")
        check_next(nxt, state["stream"], params)
    previous = json.loads(state["last_key"]) if state["last_key"] else None
    ids = set()
    for row in data["results"]:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            raise ValueError("Invalid record")
        dt = timestamp(row["date_modified"])
        key = [dt.astimezone(timezone.utc).isoformat(), row["id"]]
        if not timestamp(state["start_at"]) <= dt < timestamp(end):
            raise ValueError("Out of window")
        if previous and (dt, row["id"]) <= (timestamp(previous[0]), previous[1]):
            raise ValueError("Non-increasing pagination")
        if row["id"] in ids:
            raise ValueError("Duplicate record")
        ids.add(row["id"])
        previous = key
    if nxt and not data["results"]:
        raise ValueError("Empty continuing page")
    return data["results"], nxt, previous


def collect(store, session, *, max_requests=4, daily_limit=72, clock=time.time, sleep=time.sleep, now=utcnow):
    run_id = uuid.uuid4().hex
    with store.transaction():
        store.execute("INSERT INTO discovery_runs VALUES (?,?,NULL,?,NULL)", (run_id, now().isoformat(), "running"))
    report = {"run_id": run_id, "requests": 0, "pages": 0, "changed_records": 0, "status": "partial"}
    last_request = None
    def get(url, params=None):
        nonlocal last_request
        if report["requests"] >= max_requests:
            return None
        if last_request is not None:
            sleep(max(0, 13-(clock()-last_request)))
        at = clock()
        if not store.reserve(at, uuid.uuid4().hex, daily_limit):
            return None
        last_request = at
        report["requests"] += 1
        response = session.get(url, params=params, timeout=(10,30), allow_redirects=False)
        if response.status_code == 429:
            # Conservative global pause; no retry storm even on malformed Retry-After.
            store.block(clock()+86400)
        if response.status_code != 200:
            raise ValueError("HTTP request failed")
        return response.json()
    try:
        usage = get(BASE+"api-usage/")
        if usage is None:
            report["status"] = "budget_or_pacing_pause"
        else:
            # Unknown schema fails closed. Other workflows consume this shared allowance.
            limits = usage.get("current_usage", [])
            remaining = [x["remaining"] - (10 if x.get("window_seconds") == 86400 else 1)
                         for x in limits if isinstance(x, dict) and x.get("scope") == "user"
                         and type(x.get("remaining")) is int]
            if not remaining or type(remaining[0]) is not int:
                raise ValueError("Unknown quota response")
            allowance = max(0, min(remaining))
            cutoff = now()-timedelta(minutes=10)
            while report["requests"] < max_requests and allowance > 0:
                states = store.execute("SELECT * FROM discovery_state ORDER BY revision,start_at,stream").fetchall()
                state = next((dict(s) for s in states if timestamp(s["start_at"])+timedelta(minutes=15) <= cutoff), None)
                if state is None:
                    report["status"] = "caught_up_to_closed_windows"
                    break
                end = state["end_at"] or (timestamp(state["start_at"])+timedelta(minutes=15)).isoformat()
                params = parameters(state["stream"], state["start_at"], end)
                url = state["next_url"] or BASE+state["stream"]+"/"
                if state["next_url"]:
                    check_next(url, state["stream"], params)
                data = get(url, None if state["next_url"] else params)
                if data is None:
                    report["status"] = "budget_or_pacing_pause"
                    break
                allowance -= 1
                rows, nxt, previous = validate(data, state, end, params)
                report["changed_records"] += store.save_page(state, rows, end, nxt, previous)
                report["pages"] += 1
            if allowance <= 0:
                report["status"] = "remote_quota_pause"
    except Exception:
        report["status"] = "failed"
        report["error"] = "Request, validation or persistence failed; details withheld. Progress retained."
    report["streams"] = [dict(r) for r in store.execute("SELECT stream,start_at,end_at FROM discovery_state ORDER BY stream").fetchall()]
    for state in report["streams"]:
        state["lag_seconds"] = max(0, int((now()-timestamp(state["start_at"])).total_seconds()))
    with store.transaction():
        store.execute("UPDATE discovery_runs SET finished_at=?,status=?,details=? WHERE run_id=?", (now().isoformat(), report["status"], json.dumps(report), run_id))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize", action="store_true", help="Initialize at execution time; no historical backfill")
    args = parser.parse_args()
    stage = "configuration"
    try:
        url = os.environ.get("DATABASE_URL", "").strip()
        token = os.environ.get("COURTLISTENER_TOKEN", "").strip()
        if not url:
            print("FAIL: DATABASE_URL secret is missing or empty.")
            return 1
        if not url.startswith(("postgresql://", "postgres://")):
            print("FAIL: DATABASE_URL must be a PostgreSQL URI.")
            return 1
        if not token and not args.initialize:
            print("FAIL: COURTLISTENER_TOKEN secret is missing or empty.")
            return 1
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        if query.get("sslmode", "require") not in ("require", "verify-ca", "verify-full"):
            raise ValueError("TLS required")
        query.update(sslmode=query.get("sslmode", "require"), options="-c statement_timeout=30000 -c lock_timeout=10000 -c search_path=public")
        stage = "database_connection_or_schema_creation"
        print("START: Database connection and discovery schema initialization.")
        store = DiscoveryStore(database_url=urlunsplit(parts._replace(query=urlencode(query))))
        try:
            stage = "collector_lock"
            print("PASS: Database connection and discovery schema initialization.")
            locked = store.execute("SELECT pg_try_advisory_lock(7313279901) AS locked").fetchone()["locked"]
            if not locked:
                print("FAIL: Another collector holds the database lock. Retry after it finishes.")
                return 1
            if args.initialize:
                stage = "stream_initialization"
                store.initialize_streams(utcnow().isoformat())
                print("Initialized missing streams at execution time. Existing progress retained. No API requests.")
                return 0
            stage = "checkpoint_read"
            if store.execute("SELECT COUNT(*) AS n FROM discovery_state").fetchone()["n"] != 2:
                print("FAIL: Initialize missing streams first using the workflow checkbox.")
                return 1
            stage = "collection"
            with requests.Session() as session:
                session.headers.update({"Authorization": "Token "+token, "Accept": "application/json"})
                report = collect(store, session)
            output = json.dumps(report, indent=2)
            print(output)
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a") as handle:
                    handle.write("## Incremental collector result\n```json\n"+output+"\n```\n")
            return int(report["status"] == "failed")
        finally:
            store.close()
    except Exception:
        print(f"FAIL: stage={stage}. Secrets and raw response withheld.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
