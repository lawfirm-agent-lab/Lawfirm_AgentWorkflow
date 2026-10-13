"""Deliver persisted changes independently of filing day and current fetch success."""
import hashlib
import json
from src.notify.digest import build_digest
from src.notify.email import deliver


def send_pending_changes(store, docket_id, cfg, transport=None):
    """Return True if delivery failed or a prior attempt needs manual review."""
    failed = False
    for change in store.list_changes(docket_id):
        digest = build_digest(docket_id, [json.loads(change['after_json'])], detected_change=True)
        if not digest:
            continue
        event = f"change:{change['id']}"
        key = hashlib.sha256(json.dumps([cfg['EMAIL_FROM'], cfg['EMAIL_TO'], event]).encode()).hexdigest()
        existing = store.execute('SELECT status FROM email_outbox WHERE key=?', (key,)).fetchone()
        if existing:
            if existing['status'] != 'accepted':
                failed = True
                print(f"Change {change['id']}: email needs manual review; not retried.")
            continue
        try:
            print(f"Change {change['id']}: {deliver(store, docket_id, digest, event, cfg, transport)}")
        except RuntimeError:
            failed = True
            print(f"Change {change['id']}: email failed or uncertain; inspect outbox before retrying.")
    return failed
