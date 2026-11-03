# Complaints Without a PDF: How to Get Them and What It Costs

Date: 2026-10-30. Status: research and first implementation (screening on docket text, cost estimate). Nothing is bought.

## How many new complaints have no PDF

Measured on CourtListener for main complaints (document 1, no attachments) filed 2026-10-29 to 2026-10-30:

| | Complaints |
| --- | --- |
| All | 265 |
| PDF on CourtListener | 136 (51%) |
| No PDF | 129 (49%) |

These counts cover only what CourtListener received (mostly court RSS feeds and documents bought by RECAP users), not every federal filing.

## What is known without the PDF

| Field | Available | Example |
| --- | --- | --- |
| Case name, filing date | yes | Clay v. Discover Products Inc., 2026-10-30 |
| Docket text of the complaint entry | yes | "COMPLAINT against Discover Products Inc., Equifax ..., filed by Matthew Clay. (Attachments: # 1 Civil Cover Sheet ...)" |
| Court | from the filing fee receipt number (AFLMDC-… = M.D. Fla.) | FLMD |
| Attachments list | often | "Exhibit A - U.S. Patent No. …", "Claim Chart" |
| PACER document id | yes | used to buy the document |
| Page count | no | — |
| Nature of suit, cause of action, counsel | no (empty for new dockets) | — |

This is the "abstract" the system can screen on. It shows the parties and sometimes the subject (patents, claim charts); it rarely shows the technology.

## Ways to get the complaint

| Way | Cost | Notes |
| --- | --- | --- |
| Wait for a RECAP user to buy it | free | Uncertain; many complaints never get bought |
| Buy on PACER by hand | see below | Needs a PACER account |
| CourtListener RECAP Fetch API | PACER fees only (the API is free) | Buys with our PACER credentials and adds the document to CourtListener, where the existing pipeline reads it. Asynchronous. Accounts with multi-factor login are not supported; PACER passwords expire every 180 days |
| Other public websites | free | Plaintiff firms' press releases sometimes link the complaint; news covers large cases. Needs a web search API and checks that the page is about the same case (case number, parties). Not built yet |

## Cost

PACER charges $0.10 a page, at most $3.00 per document; each attachment counts as its own document. A quarter whose charges total $30 or less is not billed. Buying the main complaint only (no exhibits):

- New complaints with a PDF had a median of 10.5 pages (5 to 131); 15% had 30 pages or more. So a complaint usually costs $1 to $3. The page count is unknown before buying, so estimates use the $3 cap.
- Per expert: cost = complaints screened as possibly relevant × at most $3.

| Possibly relevant per day | Per day | Per quarter (about 90 days) |
| --- | --- | --- |
| 1 | ≤ $3 | ≤ $270 |
| 3 | ≤ $9 | ≤ $810 |

A purchase threshold (only "possibly relevant" complaints, a daily cap, a person approves each purchase) keeps this bounded.

## What the system does now

- Searches include complaints without a PDF.
- Screening judges them from the case name and docket text: relevant, not relevant, or unclear ("can't tell").
- Relevant ones are listed on the results and New cases by day pages as "Complaint not on CourtListener: possibly relevant", with the PACER cost (at most $3 each). They are not matched in full.

## Problem found: keyword search rarely finds complaints without a PDF

Search terms match the complaint text, which these complaints do not have; only their one-line docket text is searchable. In the first test (Matt Linton, filed since 2026-10-29) all 4 complaints found had a PDF.

Proposed fix: list all new complaints once a day (about 180 a day, 20 per request, so about 9 requests) and screen every docket line against each saved CV. The listing is shared by all experts, so its request cost does not grow with the number of CVs. It does not fit the local cap of 30 requests a day well, so it belongs in the scheduled cloud job.

## Decisions needed

1. Whether to buy complaints, and who approves each purchase.
2. A daily and quarterly spending cap.
3. A PACER account for the RECAP Fetch API (without multi-factor login).
4. Whether to try other websites first (needs a web search API key).

Sources: [PACER pricing](https://pacer.uscourts.gov/help/faqs/how-much-does-it-cost-access-documents-using-pacer), [PACER pricing FAQ](https://pacer.uscourts.gov/help/faqs/pricing), [CourtListener RECAP APIs (Fetch)](https://www.courtlistener.com/help/api/rest/v4/recap/), [Free Law Project: PACER Fetch API](https://free.law/2019/11/05/pacer-fetch-api/).
