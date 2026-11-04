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
│   ├── CourtListener RECAP search: main complaints (document 1, no attachments) filed in
│   │   the window (last year, or 1/3/7 days for daily checks), with or without a PDF,
│   │   best matches first — 1 API request per search
│   ├── download each candidate's complaint that has a PDF from public storage (no token, not counted)
│   └── model screens all candidates (case_screen_v1): on their first pages, or on the case name
│       and docket text when there is no PDF; relevant / not relevant / unclear.
│       Relevant ones with a PDF are kept, at most 3 of one case type first, up to 6.
│       Relevant ones without a PDF go to the search agent (up to 3 per CV): a docket request
│       for the docket number, 2 web searches (Tavily), the model picks likely results, code
│       verifies them (both parties + docket number or filing year). A verified complaint PDF is
│       read like the others; a verified web page alone is read but capped at Medium; otherwise
│       the case is listed with the PACER cost (see PDF_ACCESS.md)
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

# Daily check for every saved CV (or --cv NAME), same runs as the web app's "Check new cases"
.venv/bin/python -m src.web.daily --dotenv --days 1

# Cloud (see "Daily checks in the cloud"): save a CV there, list, copy the cloud checks here
.venv/bin/python -m src.matching.cloud --dotenv upload data/matching/cvs/matt_linton_CV.pdf
.venv/bin/python -m src.matching.cloud --dotenv list
.venv/bin/python -m src.web --dotenv --cloud    # the web app, showing the cloud checks too
```

### Daily checks

The goal is to see, each day, the new cases relevant to an expert. A daily check is a normal run with three differences:

- It only searches complaints filed in the last N days (1, 3 or 7; `filed_after` = today − N).
- Cases that earlier daily checks of the same CV already found are left out of the search results, so each case is shown once.
- The fixed case pool is not included.

Uploaded CVs can be saved to `data/matching/cvs/` ("Save this CV for daily checks"); daily checks run for saved CVs. The **New cases by day** page (`/today`) lists the checks by the day they ran: complaints found, screened out, and the cases read and matched with their tier and score. "Filed" is the court filing date; "Checked" is when the system found the case.

Daily checks can be started by hand (web button or the command above) or run on a schedule in the cloud (next section).

First real check (2026-10-30, Mark C. Nicely, filed since 2026-10-29): 3 searches → 4 complaints with a PDF → none relevant (two generic e-commerce patent suits, a genetic-testing patent case, a county ordinance case). Second check (Matt Linton, same window, searches now include complaints without a PDF): 4 complaints → 2 read in full, both Medium (Google LLC v. Does 1–8, credential phishing; Quectel v. Telit, remote access to IoT modules). All 4 had a PDF: keyword search rarely finds complaints without one, because only their one-line docket text is searchable. See [PDF_ACCESS.md](PDF_ACCESS.md) for the measured share, ways to get them, cost and the proposed daily listing.

### Daily checks in the cloud

The cloud and this computer share one database: the pipeline's Supabase PostgreSQL (`DATABASE_URL`). Migration [005_matching.sql](../sql/migrations/005_matching.sql) adds two tables, created by the next pipeline or matching run that opens the database:

- `matching_cvs`: CVs saved for daily checks (the PDF as base64).
- `matching_runs`: finished daily checks, each the run folder's `status.json`, `result.json` and `trace.json`.

The job [daily-matching.yml](../.github/workflows/daily-matching.yml) runs `python -m src.web.daily --cloud --days 2` every day at 11:30 UTC (and by hand with a chosen window). Supabase starts it: pg_cron calls `matching_start_daily()` ([013_matching_daily_schedule.postgres.sql](../sql/migrations/013_matching_daily_schedule.postgres.sql)), which asks GitHub to run the workflow through pg_net with the token in Vault, because GitHub's own schedule can run hours late (the first scheduled run, due 11:30 UTC on 2026-11-03, ran at 18:36). GitHub's schedule remains as a backup at 17:00 UTC with `--skip-checked-within 20`, so it only checks CVs the main run missed. It copies the saved CVs and earlier checks from the database, runs each CV's daily check, and saves every check back. Earlier checks decide which cases are left out as already shown, so a 2-day window covers a skipped scheduled run without showing a case twice. Its CourtListener requests count against the pipeline's ledger in the database (`api_requests`), so the docket monitor and the matching job together stay under the account's limits (100 a day after headroom; at most 6 per CV).

This computer connects with `--cloud` ([src/matching/cloud.py](../src/matching/cloud.py)):

- `python -m src.web --dotenv --cloud`: the **New cases by day** page first copies the cloud checks it does not have yet. A CV saved with "Save this CV for daily checks" is also saved to the cloud, and a daily check run here is saved there too (with the local 30-a-day ledger).
- `python -m src.matching.cloud upload|list|pull`: save a CV for the scheduled job, list saved CVs and recent checks, copy the cloud checks.

The job needs these repository settings: secrets `DATABASE_URL`, `COURTLISTENER_TOKEN`, `LLM_API_KEY`; variables `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL` (and optionally `LLM_EXTRA_BODY`). These are the docket pipeline's LLM settings. To give matching its own model, set the variable `MATCHING_LLM_MODEL`: the job then uses `MATCHING_LLM_PROVIDER`, `MATCHING_LLM_BASE_URL`, `MATCHING_LLM_EXTRA_BODY` and the secret `MATCHING_LLM_API_KEY` instead (for OpenRouter: `openai_compatible`, `https://openrouter.ai/api/v1`, e.g. `deepseek/deepseek-v4.1-flash`, no extra body). With no CV saved in the cloud, the job fails with "No saved CV in the cloud database". The LLM cache is not kept between cloud runs, so each daily check calls the model for everything it reads.

