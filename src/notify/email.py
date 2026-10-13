"""SMTP delivery with a persisted outbox; ambiguous attempts are not retried automatically."""
import hashlib
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid


def settings():
    names = ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO")
    values = {k: os.environ.get(k, "").strip() for k in names}
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise ValueError("Missing email settings: " + ", ".join(missing))
    for key in ("EMAIL_FROM", "EMAIL_TO"):
        value = values[key]
        if any(c in value for c in "\r\n,; ") or value.count("@") != 1:
            raise ValueError(key + " must be one plain email address")
    try:
        values["SMTP_PORT"] = int(values["SMTP_PORT"])
    except ValueError:
        raise ValueError("SMTP_PORT must be 465 or 587") from None
    if values["SMTP_PORT"] not in (465, 587):
        raise ValueError("Use TLS SMTP port 465 or STARTTLS port 587")
    return values


def deliver(store, docket_id, digest, event_key, cfg, transport=None):
    subject, body = digest
    key = hashlib.sha256(json.dumps([cfg["EMAIL_FROM"], cfg["EMAIL_TO"], event_key]).encode()).hexdigest()
    message_id = make_msgid()
    message = EmailMessage()
    message["From"], message["To"] = cfg["EMAIL_FROM"], cfg["EMAIL_TO"]
    message["Subject"], message["Message-ID"] = subject, message_id
    message.set_content(body)
    inserted = store.claim_email(key, docket_id, cfg["EMAIL_TO"], subject, body, message_id)
    if not inserted:
        status = store.email_status(key)
        return f"Skipped existing email ({status}); no duplicate send."
    try:
        if transport:
            transport(message)
        else:
            context = ssl.create_default_context()
            client = smtplib.SMTP_SSL(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=30, context=context) if cfg["SMTP_PORT"] == 465 else smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=30)
            with client:
                if cfg["SMTP_PORT"] == 587:
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                client.login(cfg["SMTP_USERNAME"], cfg["SMTP_PASSWORD"])
                refused = client.send_message(message)
                if refused:
                    raise RuntimeError("Recipient refused")
    except Exception as exc:
        # SMTP may have accepted a message before a connection error; do not retry blindly.
        store.finish_email(key, "uncertain", type(exc).__name__)
        raise RuntimeError("Email delivery failed or is uncertain; inspect email_outbox before retrying.") from None
    store.finish_email(key, "accepted")
    return "SMTP server accepted the email; inbox delivery is not yet confirmed."
