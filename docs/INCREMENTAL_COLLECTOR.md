# Incremental Metadata Collector — Work Log and Acceptance

Date: 2026-10-22 (America/Los_Angeles)
Status: Local implementation and unit validation completed; local PostgreSQL validation completed; independent review and cloud validation pending. Not committed or deployed.

## Scope

Manual collection of docket-entry and opinion metadata across cases. This is not full-site coverage or full-text ingestion. No email, OCR, PDF download, dotenv loading, or paid PACER requests. Existing scheduled monitoring and unrelated README changes remain unchanged.

## Implemented

- Separate discovery tables for latest metadata, stream checkpoints, request reservations and run reports.
- Explicit initialization at execution time. Reinitialization preserves existing progress; no historical backfill.
- Fixed 15-minute windows ending at least 10 minutes before collection time.
- At most four HTTP attempts per invocation, including one API usage query; no retries or redirects.
- Persistent rolling 24-hour collector budget of 72 attempts. External workflow traffic is not in this local ledger; live account allowance is checked conservatively, retaining daily headroom. Concurrent external usage can still cause throttling.
- At least 13 seconds between requests within a run; immediate runs can pause due to persisted pacing.
- HTTP 429 causes a conservative 24-hour collector pause.
- Pages and continuation checkpoints commit atomically. Failed pages leave previous progress intact.
- Optimistic checkpoint revisions reject stale updates. PostgreSQL CLI uses a session advisory lock.
- Least-serviced stream ordering prevents a busy stream from permanently starving the other.
- Metadata hash deduplication; no historical content-change event queue yet.
- English JSON run reports and GitHub step summaries report partial completion and lag.

## Validation

- Seven new SQLite/mocked tests passed: partial-page resume, atomic rollback, deduplication, invalid-page retention, budget reservation, stale-checkpoint conflict and idempotent initialization.
- Full local unit suite: 75 tests passed. HTTP and SMTP calls in relevant tests are mocked.
- Independent QA agent was interrupted by an account usage limit. Engineer agent was interrupted by revoked network permission; the primary agent completed the draft and tests.
- Corrected draft parsing of API usage from `limits` to the actual `current_usage` response field.
- No real CourtListener calls or Supabase writes were performed during implementation.
- Nine integration tests passed against a disposable local PostgreSQL 16 instance: the seven collector checks plus reconnect persistence/budget retention and session-lock exclusion. The temporary container was removed after testing. Supabase deployment and independent acceptance remain unverified.

## Deployment and manual verification (pending approval)

After review, commit and owner push: select **Collect incremental metadata manually**.
First run with initialize checked: creates discovery tables and missing stream checkpoints in Supabase, but makes no API requests. Wait at least 25 minutes before a collection run with initialize unchecked. Subsequent runs resume stored progress. These operations DO write the cloud database and require owner execution/authorization.

Inspect the JSON report: requests <= 4; partial/pause is not full synchronization success. Confirm saved records and checkpoints across separate runs. A job may complete successfully while reporting partial collection. Initialization creates only new discovery tables, not legacy snapshot tables.

## Remaining limitations

- Source records can change during pagination; no immutable upstream snapshot or late-arrival reconciliation is provided. Never claim lossless full-site coverage.
- Current metadata fields are ID, creation/modification timestamps and docket/cluster reference; bodies and document metadata need later enrichment.
- An invalid/expired cursor fails closed; automated cursor recovery is not yet implemented.
- First-page failures retry the same window on a later invocation. Hard process termination can leave a run marked running.
- The live quota reading is a snapshot, not a reservation across all applications.
- Sustained ingress above available request capacity creates backlog. Manual execution is intentionally not a production hourly replacement.

## PostgreSQL verification update

- Date: 2026-10-22. Primary agent executed tests/integration/discovery_checks.py with a cached PostgreSQL 16 image and synthetic localhost credentials.
- Result: 9/9 passed. No live API requests, SMTP calls, or Supabase connections.
- This verifies local PostgreSQL compatibility, not cloud permissions, connectivity, upstream cursor longevity, or independent QA approval.

## Cloud initialization failure investigation — 2026-10-22

- Owner reported generic initialization failure (exit 1). Root cause is not established; cloud initialization has NOT passed.
- Added safe stage diagnostics for configuration, database connection/schema creation, locking, and checkpoint initialization. No raw database error or secret is printed.
- Initialization no longer requires COURTLISTENER_TOKEN because it performs no API requests. Collection still requires it. This removes an unnecessary dependency but is not claimed to explain the observed failure.
- Three new diagnostic tests plus seven collector tests passed (10 total in this targeted run).
- Changes are local; deployment and rerun remain pending. No cloud resources were accessed during investigation.

## Work performed while awaiting the first closed window — 2026-10-22

- Owner-provided cloud output now confirms database/schema initialization and stream initialization succeeded. No API requests occurred. Actual collection and cross-run cloud resume remain pending.
- Reviewed notification compatibility: metadata-only discovery is not connected to legacy changes or description-based screening. Wrote NOTIFICATION_INTEGRATION_PLAN.md; no delivery code was enabled.
- Added four boundary tests: unsafe next-page URL leaves no page writes; daily headroom halts fetching; HTTP 429 persists a pause; unknown quota schema fails closed.
- Full local unit suite: 82 tests passed. No live API calls, database connections to Supabase, or emails were performed.
- Independent QA remains pending; these additional tests were authored and run by the primary agent.

## Versioned discovery events — 2026-10-22

- Added discovery_events with immutable metadata snapshots, resource-scoped version hashes, source modification time, observation time and pending_enrichment state.
- first_seen means first observation by this collector, not a new court filing. metadata_changed does not assert substantive legal change.
- Events, latest metadata and checkpoint updates share one transaction. Duplicate versions do not create duplicate events.
- No API fields were changed, no extra requests were added, and existing pending cursors remain compatible.
- Existing metadata already saved before this feature is not automatically backfilled into events; unchanged replays of such rows do not create events. This prevents unapproved historical notifications but requires an explicit migration decision if historical event recovery is desired.
- Validation: 87 local unit tests passed; 14 tests passed on disposable local PostgreSQL 16, including 5 event tests for deduplication, immutable versions, resource identity and rollback. Container removed afterward.
- Primary agent performed these checks; independent QA remains pending. No cloud writes, real API requests, or SMTP delivery occurred.
- Enrichment, screening, email rendering and delivery remain unimplemented for discovery events. No commit or deployment yet.
