"""Detect stage: fetch changed entries per watched docket and record new content versions.

Events, the latest entry snapshot and the docket cursor commit in one transaction, so a
failure never advances the cursor past unrecorded changes. Replays are idempotent.
"""
import hashlib
import json
from datetime import timedelta

from src.ingest.probe_incremental import timestamp
from src.sources.courtlistener import QuotaExhausted, SourceError
from src.store.db import content, encode

DOCKET_URL = "https://www.courtlistener.com/api/rest/v4/dockets/{}/"


def watched_dockets(store):
    rows = store.execute(
        "SELECT DISTINCT s.docket_id FROM subscriptions s JOIN recipients r ON r.recipient_id=s.recipient_id "
        "WHERE s.active=1 AND r.active=1").fetchall()
    return [row["docket_id"] for row in rows]


def ensure_watch(store, docket_id, now):
    """Start a new watch at the last legacy success, else now. History is never backfilled."""
    legacy = store.execute("SELECT MAX(started_at) AS t FROM runs WHERE docket_id=? AND status='success'",
                           (docket_id,)).fetchone()["t"]
    store.execute("INSERT INTO docket_watch(docket_id,cursor_at,created_at) VALUES (?,?,?) ON CONFLICT(docket_id) DO NOTHING",
                  (docket_id, legacy or now.isoformat(), now.isoformat()))
    return dict(store.execute("SELECT * FROM docket_watch WHERE docket_id=?", (docket_id,)).fetchone())


def event_id(source, docket_id, entry_id, before, after):
    return hashlib.sha256(json.dumps([source, docket_id, entry_id, before, after]).encode()).hexdigest()


def save(store, source, watch, result, now):
    """Return the number of new events. Raises ValueError on invalid rows or a stale cursor."""
    docket_id, created = watch["docket_id"], 0
    with store.transaction():
        updated = store.execute(
            "UPDATE docket_watch SET cursor_at=?,last_checked_at=?,last_success_at=?,last_error=NULL,revision=revision+1 "
            "WHERE docket_id=? AND revision=?",
            (result.cursor, now.isoformat(), now.isoformat() if result.complete else watch["last_success_at"],
             docket_id, watch["revision"]))
        if updated.rowcount != 1:
            raise ValueError("Cursor conflict: another run updated this docket")
        for row in result.rows:
            if row.get("docket") != DOCKET_URL.format(docket_id):
                raise ValueError("Entry belongs to another docket or has no docket reference")
            if not isinstance(row.get("description"), str):
                raise ValueError("Entry description must be a string")
            raw, signature = encode(row), content(row)
            old = store.execute("SELECT raw_json,content_json FROM entries WHERE docket_id=? AND id=?",
                                (docket_id, row["id"])).fetchone()
            if old is not None and old["content_json"] == signature:
                continue
            ident = event_id(source, docket_id, row["id"], old["content_json"] if old else None, signature)
            inserted = store.execute(
                "INSERT INTO events(event_id,source,docket_id,entry_id,kind,payload_json,before_json,source_modified_at,detected_at,status,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'detected',?) ON CONFLICT(event_id) DO NOTHING",
                (ident, source, docket_id, row["id"], "modified" if old else "added", raw,
                 old["raw_json"] if old else None, row.get("date_modified"), now.isoformat(), now.isoformat()))
            created += inserted.rowcount
            store.execute("INSERT INTO entries VALUES(?,?,?,?) ON CONFLICT(docket_id,id) DO UPDATE SET raw_json=excluded.raw_json,content_json=excluded.content_json",
                          (docket_id, row["id"], raw, signature))
    return created


def record_error(store, docket_id, error, now):
    with store.transaction():
        store.execute("UPDATE docket_watch SET last_checked_at=?,last_error=? WHERE docket_id=?",
                      (now.isoformat(), error, docket_id))


def fetch_missing_info(store, source, watches, now, report):
    """Display metadata is fetched once per docket and once per court, after change detection,
    from leftover budget."""
    for watch in watches:
        docket_id = watch["docket_id"]
        if store.execute("SELECT 1 FROM docket_info WHERE docket_id=?", (docket_id,)).fetchone():
            continue
        try:
            info = source.fetch_docket_info(docket_id)
        except QuotaExhausted:
            return
        except SourceError as exc:
            report["info_failed"].append({"docket_id": docket_id, "error": str(exc)})
            continue
        with store.transaction():
            store.execute("INSERT INTO docket_info(docket_id,case_name,docket_number,court,absolute_url,fetched_at,court_id) "
                          "VALUES (?,?,?,?,?,?,?) ON CONFLICT(docket_id) DO NOTHING",
                          (docket_id, info["case_name"], info["docket_number"], info["court"], info["absolute_url"],
                           now.isoformat(), info.get("court_id")))
        report["info_fetched"] += 1
    missing = store.execute("SELECT DISTINCT d.court_id FROM docket_info d LEFT JOIN court_info c ON c.court_id=d.court_id "
                            "WHERE d.court_id IS NOT NULL AND c.court_id IS NULL ORDER BY d.court_id").fetchall()
    for row in missing:
        try:
            court = source.fetch_court_info(row["court_id"])
        except QuotaExhausted:
            return
        except SourceError as exc:
            report["info_failed"].append({"court_id": row["court_id"], "error": str(exc)})
            continue
        with store.transaction():
            store.execute("INSERT INTO court_info VALUES (?,?,?,?) ON CONFLICT(court_id) DO NOTHING",
                          (row["court_id"], court["citation"], court["full_name"], now.isoformat()))
        report["courts_fetched"] += 1


def detect(store, source, settings, now):
    until = now - timedelta(seconds=settings.source_lag_seconds)
    report = {"dockets": 0, "events": 0, "partial": [], "failed": [], "quota_stopped": False,
              "info_fetched": 0, "courts_fetched": 0, "info_failed": []}
    with store.transaction():
        watches = [ensure_watch(store, d, now) for d in watched_dockets(store)]
    # Least recently checked first, so a tight budget rotates fairly across dockets.
    watches.sort(key=lambda w: (w["last_checked_at"] or "", w["docket_id"]))
    for watch in watches:
        if timestamp(watch["cursor_at"]) >= until:
            continue
        try:
            result = source.fetch_docket_changes(watch["docket_id"], watch["cursor_at"], until.isoformat())
            report["events"] += save(store, source.name, watch, result, now)
        except QuotaExhausted:
            report["quota_stopped"] = True
            break
        except (SourceError, ValueError) as exc:
            # Our own messages only; they never contain credentials or response bodies.
            record_error(store, watch["docket_id"], str(exc), now)
            report["failed"].append({"docket_id": watch["docket_id"], "error": str(exc)})
            continue
        report["dockets"] += 1
        if not result.complete:
            report["partial"].append(watch["docket_id"])
    if not report["quota_stopped"]:
        fetch_missing_info(store, source, watches, now, report)
    return report
