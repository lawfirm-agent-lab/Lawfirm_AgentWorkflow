-- On-demand runs started from the results website (src/web/request.py, migration 009).
-- state: queued (waiting for GitHub Actions), running, done (run_id set) or failed (error set).
-- pdf_base64 holds the uploaded CV until the run ends, done or failed; it is then cleared (a CV
-- saved for daily checks has its own copy in matching_cvs). Migration 015 clears older copies.
CREATE TABLE IF NOT EXISTS matching_requests (
    request_id TEXT PRIMARY KEY,
    cv_name TEXT NOT NULL,
    pdf_base64 TEXT,
    save INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    run_id TEXT,
    progress TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
