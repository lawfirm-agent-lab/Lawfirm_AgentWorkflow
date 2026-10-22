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
