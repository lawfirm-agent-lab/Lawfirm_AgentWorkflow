# Cross-case notification integration plan

Date: 2026-10-22
Status: Event persistence implemented locally and tested; remaining integration is design only. Not deployed.

## Findings

The new collector stores latest metadata (ID, timestamps and docket/cluster reference). It does not persist an event history. Existing notification code reads legacy `changes` and relies on docket descriptions. Wiring it directly to discovery_records would silently produce no useful notifications. Opinion cluster references are not docket IDs and cannot be passed to the legacy docket renderer.

## Proposed implementation order

1. Persist versioned discovery events atomically with records/checkpoints. Use a unique event identity scoped by resource type, record ID and source version. Label first observation as first_seen, not necessarily a newly filed court document. Preserve immutable payload per event and explicit processing state.
2. Enrich pending events with the descriptions and source metadata required for rule screening. Detail requests must share the collector request ledger and account quota; never silently add unbudgeted requests. Existing pending cursors must retain their original field selection. Avoid changing selection mid-window.
3. Separate renderers for docket entries and opinions. Opinion relevance rules require an explicit policy; do not apply docket-description prefix rules blindly. Keep unclassified events pending or explicitly classified, never imply they were notified.
4. Add a dedicated dry-run preview mode before any SMTP enablement. Show first_seen/modified meaning, source links and rule basis, not AI conclusions.
5. Reuse transport only with distinct discovery event keys; ensure email outbox tables are explicitly provisioned. The DiscoveryStore initializer does not create legacy email tables. Accepted events skip delivery; uncertain delivery requires manual review.
6. Enable real delivery only after owner approval and end-to-end evidence. Existing hourly single-case workflow stays unchanged during this work.

## Acceptance cases

- Saving an event and advancing its page checkpoint succeeds or rolls back together.
- Replaying a page does not duplicate event or email; later source versions remain distinguishable.
- Failed enrichment retains the pending event and consumes the same request budget.
- Metadata-only changes do not masquerade as verified substantive legal changes.
- Docket and opinion source links use correct resource identities.
- Dry run invokes no SMTP transport. Accepted and uncertain delivery states are handled separately.
- Source changes during enrichment are explicitly represented rather than attributed to an older event version.

## Remaining decisions

Define relevant opinion categories and recipient scope before broad email delivery. No full-site completeness or zero-backlog promise is made.

## Implementation update

Step 1 now has local event persistence, atomicity and version-dedup tests. Source version identity includes the complete selected metadata hash. Existing pre-feature records are not backfilled. Steps 2–6 remain pending.
