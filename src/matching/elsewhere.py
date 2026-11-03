"""Find a complaint that CourtListener has no PDF for on other websites (placeholder).

find_cases calls find(case) for every complaint that screening judged relevant but that has no
PDF on CourtListener, and stores the answer as the candidate's "elsewhere" (shown on the results
pages). Until the search agent is built, it finds nothing.

Planned (see docs/PDF_ACCESS.md): search the web with strong identifiers only (docket number,
parties, "complaint"), prefer law-firm press releases and court or class-action sites, download
a PDF, and keep it only if code finds the same docket number and parties in its text. Not found
means the complaint can still be bought from PACER (case["cost"]).

case holds: name, court, filed, url (CourtListener docket), docket_number (may be None),
description (the docket line), screen (the screening verdict).
Return None, or {"url": address of the complaint, "source": site name, "verified": bool}.
"""


def find(case):
    return None
