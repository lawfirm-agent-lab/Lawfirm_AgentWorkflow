"""Deliver stage: one digest per recipient per run, built from pending notifications."""
import hashlib
import json
from pathlib import Path

from src.core.models import Event
from src.notify.render import render_digest
from src.pipeline.route import latest_analysis
from src.store.db import now as utcnow


def pending(store, recipient_id, limit):
    rows = store.execute("SELECT e.* FROM notifications n JOIN events e ON e.event_id=n.event_id "
                         "WHERE n.recipient_id=? AND n.status='pending' ORDER BY e.docket_id,e.entry_id,e.detected_at LIMIT ?",
                         (recipient_id, limit)).fetchall()
    return [(Event.from_row(row), latest_analysis(store, row["event_id"])) for row in rows]


def docket_info(store, docket_ids):
    rows = [store.execute("SELECT * FROM docket_info WHERE docket_id=?", (d,)).fetchone() for d in docket_ids]
    return {row["docket_id"]: dict(row) for row in rows if row}


def deliver(store, notifier, settings, dry_run=False, preview_dir=None):
    """dry_run renders digests without sending or changing state. Previews never include addresses."""
    report = {"digests": 0, "sent": 0, "needs_review": 0, "dry_run": dry_run, "previews": []}
    recipients = store.execute("SELECT DISTINCT r.recipient_id,r.email FROM notifications n "
                               "JOIN recipients r ON r.recipient_id=n.recipient_id "
                               "WHERE n.status='pending' AND r.active=1 ORDER BY r.recipient_id").fetchall()
    for recipient in recipients:
        items = pending(store, recipient["recipient_id"], settings.max_events_per_digest)
        if not items:
            continue
        subject, body, rich = render_digest(items, docket_info(store, {event.docket_id for event, _ in items}))
        ids = sorted(event.event_id for event, _ in items)
        key = hashlib.sha256(json.dumps(["digest-v1", recipient["recipient_id"], ids]).encode()).hexdigest()
        report["digests"] += 1
        if dry_run:
            report["previews"].append({"recipient_id": recipient["recipient_id"], "subject": subject, "items": len(items)})
            if preview_dir:
                Path(preview_dir).mkdir(parents=True, exist_ok=True)
                (Path(preview_dir) / f"digest_{key[:16]}.txt").write_text(subject + "\n\n" + body, encoding="utf-8")
                (Path(preview_dir) / f"digest_{key[:16]}.html").write_text(rich, encoding="utf-8")
            continue
        status = notifier.send(recipient["email"], subject, body, key, html=rich)
        with store.transaction():
            store.execute(f"UPDATE notifications SET status=?,digest_key=?,updated_at=? WHERE recipient_id=? "
                          f"AND status='pending' AND event_id IN ({','.join('?' * len(ids))})",
                          (status, key, utcnow(), recipient["recipient_id"], *ids))
        report[status] += 1
    return report