### Results website (phone)

[site/](../site/) is a read-only website on GitHub Pages (https://ctrterry.github.io/Lawfirm_AgentWorkflow/): "New cases by day" per expert, and for each case why it fits, the case summary, the match reasons with their CV and complaint quotes, and an editable email draft that opens in Gmail or the phone's mail app. It is plain HTML and JavaScript and can be added to a phone's home screen.

**One link per person.** Each link's label names its holder (`new-key --label advisor`); that person sees and manages only the CVs they uploaded and the results made from them, and a file name someone else already uses gets a number (`cv.pdf` → `cv-2.pdf`), so nobody's CV is replaced. An admin link (`new-key --label terry --admin`) sees and manages everything. CVs, runs and uploads record their owner (migration 011); those made before, or saved from the command line or the local web app, are the admin's. The functions in [012_matching_owner_functions.postgres.sql](../sql/migrations/012_matching_owner_functions.postgres.sql) filter by the caller, and `matching_whoami` names the holder in the page header. Each person may upload 10 CVs a day.

Access is by link: `https://…/#key=…`. The page sends the key only to two database functions ([007_matching_feed.postgres.sql](../sql/migrations/007_matching_feed.postgres.sql)), `matching_feed` and `matching_run`, which return data only when the SHA-256 of the key matches an active row in `matching_access_keys`. The tables stay closed to Supabase's API roles (RLS without policies); the functions run as their owner. The page itself holds only the Supabase URL and the publishable key, which alone read nothing.

```
.venv/bin/python -m src.matching.cloud --dotenv new-key --label advisor   # prints the link once
.venv/bin/python -m src.matching.cloud --dotenv keys                      # labels and state, never keys
.venv/bin/python -m src.matching.cloud --dotenv revoke-key advisor        # the link stops working
```

**Guide** (website, top bar): what the system does, getting started (personal link, adding it to the home screen), uploading, when and where new cases appear (times shown in the reader's time zone), how to read tiers, scores and match reasons, contacting lawyers, managing CVs, and limits. There are no email or phone alerts yet; the guide says so.

**Upload a CV** (website): the CV goes to `matching_submit` ([009_matching_submit.postgres.sql](../sql/migrations/009_matching_submit.postgres.sql)), which checks the key, that the file is a PDF of at most 10 MB and that fewer than 10 uploads were made in the last day, saves the CV for daily checks when asked, and queues a request (`matching_requests`, migration 008). It then asks GitHub to run [matching-request.yml](../.github/workflows/matching-request.yml) for that request, through pg_net (an HTTP call the database makes after the transaction commits) with a GitHub token kept in Supabase Vault. The job runs `python -m src.web.request --id …`: complaints filed in the last year, no fixed case pool, the same steps and ledger as a daily check. It reports each step to the request, saves the run like a daily check, and clears the request's copy of the CV when it ends, done or failed (a saved CV keeps its own copy in `matching_cvs`; migration 015 cleared copies left by earlier uploads). The progress page reads `matching_request` every 10 seconds and opens the results when the run is done; the list shows these runs under "From uploaded CVs". A job that is cancelled or times out marks its request failed.

The token: a fine-grained GitHub personal access token for this repository only, with the permission Actions: read and write. Put it in `.env` as `GITHUB_DISPATCH_TOKEN` and run `python -m src.matching.cloud --dotenv set-dispatch-token` (stores it in Vault as `matching_github_token`; never printed). Without it an upload stays waiting and says why.

**My CVs** (website page, linked in the top bar once a CV is saved, and commands): a saved CV can be stopped (no more daily checks; its past results stay; it can be resumed) or deleted (the CV, every daily check and upload run made from it, and its finished upload requests; copies already on someone's computer in `data/matching/runs` are not touched). Database functions `matching_cvs`, `matching_cv_active` and `matching_cv_delete` ([010_matching_cv_admin.postgres.sql](../sql/migrations/010_matching_cv_admin.postgres.sql)) check the access key like the others; on the command line: `list`, `stop-cv`, `resume-cv`, `delete-cv`. Each person can do this only for their own CVs; an admin link for any.

**Search agent** ([src/matching/elsewhere.py](../src/matching/elsewhere.py), prompt `elsewhere_pick_v1`): complaints judged relevant but without a PDF on CourtListener, at most 3 per CV and run. It runs only when `TAVILY_API_KEY` is set (`.env` locally, a GitHub secret in the cloud); without it the run is unchanged.

1. One CourtListener docket request adds the docket number and court (counted in the same budget).
2. Two Tavily searches with strong identifiers only: `"<defendant>" <plaintiff> complaint <year>` and `"<docket number>" complaint`. Only public case names and numbers are sent; never the CV.
3. The model sees the titles, addresses and snippets as untrusted data and picks the results about this very case, labelled complaint, press release, news or other.
4. Code fetches up to 3 picks (complaint PDFs first) from public addresses only (no private or local hosts, every redirect checked, 10 MB, PDF or HTML). A result is kept only if its text names both parties and has the docket number, or failing that the filing year. A PDF must also say "complaint". A verified page is searched for links to the complaint PDF, which are checked the same way.
5. A verified complaint PDF joins the cases read in full, labelled "Complaint (from ftc.gov)". A verified web page alone is read the same way (at least 800 characters) but labelled as not the complaint, and its match is never above Medium. Otherwise nothing changes: the case stays listed with the PACER cost, and the website says the web was searched.

Searches and downloads are cached for the day. The model only ranks results; it cannot make a result count without the code check. Measured on 2026-11-04 with two real cases: *FTC v. Digital Forensics Corporation LLC* (N.D. Ohio 1:26-cv-02615, no PDF on CourtListener) found the complaint PDF on ftc.gov, verified by docket number and parties, then read in full (49 pages) and matched Medium to Matt Linton's CV. *Koester v. Palo Alto Networks* (N.D. Tex., an employment case) found nothing about the case, and nothing was kept. Cost: 4 Tavily searches, 2 CourtListener requests and 2 small model calls (about 5,000 tokens).

Email drafts list every lawyer named in the complaint with a checkbox. Their addresses are found by code, not by the model ([profiles.py](../src/matching/profiles.py) `add_emails`): every address in the complaint is listed, and a lawyer gets the one whose user name contains their surname, preferring one that starts with their first initial, because signature blocks often list the addresses apart from the names. Lawyers for the party the draft is written for are checked; addresses no lawyer claims are offered unchecked under "Other addresses found in the complaint"; the user can add more. "Open in Gmail" opens a new message in the user's own Gmail with the chosen recipients, subject and text (Gmail adds their signature); "Open in Mail app" does the same with a `mailto:` link; nothing is sent automatically. On five complaints checked by hand, all 14 lawyers got the right address. Checks saved before this got their addresses with `python -m src.matching.cloud --dotenv add-emails` (re-reads the complaint PDFs from public storage; no model call).

[pages.yml](../.github/workflows/pages.yml) publishes `site/` on every push to main that changes it, writing `config.js` from the repository variables `SUPABASE_URL` and `SUPABASE_ANON_KEY` (the publishable key; the job refuses a secret key). The manifest has no `start_url`, so a home-screen icon opens the link it was added from, key included.

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
