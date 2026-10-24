"""Route stage: decide which subscribers receive each analyzed event."""
from src.core.models import SHADOW_ANALYZER, Analysis, importance_rank
from src.store.db import now as utcnow


def latest_analysis(store, event_id):
    row = store.execute("SELECT result_json FROM analyses WHERE event_id=? AND analyzer<>? ORDER BY created_at DESC LIMIT 1",
                        (event_id, SHADOW_ANALYZER)).fetchone()
    return Analysis.from_json(row["result_json"]) if row else None


def route(store):
    report = {"routed": 0, "skipped": 0, "notifications": 0}
    events = store.execute("SELECT event_id,docket_id FROM events WHERE status='analyzed' ORDER BY detected_at,event_id").fetchall()
    for event in events:
        analysis = latest_analysis(store, event["event_id"])
        subscribers = store.execute(
            "SELECT s.recipient_id,s.min_importance FROM subscriptions s JOIN recipients r ON r.recipient_id=s.recipient_id "
            "WHERE s.docket_id=? AND s.active=1 AND r.active=1", (event["docket_id"],)).fetchall()
        targets = [s["recipient_id"] for s in subscribers
                   if importance_rank(analysis.importance) >= importance_rank(s["min_importance"])]
        with store.transaction():
            for recipient_id in targets:
                created = store.execute("INSERT INTO notifications VALUES (?,?,'pending',NULL,?,?) "
                                        "ON CONFLICT(event_id,recipient_id) DO NOTHING",
                                        (event["event_id"], recipient_id, utcnow(), utcnow()))
                report["notifications"] += created.rowcount
            status = "routed" if targets else "skipped"
            store.execute("UPDATE events SET status=?,updated_at=? WHERE event_id=? AND status='analyzed'",
                          (status, utcnow(), event["event_id"]))
        report[status] += 1
    return report
