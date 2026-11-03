-- Expert-case matching in the cloud: saved CVs and finished daily checks (src/matching/cloud.py).
-- The scheduled job reads the CVs and earlier checks, and writes each new check; the local web
-- app copies the checks back to show them. A CV is stored as base64 text so both backends agree.
CREATE TABLE IF NOT EXISTS matching_cvs (
    name TEXT PRIMARY KEY,
    pdf_base64 TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    saved_at TEXT NOT NULL
);

-- status_json, result_json and trace_json are the run folder's files (src/web/jobs.py).
CREATE TABLE IF NOT EXISTS matching_runs (
    run_id TEXT PRIMARY KEY,
    cv_name TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status_json TEXT NOT NULL,
    result_json TEXT,
    trace_json TEXT,
    saved_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS matching_runs_cv_idx ON matching_runs (cv_name, started_at);
