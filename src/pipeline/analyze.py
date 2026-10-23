"""Analyze stage: score detected events with the configured analyzer."""
from src.core.models import Event
from src.store.db import now as utcnow

MAX_ATTEMPTS = 3


def analyze(store, analyzer, limit=200):
    report = {"analyzer": f"{analyzer.name}/{analyzer.version}", "analyzed": 0, "failed": 0}
    rows = store.execute("SELECT * FROM events WHERE status='detected' ORDER BY detected_at,event_id LIMIT ?",
                         (limit,)).fetchall()
    for row in rows:
        event = Event.from_row(row)
        try:
            result = analyzer.analyze(event)
        except Exception as exc:
            # Type only: analyzer errors may echo provider responses or prompts.
            with store.transaction():
                store.execute("UPDATE events SET attempts=attempts+1,last_error=?,updated_at=?,"
                              "status=CASE WHEN attempts+1>=? THEN 'failed' ELSE status END WHERE event_id=?",
                              (type(exc).__name__, utcnow(), MAX_ATTEMPTS, event.event_id))
            report["failed"] += 1
            continue
        with store.transaction():
            store.execute("INSERT INTO analyses VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(event_id,analyzer,analyzer_version) DO NOTHING",
                          (event.event_id, analyzer.name, analyzer.version, result.importance, result.category,
                           result.summary, result.to_json(), utcnow()))
            store.execute("UPDATE events SET status='analyzed',updated_at=? WHERE event_id=? AND status='detected'",
                          (utcnow(), event.event_id))
        report["analyzed"] += 1
    return report
