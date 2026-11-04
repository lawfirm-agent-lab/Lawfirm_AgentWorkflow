-- Feedback on matched cases from the results website (functions in migration 017).
-- kind "rating": a person's verdict on a case, up or down, with an optional reason, the level it
-- should have had, whether they would contact the lawyers, and a note. A new rating replaces the
-- person's earlier one on the same case (current 0), which is kept as history.
-- kind "email": the person opened the email draft in Gmail or their mail app, or copied it.
-- snapshot_json is what the system decided when the feedback was given (level, score, source,
-- model, prompt version...), so it still means something after the run is deleted or re-run.
CREATE TABLE IF NOT EXISTS matching_feedback (
    feedback_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    run_id TEXT NOT NULL,
    case_index INTEGER NOT NULL,
    case_url TEXT,
    case_name TEXT,
    cv_name TEXT,
    kind TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT,
    should_be TEXT,
    would_contact INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    snapshot_json TEXT,
    current INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS matching_feedback_case_idx ON matching_feedback (run_id, case_index, owner);

CREATE INDEX IF NOT EXISTS matching_feedback_time_idx ON matching_feedback (created_at);
