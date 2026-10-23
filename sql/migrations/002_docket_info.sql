-- Case metadata shown in digests (name, number, court, page path). Fetched once per docket.
CREATE TABLE IF NOT EXISTS docket_info (
    docket_id BIGINT PRIMARY KEY,
    case_name TEXT,
    docket_number TEXT,
    court TEXT,
    absolute_url TEXT,
    fetched_at TEXT NOT NULL
);
