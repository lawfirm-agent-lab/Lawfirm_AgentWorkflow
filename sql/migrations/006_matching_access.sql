-- Access keys for the results website (site/). Only the SHA-256 of each key is stored; the key
-- itself is shown once when it is made (python -m src.matching.cloud new-key) and lives in the link.
CREATE TABLE IF NOT EXISTS matching_access_keys (
    key_hash TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
