"""Build source-linked plain text digests from docket descriptions."""
from src.analyze.classifier import classify


def build_digest(docket_id, rows, demo=False, today=None, detected_change=False):
    selected = [(row, classify(row, today=today, historical=demo, detected_change=detected_change)) for row in rows]
    selected = [(row, finding) for row, finding in selected if finding["important"]]
    selected.sort(key=lambda item: (item[0].get("date_filed") or "", item[0]["id"]), reverse=True)
    if demo:
        selected = selected[:5]
    if not selected:
        return None
    subject = f"{'[DEMO historical snapshot] ' if demo else ''}Docket {docket_id}: {len(selected)} court-update candidates for review"
    lines = [subject, "", "Rule-based screening of docket descriptions only. PDFs have not been reviewed.",
             "Court origin and importance are inferred from description rules, not verified. Review the source before acting.",
             "These are historical records selected for demonstration, not newly detected filings." if demo else "These are detected changes; filing dates may be earlier than discovery. No AI analysis has been performed." if detected_change else "These are added/modified records filed today, using America/Los_Angeles as the reference date. Filing date is not necessarily the order issuance date.", ""]
    for row, finding in selected:
        lines += [f"Entry #{row.get('entry_number')} | Filed: {row.get('date_filed')} | ID: {row['id']}",
                  "Why selected: " + "; ".join(finding["reasons"]),
                  "Source description (verbatim):", row.get("description", ""),
                  f"Entry API: https://www.courtlistener.com/api/rest/v4/docket-entries/{row['id']}/",
                  f"Docket: https://www.courtlistener.com/docket/{docket_id}/", ""]
    return subject, "\n".join(lines)

