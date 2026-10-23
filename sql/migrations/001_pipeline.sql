-- M1 pipeline schema. Portable across SQLite (tests) and PostgreSQL (cloud).
-- Statements are split on semicolons: never put a semicolon inside a statement or comment.
-- Legacy tables (dockets, entries, runs, changes, email_outbox) are created by Store and kept.

-- Per-docket incremental cursor: entries modified at or after cursor_at are fetched next.
CREATE TABLE IF NOT EXISTS docket_watch (
    docket_id BIGINT PRIMARY KEY,
    cursor_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    revision INTEGER NOT NULL DEFAULT 0
);

-- One row per detected content version. Status: detected, analyzed, routed, skipped, failed.
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    docket_id BIGINT NOT NULL,
    entry_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    before_json TEXT,
    source_modified_at TEXT,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS events_status_idx ON events (status, detected_at);

-- Analyzer output. Rules in M1, LLM providers later. Several analyzers may score one event.
CREATE TABLE IF NOT EXISTS analyses (
    event_id TEXT NOT NULL REFERENCES events (event_id),
    analyzer TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    importance TEXT NOT NULL,
    category TEXT,
    summary TEXT,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_id, analyzer, analyzer_version)
);

CREATE TABLE IF NOT EXISTS recipients (
    recipient_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- Watched dockets are exactly the dockets with an active subscription.
CREATE TABLE IF NOT EXISTS subscriptions (
    recipient_id TEXT NOT NULL REFERENCES recipients (recipient_id),
    docket_id BIGINT NOT NULL,
    min_importance TEXT NOT NULL DEFAULT 'medium',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (recipient_id, docket_id)
);

-- Per-recipient delivery state. Status: pending, sent, needs_review.
CREATE TABLE IF NOT EXISTS notifications (
    event_id TEXT NOT NULL REFERENCES events (event_id),
    recipient_id TEXT NOT NULL REFERENCES recipients (recipient_id),
    status TEXT NOT NULL,
    digest_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (event_id, recipient_id)
);

-- Shared request ledger and throttle pause for external APIs.
CREATE TABLE IF NOT EXISTS api_requests (
    request_id TEXT PRIMARY KEY,
    api TEXT NOT NULL,
    attempted_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS api_requests_idx ON api_requests (api, attempted_at);

CREATE TABLE IF NOT EXISTS api_control (
    api TEXT PRIMARY KEY,
    blocked_until DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    report_json TEXT
);
