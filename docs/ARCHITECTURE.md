# Architecture

## Principles

1. **Stages communicate through the database, not through each other.** Each stage picks up rows in one state and moves them to the next. A stage can crash, be rerun, or be replaced without touching the rest.
2. **Every external dependency sits behind an interface** ([src/core/interfaces.py](../src/core/interfaces.py)): `Source`, `Analyzer`, `Notifier`, `LLMClient`. New providers are new classes, not edits to the pipeline.
3. **Nothing is guessed silently.** Invalid upstream data stops that docket and is recorded. Uncertain email delivery is flagged for review, never retried blindly. A cursor only moves forward in the same transaction that records the events.
4. **Auditable over clever.** Analyzer name and version are stored with every result. Digests quote the source verbatim and link to it.

## Pipeline

```
                      ┌──────────── src/pipeline/runner.py ─────────────┐
 Source (interface)   │  detect  ─►  analyze  ─►  route  ─►  deliver     │  Notifier (interface)
 CourtListenerSource ─┤                 ▲                                ├─ EmailNotifier
 (src/sources)        │           Analyzer (interface)                   │  (src/notifiers)
                      │           RulesAnalyzer │ LLM analyzer (M2)      │
                      └──────────────────────────────────────────────────┘
                                         │
                         PipelineStore (SQLite for tests, PostgreSQL in cloud)
```

| Stage | Module | Reads | Writes |
| --- | --- | --- | --- |
| detect | [src/pipeline/detect.py](../src/pipeline/detect.py) | `subscriptions`, `docket_watch`, `entries` | `events` (`detected`), `entries`, `docket_watch` (one transaction per docket); `docket_info` once per docket from leftover budget |
| analyze | [src/pipeline/analyze.py](../src/pipeline/analyze.py) | `events` (`detected`) | `analyses`, `events` → `analyzed`, or `failed` after 3 attempts |
| route | [src/pipeline/route.py](../src/pipeline/route.py) | `events` (`analyzed`), latest `analyses`, `subscriptions` | `notifications` (`pending`), `events` → `routed` / `skipped` |
| deliver | [src/pipeline/deliver.py](../src/pipeline/deliver.py) | `notifications` (`pending`) | `email_outbox`, `notifications` → `sent` / `needs_review`; rendering in [src/notify/render.py](../src/notify/render.py) |

The runner executes the selected stages in order. A failing stage is reported, and later stages still run, so pending work keeps draining while CourtListener is down. Each run is recorded in `pipeline_runs` with its JSON report.

### Event identity

`event_id = sha256(source, docket_id, entry_id, previous content signature, new content signature)`

- Replaying the same page creates nothing new.
- A change followed by a revert (A → B → A) produces three events, because each transition is distinct.
- The content signature (`src.store.db.content`) covers description, filing date and time, entry number and document metadata. Changes to `date_modified` alone are ignored.
- `kind` is `added` (first seen by this system) or `modified`. "Added" does not prove the filing is new at the court.

### Cursor rules

- A new watch starts at the last legacy success for that docket, if one exists, otherwise at *now*. No history is backfilled.
- The query window is `[cursor, now − source_lag_seconds)`, ordered by `date_modified,id`.
- If the page cap or the request budget stops a docket mid-way, the cursor moves to the last timestamp read. Rows at that timestamp are fetched again next time and deduplicated.
- An optimistic `revision` check rejects concurrent updates. PostgreSQL runs also take a session advisory lock, so only one pipeline runs at a time.

## Data model

Created by [sql/migrations/](../sql/migrations/) on first connection, applied once each and tracked in `schema_migrations`:

| Table | Purpose |
| --- | --- |
| `docket_watch` | Cursor, last check, last success and last error per docket |
| `events` | Content versions and their state: `detected → analyzed → routed / skipped / failed` |
| `analyses` | Output of each analyzer and version, stored as importance, category, summary and full JSON |
| `recipients`, `subscriptions` | Who receives which docket, with a minimum importance |
| `notifications` | Per-recipient delivery state: `pending → sent / needs_review` |
| `api_requests`, `api_control` | Persistent request ledger and throttle pause, per API |
| `pipeline_runs` | Run history and reports |
| `docket_info` | Case name, number, court and page path for digests, fetched once per docket |

The legacy tables (`dockets`, `entries`, `runs`, `changes`, `email_outbox`) are kept. `entries` is the comparison baseline. `email_outbox` provides once-only delivery.

**Adding a migration:** create `sql/migrations/002_<name>.sql`. Statements are split on `;`, so do not use semicolons inside statements or comments. Write SQL that works in both SQLite and PostgreSQL: use `TEXT`, `BIGINT` and `DOUBLE PRECISION`, and `INTEGER` 0/1 for booleans. Never edit a migration that has already been applied.

## Extension points

### New data source
Implement `Source.fetch_docket_changes(docket_id, since, until) -> SourceResult` and `fetch_docket_info(docket_id)` (see [src/sources/courtlistener.py](../src/sources/courtlistener.py)). Draw every request from a `RequestBudget` with its own `api` name. Raise on invalid data rather than skipping it. Webhooks (M4) will write to the same `events` table.

### New analyzer
Implement `Analyzer.analyze(event) -> Analysis` and register it in [src/analyzers/registry.py](../src/analyzers/registry.py). Return the shared `Analysis` shape (`importance`, `category`, `summary`, `reasons`, `details`), and bump `version` whenever behaviour changes. Raise on failure: the stage records only the exception type and retries up to three times.

### New LLM provider
OpenAI-compatible providers need configuration only (`LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`). For any other API, add a class with `complete(messages, json_mode=False) -> str` and decorate it with `@register("<name>")` in [src/llm/client.py](../src/llm/client.py). Error messages must never contain keys, prompts or response bodies.

### New notification channel
Implement `Notifier.send(recipient, subject, body, key, html=None)` and return `sent` or `needs_review`. Deliver at most once per `key`; the email notifier uses `email_outbox` for this.

## Toward agents

Each stage already has the shape of an agent's job: claim rows in one state, do one task, write the result and the next state. When a task needs autonomy (for example, "read the order, find the deadline, cite the page"), that stage's implementation can become an agent behind the same interface. The rest of the pipeline does not change.

The planned Milestone 2 design for the LLM analyzer is a deliberate middle step: one model call per event, strict JSON output, no tools, quoted source evidence that the code verifies, and a fallback to rules when verification fails.
