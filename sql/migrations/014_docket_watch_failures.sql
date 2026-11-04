-- Consecutive failed checks of a watched docket; a successful check resets it. CourtListener
-- often times out for a moment, so the run is only marked failed when the same docket keeps
-- failing (src/pipeline/detect.py, FAIL_AFTER).
ALTER TABLE docket_watch ADD COLUMN failures INTEGER NOT NULL DEFAULT 0;
