-- One row per LLM attempt or cache reuse: the audit trail and the token budget ledger.
-- Status: ok, cached, invalid, failed. response_text is the raw model output for audits.
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id TEXT PRIMARY KEY,
    event_id TEXT,
    input_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    response_text TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS llm_calls_cache_idx ON llm_calls (input_hash, model, prompt_version, status);

CREATE INDEX IF NOT EXISTS llm_calls_day_idx ON llm_calls (created_at);
