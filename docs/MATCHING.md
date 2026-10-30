# Expert–Case Matching (prototype)

Started: 2026-10-30. Branch: `expert-matching`.

## Goal

The user is a technical expert, not a lawyer. Given the expert's CV, the system finds recent U.S. federal cases where the expert's background may be relevant, explains each case in plain English, shows which CV experience matches which case issue (with quotes and pages from both documents), ranks the cases, and drafts an email from the expert to the case's lawyers. The expert reviews and sends the email; nothing is sent automatically.

## Pipeline

```
Expert CV (PDF)
├── 1. Read CV → experience items E1, E2 … (kind: role, project, patent, publication,
│      testimony, certification, education, skill, award), each with a verified quote and page
├── 2. Find cases (optional, --find-cases or the web checkbox)
│   ├── model writes up to 3 keyword searches from the CV            (case_search_v1)
│   ├── CourtListener RECAP search: complaints (document 1) filed in the last year,
│   │   PDF available, best matches first — 1 API request per search
│   ├── download each candidate's complaint from public storage (no token, not counted)
│   └── model screens all candidates on their first pages (case_screen_v1);
│       relevant ones are kept, at most 3 of one case type first, up to 6
├── 3. Understand each case (fixed pool + found) → plain-English profile (case_profile_v1):
│      parties, case type, summary, technology, issues I1…, expertise areas X1…
│      ("stated" by the document or "inferred"), counsel. Quotes verified.
│      No readable document → "Insufficient information"
├── 4. Match (case_match_v1): tier + reasons E↔I/X (direct or related) + gaps + email draft
│      Code: drops reasons citing unknown or non-technical points; high needs a verified
│      direct reason, medium at least one reason; no email below medium; flags emails
│      claiming the case needs an expert when the document does not say so
├── 5. Categorize: group by tier (High, Medium, Low, Insufficient information,
│      Not relevant collapsed); tags and filters by technical domain and case type
└── 6. Rank within a tier by a score shown on the page:
       evidence (2 per direct + 1 per related reason) + 1 if the matched expertise is stated
       in the document + share of technical issues + recency (1 if filed ≤ 90 days, 0.5 ≤ 1 year)
```

Why tiers plus a transparent score instead of a model score: model scores vary between runs and do not explain themselves. Tiers are checked against verified evidence, and every part of the score is visible.

## Verification

Every experience item, issue, expertise area and counsel entry must quote its source. Code keeps a claim only if the quote is on the cited page (or found once elsewhere, with the page corrected), as in M3 ([src/llm/document.py](../src/llm/document.py)). Bullet glyphs from symbol fonts are removed before the model sees the text, so quotes match. Reasons can only reference verified ids, so every quote shown in the report is real. Summaries, reasons and emails are AI reading and are labelled as such.

## Running it

```
# Web app (127.0.0.1 only): pick or upload a CV, follow progress, read results
.venv/bin/python -m src.web --dotenv            # http://localhost:8000

# Command line
.venv/bin/python -m src.matching.run --dotenv --cv data/matching/cvs/Mark_C_Nicely.ExpertWitness.pdf \
    --cases eval/matching_cases.json --find-cases
```

- Sample CVs: put PDFs in `data/matching/cvs/` (the two public CVs used so far are there locally).
- Fixed case pool: [eval/matching_cases.json](../eval/matching_cases.json) (saved CourtListener entries or a local PDF per case).
- Everything generated stays under `data/matching/` (ignored by git): runs, LLM cache, search cache, downloaded PDFs, the request ledger.

## CourtListener usage

Local runs keep their own request ledger (`data/matching/courtlistener.db`) through `RequestBudget`, capped at 30 requests a day and 6 per run, because the cloud pipeline uses the same account (125 a day). A search repeated on the same day is served from the cache. PDF downloads from `storage.courtlistener.com` go without the API token.

## First results (2026-10-30, deepseek-flash)

| CV | Found | Read in full | Result |
| --- | --- | --- | --- |
| Mark C. Nicely (casino game math, 200+ gaming patents, prior expert witness) | 47 complaints from 3 searches → 24 screened → 14 relevant | 6 + DraftKings | High: social / sweepstakes casino class actions (RNG, odds, virtual coins). Medium: Interactive Games v. DraftKings and v. FanDuel (mobile gambling patents: location, root detection), Light & Wonder v. Twin States (slot game copying), Tamaris v. Rogonov (live casino feed used for cheating) |
| Matt Linton (incident response, digital forensics, Google/NASA) | 41 complaints → 24 screened → 15 relevant | 6 + DraftKings | High: data breach class actions (detection, dwell time, safeguards). Medium: DraftKings (root/jailbreak detection), Magnet Forensics v. Del Gaudio. Low: two FedEx supply-chain cases |

Screening rejected cases that only mention a keyword (ship repair liens, criminal cases, constitutional challenges, wage claims).

## Known limits and next steps

- Match quality has not been measured. Reasons are sometimes padded to the maximum of 6, and some "related" pairs are weak. Next: a small labeled set (expert × case → tier) reviewed by a person.
- Only the complaint is read. Later filings (motions to dismiss, scheduling orders) would add the defense's counsel and the case stage (whether expert discovery has started).
- Case stage is approximated by the filing date.
- Scanned complaints without a text layer are screened on an empty excerpt (OCR is used only for the full read).
- Lawyer contact details come from the complaint's signature block only.
