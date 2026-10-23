-- Court display names (e.g. S.D.N.Y.), fetched once per court and shared by its dockets.
CREATE TABLE IF NOT EXISTS court_info (
    court_id TEXT PRIMARY KEY,
    citation TEXT,
    full_name TEXT,
    fetched_at TEXT NOT NULL
);

-- Keep the raw court identifier on each docket so its display name can be looked up.
ALTER TABLE docket_info ADD COLUMN court_id TEXT;

-- Rows saved before this migration stored the upper-cased identifier as the display value.
UPDATE docket_info SET court_id = LOWER(court) WHERE court_id IS NULL AND court IS NOT NULL;
