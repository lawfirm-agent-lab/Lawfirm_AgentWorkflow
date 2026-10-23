"""Email notifier on top of the persisted outbox: at most one attempt per key."""
from email.message import EmailMessage
from email.utils import make_msgid

from src.notify.email import smtp_send


class EmailNotifier:
    name = "email"

    def __init__(self, store, cfg, transport=None):
        self.store, self.cfg = store, cfg
        self.transport = transport or (lambda message: smtp_send(cfg, message))

    def send(self, recipient, subject, body, key):
        message_id = make_msgid()
        if not self.store.claim_email(key, None, recipient, subject, body, message_id):
            # Already attempted: never resend. Only a confirmed acceptance counts as sent.
            return "sent" if self.store.email_status(key) == "accepted" else "needs_review"
        message = EmailMessage()
        message["From"], message["To"] = self.cfg["EMAIL_FROM"], recipient
        message["Subject"], message["Message-ID"] = subject, message_id
        message.set_content(body)
        try:
            self.transport(message)
        except Exception as exc:
            # The server may have accepted the message before failing; do not retry blindly.
            self.store.finish_email(key, "uncertain", type(exc).__name__)
            return "needs_review"
        self.store.finish_email(key, "accepted")
        return "sent"
