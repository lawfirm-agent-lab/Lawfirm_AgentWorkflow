# Docket Agent

Docket Agent monitors U.S. federal court dockets on [CourtListener](https://www.courtlistener.com/), detects new and changed docket entries, screens them, and emails a digest to the people subscribed to each case.

Court records change often, and lawyers cannot watch every case all day. The goal is a reliable, auditable alert: what changed, why it was selected, and a link to the source.

> **Status: Milestone 1 (pipeline skeleton) is in progress.** Screening is rule-based. LLM analysis (DeepSeek first, but any provider) arrives in Milestone 2. See [docs/M1.md](docs/M1.md) for scope and acceptance criteria and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how to extend the system.

## How it works

```
CourtListener ──► Detect ──► Analyze ──► Route ──► Deliver ──► email digest
                    │           │           │          │
                    └───────────┴───── PostgreSQL (Supabase) ─────┘
                       events state machine, cursors, outbox, audit
```

| Stage | What it does |
| --- | --- |
| **Detect** | For each subscribed docket, asks CourtListener only for entries modified since the stored cursor. An unchanged docket costs one request. New or changed content becomes an *event*. |
| **Analyze** | Scores each event (`high`, `medium`, `low`, `ignore`). M1 uses description rules that look for court orders, judgments and court notices. |
| **Route** | Matches events to subscribers by docket and minimum importance. |
| **Deliver** | Sends one digest per recipient per run (HTML with a plain-text fallback). Court orders come first, grouped by case name and number, with the verbatim description and links to the entry and its PDF. Other activity follows on one line each. |

Every stage reads and writes the database only, so a stage can fail, be rerun, or later be replaced (for example by an LLM-backed agent) without touching the others.

## Quick start (local)

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Create .env only if it does not exist; never overwrite private local settings.
if [ ! -e .env ]; then cp .env.example .env; fi
python -m unittest discover -s tests -v
```

Try the pipeline against a throwaway SQLite file without network or email:

```bash
python -m src.admin --sqlite data/dev.db add-recipient you@example.com
python -m src.admin --sqlite data/dev.db subscribe you@example.com 73132799
python -m src.pipeline.runner --sqlite data/dev.db --stages analyze,route,deliver --dry-run
```

Dry-run previews (`.txt` and `.html`) are written to `data/previews/`. The `data/` directory is git-ignored.

## Cloud operation (GitHub Actions + Supabase)

### 1. Secrets

Add these in **Settings → Secrets and variables → Actions**. Never commit real values.

| Name | Kind | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | Secret | Supabase PostgreSQL URI (session pooler). TLS is enforced. |
| `COURTLISTENER_TOKEN` | Secret | CourtListener API token. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | Secrets | Outgoing mail. Port 465 (TLS) or 587 (STARTTLS). |
| `EMAIL_FROM` | Secret | Sender address. |
| `EMAIL_TO` | Secret | Legacy workflow only. The pipeline reads recipients from the database. |
| `ANALYZER` | Variable (optional) | Overrides `pipeline.analyzer` in `config.yaml`. |
| `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL` / `LLM_API_KEY` | Variables / Secret | Milestone 2. See [LLM providers](#llm-providers). |

### 2. Recipients and subscriptions

Recipient addresses live only in the database and never in the repository or in logs. Manage them from your machine with `DATABASE_URL` in your local `.env`:

```bash
python -m src.admin --dotenv add-recipient lawyer@example.com --name "A. Lawyer"
python -m src.admin --dotenv subscribe lawyer@example.com 73132799 --min-importance medium
python -m src.admin --dotenv list
python -m src.admin --dotenv unsubscribe lawyer@example.com 73132799
python -m src.admin --dotenv deactivate-recipient lawyer@example.com
```

`--min-importance` defaults to `medium`. With the M1 rules analyzer this means court-origin entries only. Use `low` to receive every change.

A newly watched docket starts **now**, so its history is never sent. A docket that the legacy workflow already checked resumes from that workflow's last successful run.

### 3. Run

**Actions → Run docket pipeline** runs every two hours (`17 */2 * * *`) and can also be started manually. Tick **dry_run** to render digests without sending. The first run creates the new tables automatically through versioned migrations in [sql/migrations/](sql/migrations/).

Each run prints a JSON report and adds it to the job summary: requests used, events detected, analyzed and routed, and digests sent. The job fails (red) when a stage fails or a delivery needs manual review.

### Workflows

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `run-pipeline.yml` | Every 2 h + manual | The M1 pipeline. |
| `tests.yml` | Push / PR / manual | Offline unit tests plus PostgreSQL 16 integration tests. |
| `check-dockets.yml` | Manual | **Legacy** single-docket check. Do not run it alongside the pipeline (see below). |
| `check-database.yml` | Manual | Read-only connection and TLS check. |
| `check-storage.yml` | Manual | Write, reconnect, read, then clean up a probe table. |
| `init-database.yml` | Manual | Creates the legacy tables. The pipeline creates its own tables on first run. |
| `collect-incremental.yml` | Manual | Cross-case metadata collector. **Paused** during M1 to save API quota. |
| `probe-discovery.yml`, `probe-incremental.yml` | Manual | API capability probes. |

GitHub may delay or skip scheduled runs. In practice runs arrive every few hours, so alerts are not real-time.

## Configuration

[config.yaml](config.yaml) holds non-secret settings:

- `pipeline.analyzer`: `rules` (M1) or `llm` (M2).
- `pipeline.max_events_per_digest`: the largest number of items in one email. Anything beyond waits for the next run.
- `pipeline.source_lag_seconds`: skip the most recent minutes, which may still be changing.
- `courtlistener.*`: request budget. CourtListener allows **5/minute, 50/hour and 125/day** per account on rolling windows. The pipeline keeps a persistent request ledger, waits at least 13 s between requests, caps each run, and keeps headroom for manual tools. An HTTP 429 pauses all requests for 24 hours.

## LLM providers

The model is configuration and never code. Any OpenAI-compatible chat API works through one adapter. That covers DeepSeek, OpenAI, Qwen, Kimi and OpenRouter, plus local Ollama or vLLM:

```bash
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.deepseek.com      # or https://api.openai.com/v1, http://localhost:11434/v1 ...
LLM_MODEL=<model name as the provider spells it>
LLM_API_KEY=<secret>                        # optional only for localhost
```

To add a provider with its own API, register a new adapter in [src/llm/client.py](src/llm/client.py). The LLM analyzer itself is Milestone 2. Selecting `ANALYZER=llm` today validates these settings and then stops with a clear message.

**Data residency:** M1 only handles public court records. Before sending any client-confidential material to a model, check the provider's data handling terms. DeepSeek, for example, states that it stores data in mainland China. Pick the provider per deployment.

## Security

- Secrets come from environment variables only. Cloud jobs never read `.env`. Logs never print tokens, connection strings, recipient addresses or raw provider or driver errors.
- Database connections require TLS and use statement and lock timeouts.
- **Row Level Security** is enabled on every application table in `public`. Supabase's Data API roles (`anon`, `authenticated`) therefore see nothing, while the owning application role keeps full access. The check reruns at every pipeline start, so tables added later are covered too.
- CourtListener pagination is only followed on the same host, path and filters. Redirects are refused, so the API token is never forwarded elsewhere.
- Email uses a persisted outbox. Each digest is attempted at most once. If delivery is uncertain, the digest is marked `needs_review` and never resent automatically.
- LLM output, when added, will be treated as untrusted: structured JSON only, no tools, and claims checked against the source text.

## Known limitations

- CourtListener only has what reaches RECAP. Entries can be delayed or missing, so the digest is not a complete or real-time record.
- Only docket-entry descriptions are screened. PDFs are not read (Milestone 3).
- Detection compares content (description, dates, numbers, document metadata). Re-indexing alone does not alert. Entries that disappear upstream are kept, not reported.
- If a run is killed mid-way, it can stay marked `running` in `pipeline_runs`.
- The legacy workflow and the pipeline share the `entries` table. A legacy run can absorb a change before the pipeline records it, so keep the legacy workflow disabled.

## Other tools in this repository

- `python -m src.main`: the legacy single-docket check (local snapshot, `--import-baseline`, `--email-demo`, `--send-email`, `--cloud-email`). It is superseded by the pipeline and kept for reference.
- `python -m src.ingest.pdf file.pdf --output-dir data/ocr`: local PDF text extraction with a per-page Tesseract OCR fallback. It needs Poppler and Tesseract (`brew install poppler tesseract`). Output is page-numbered JSON and text. OCR text is not verified evidence. This tool is planned for Milestone 3.
- `python -m src.ingest.collect_incremental`: the cross-case metadata collector. See [docs/INCREMENTAL_COLLECTOR.md](docs/INCREMENTAL_COLLECTOR.md).

## Testing

```bash
python -m unittest discover -s tests -v                    # offline; no network, SMTP or .env
TEST_POSTGRES_PORT=<port> python tests/integration/pipeline_checks.py
```

Integration scripts connect only to a disposable localhost database named `lawfirm_test` (user `postgres`, password `local-test-only`) and **truncate its tables**. Never point them at real data. CI runs them against a PostgreSQL 16 service container.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): stages, data model, extension points
- [docs/M1.md](docs/M1.md): Milestone 1 scope, status and deployment checklist
- [docs/NOTIFICATION_INTEGRATION_PLAN.md](docs/NOTIFICATION_INTEGRATION_PLAN.md), [docs/INCREMENTAL_COLLECTOR.md](docs/INCREMENTAL_COLLECTOR.md), [docs/DISCOVERY_PROBE.md](docs/DISCOVERY_PROBE.md): cross-case collector history
- CourtListener API: <https://wiki.free.law/c/courtlistener/help/api/rest/v4/rest-api-v47>
