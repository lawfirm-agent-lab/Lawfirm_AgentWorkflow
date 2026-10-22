# Discovery capability probe

## Work log — 2026-10-21

- Goal: assess cross-case incremental discovery before implementing ingestion.
- Added a manually dispatched GitHub Actions workflow and a metadata-only probe.
- Each run attempts at most three requests: GET api-usage, OPTIONS docket-entries, OPTIONS opinions.
- No retries or redirects; authentication failures and throttling stop the probe early.
- No local .env access, database access, email delivery, or paid PACER fetch requests.
- Existing scheduled monitoring and pre-existing README edits are unchanged.
- No other agents were started for this bounded task to conserve usage.

## Run instructions

After reviewing and pushing the files, select Actions > Probe CourtListener discovery > Run workflow.
The existing COURTLISTENER_TOKEN repository secret is required. Read the output of
"Probe capabilities without fetching case content". Avoid repeated runs near the hourly monitor,
as both use the same account allowance. Do not share tokens or raw credentials.

## Acceptance and limitations

- Local mocked tests verify the request cap, redirect prevention, throttle handling,
  missing-token behavior, and omission of raw error bodies.
- Capability probe cloud execution passed, based on owner-provided GitHub Actions output: all three responses were HTTP 200. Both endpoints advertised creation/modification filters and ordering. The usage snapshot reported 100 of 125 daily requests remaining before subsequent checks.
- OPTIONS metadata does not prove GET access, filter correctness, complete update detection,
  pagination stability, or manageable data volume. Those require a subsequent bounded test.
- Missing filter metadata is reported as unknown, not as confirmed support.
- These two candidate resources do not cover all CourtListener data types.


## Bounded incremental query probe — 2026-10-21

- Added a separate manual workflow: **Probe CourtListener incremental queries**.
- Queries a common 15-minute UTC window ending 10 minutes before execution.
- Uses modification-time bounds and timestamp/ID ordering, without a docket filter.
- Selects only identifiers, timestamps and docket/cluster references; no body output.
- Maximum two pages per resource and four requests overall, with no retries or redirects.
- Pagination must preserve endpoint, host, filters, ordering and selected fields.
- Validates timestamps, monotonic ordering and duplicate IDs across sampled pages.
- Reports partial_page_cap when results remain; an empty window is inconclusive.
- Distinct docket references can show cross-case retrieval. Opinion cluster counts do not establish distinct case counts.
- This is sampling, not production synchronization: no database writes, PDF retrieval or email.
- Local mocked validation covers request limits, unsafe pagination, throttling, invalid timestamps, valid records and missing credentials.
- Actual cloud query results remain pending owner deployment and execution.
- Run once and share only the statistics from **Probe incremental records without saving or emailing**.
- Existing monitor shares the account quota. A 429 stops the probe; avoid repeated runs.
