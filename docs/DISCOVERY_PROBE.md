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
- Cloud execution is pending owner deployment and manual dispatch.
- OPTIONS metadata does not prove GET access, filter correctness, complete update detection,
  pagination stability, or manageable data volume. Those require a subsequent bounded test.
- Missing filter metadata is reported as unknown, not as confirmed support.
- These two candidate resources do not cover all CourtListener data types.
