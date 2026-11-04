"""Find new cases for an expert: CV profile -> keyword searches -> CourtListener -> screening.

1. The model writes up to 3 keyword searches from the expert profile (case_search_v1).
2. Each search is one CourtListener request (RECAP document search): main complaint documents
   (document 1, no attachments) filed in the last `days`, best matches first. About half of new
   complaints have no PDF on CourtListener yet; for those only the case name and the docket
   description are known.
3. Each candidate with a PDF is downloaded from storage.courtlistener.com (public, no token, not
   counted against the API budget) and its first pages are extracted.
4. The model screens all candidates in one call (case_screen_v1): relevant, not relevant, or
   unclear. Relevant ones with a PDF, up to `keep`, become cases for the normal profile and match
   steps. Relevant ones without a PDF are listed as worth getting, with the PACER cost of buying
   the complaint (nothing is bought). Rejected ones are kept with the reason.

API requests go through RequestBudget with a local ledger, capped well below the account's
daily limit because the cloud pipeline shares it. Search results are cached for the day.
"""
import hashlib
import json
import re
import subprocess
from datetime import date, timedelta

from src.ingest.pdf import tool
from src.llm.output import InvalidOutput, text
from src.matching.profiles import DOMAINS, clean, load, objects

SEARCH = "https://www.courtlistener.com/api/rest/v4/search/"
DOCKET = "https://www.courtlistener.com/api/rest/v4/dockets/{}/"
SITE = "https://www.courtlistener.com"
STORAGE = "https://storage.courtlistener.com/"
FILEPATH = re.compile(r"recap/gov\.uscourts\.([a-z0-9]+)\.\d+/[A-Za-z0-9._-]+\.pdf")
DOCKET_URL = re.compile(r"/docket/(\d+)/\d+/([a-z0-9-]+)/")
TERMS = re.compile(r'[A-Za-z0-9 "\'&/.-]+')
CASE_NUMBER = re.compile(r"Case:?\s+(\d:\d{2}-[a-z]{2}-\d{3,6})", re.I)
# Filing fee receipts name the court: "receipt number AWAWDC-10110117" is W.D. Wash. (WAWD).
RECEIPT = re.compile(r"\bA([A-Z]{2,4})C-\d{5,}")
MAX_PDF_BYTES = 40 * 1024 * 1024
# PACER: $0.10 a page, at most $3.00 a document; no bill when a quarter's total is $30 or less.
PAGE_FEE, DOCUMENT_CAP, FREE_PER_QUARTER = 0.10, 3.00, 30.00


class DiscoveryError(RuntimeError):
    """Search or download failure. Messages never include tokens or response bodies."""


def parse_searches(raw):
    data, searches = load(raw), []
    for item in objects(data, "searches", 3):
        terms = " ".join(re.sub(r"\b(or|and)\b", lambda m: m.group(1).upper(), str(item.get("terms") or "")).split())
        # The terms go into a query string: allow only plain words, phrases and OR.
        if not terms or len(terms) > 300 or not TERMS.fullmatch(terms) or terms.count('"') % 2:
            continue
        searches.append({"label": text(item.get("label"), 60, "label"), "terms": terms})
    if not searches:
        raise InvalidOutput("no usable search")
    return searches


def query(terms):
    return f"document_number:1 AND description:complaint AND NOT attachment_number:[1 TO 99999] AND ({terms})"


def pacer_cost(pages):
    """What buying one document on PACER costs; the cap when the page count is unknown."""
    return round(min(PAGE_FEE * pages, DOCUMENT_CAP), 2) if pages else DOCUMENT_CAP


def parse_screen(raw):
    data = load(raw)
    return {item.get("id"): {"relevant": item.get("relevant") is True, "unclear": item.get("relevant") == "unclear",
                             "domains": [d for d in (item.get("domains") or [])[:2] if d in DOMAINS],
                             "case_type": text(item.get("case_type"), 80, "case_type", required=False),
                             "reason": text(item.get("reason"), 200, "reason")}
            for item in objects(data, "candidates", 60) if isinstance(item.get("id"), str)}


def diverse(cases, keep, per_type=3):
    """Up to keep cases in search order, at most per_type of one case type first, so a run of
    near-identical class actions does not crowd out other kinds of cases; then fill the rest."""
    kept, counts = [], {}
    for case in cases:
        kind = ((case.get("screen") or {}).get("case_type") or "").lower()
        if len(kept) < keep and counts.get(kind, 0) < per_type:
            kept.append(case)
            counts[kind] = counts.get(kind, 0) + 1
    return kept + [c for c in cases if c not in kept][:keep - len(kept)]


def title(slug):
    small = {"v", "of", "and", "the", "for", "in", "on", "at", "a", "an", "llc", "inc", "co", "corp", "ltd", "lp", "et", "al"}
    words = slug.split("-")
    out = []
    for i, word in enumerate(words):
        if word in ("llc", "inc", "corp", "ltd", "lp", "co"):
            out.append(word.upper() if word in ("llc", "lp") else word.capitalize() + ".")
        elif word == "v":
            out.append("v.")
        elif i and word in small:
            out.append(word)
        else:
            out.append(word.capitalize())
    return " ".join(out)


def candidate(row):
    """A search result as a case dict, or None if it is not a main complaint. has_pdf: the
    complaint is on CourtListener; otherwise only the docket description is known."""
    path, url = row.get("filepath_local") or "", row.get("absolute_url") or ""
    court, link = FILEPATH.fullmatch(path), DOCKET_URL.fullmatch(url)
    if not link or row.get("attachment_number") is not None:
        return None
    description = " ".join(str(row.get("description") or "").split())[:600]
    has_pdf = bool(row.get("is_available") and court)
    receipt = RECEIPT.search(description)
    return {"docket_id": int(link[1]), "name": title(link[2]),
            "court": court[1].upper() if court else receipt[1] if receipt else "",
            "filed": row.get("entry_date_filed"), "url": f"{SITE}/docket/{link[1]}/{link[2]}/",
            "document_label": "Complaint (ECF No. 1)", "document_url": STORAGE + path if has_pdf else None,
            "description": description, "page_count": row.get("page_count"), "filepath": path if has_pdf else None,
            "has_pdf": has_pdf, "pacer_doc_id": row.get("pacer_doc_id")}


class CourtListenerSearch:
    def __init__(self, session, budget, cache_dir, days=365, today=date.today):
        self.session, self.budget, self.cache_dir, self.days, self.today = session, budget, cache_dir, days, today
        cache_dir.mkdir(parents=True, exist_ok=True)

    def since(self):
        """First filing date searched (inclusive)."""
        return self.today() - timedelta(days=self.days)

    def search(self, terms):
        """Returns (cases, cached). One API request unless the same search ran today."""
        params = {"type": "rd", "q": query(terms), "order_by": "score desc",
                  "filed_after": self.since().isoformat()}
        key = hashlib.sha256(json.dumps([params, self.today().isoformat()]).encode()).hexdigest()
        path = self.cache_dir / f"search-{key}.json"
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
            cached = True
        else:
            if not self.budget.acquire():
                raise DiscoveryError("CourtListener request budget is used up for now")
            try:
                response = self.session.get(SEARCH, params=params, timeout=(10, 30), allow_redirects=False)
            except OSError as exc:  # requests' timeouts and connection errors
                raise DiscoveryError(f"CourtListener search failed: {type(exc).__name__}") from None
            if response.status_code == 429:
                self.budget.block()
                raise DiscoveryError("CourtListener returned HTTP 429; requests paused")
            if response.status_code != 200:
                raise DiscoveryError(f"CourtListener search returned HTTP {response.status_code}")
            try:
                rows = response.json()["results"]
            except (ValueError, KeyError, TypeError):
                raise DiscoveryError("Unexpected search response") from None
            path.write_text(json.dumps(rows), encoding="utf-8")
            cached = False
        return [c for c in (candidate(r) for r in rows if isinstance(r, dict)) if c], cached

    def docket(self, docket_id):
        """{docket_number, court} of a docket: one API request, cached. For the search agent."""
        path = self.cache_dir / f"docket-{int(docket_id)}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if not self.budget.acquire():
            raise DiscoveryError("CourtListener request budget is used up for now")
        try:
            response = self.session.get(DOCKET.format(int(docket_id)), timeout=(10, 30), allow_redirects=False)
        except OSError as exc:
            raise DiscoveryError(f"CourtListener docket request failed: {type(exc).__name__}") from None
        if response.status_code == 429:
            self.budget.block()
            raise DiscoveryError("CourtListener returned HTTP 429; requests paused")
        if response.status_code != 200:
            raise DiscoveryError(f"CourtListener docket request returned HTTP {response.status_code}")
        try:
            data = response.json()
            number, court = data.get("docket_number"), data.get("court_id")
        except (ValueError, AttributeError):
            raise DiscoveryError("Unexpected docket response") from None
        info = {"docket_number": number if isinstance(number, str) and len(number) < 60 else None,
                "court": court.upper() if isinstance(court, str) and court.isalnum() and len(court) < 16 else None}
        path.write_text(json.dumps(info), encoding="utf-8")
        return info

    def download(self, case, pdf_dir):
        """The complaint PDF from public storage (no token sent), cached by path."""
        pdf_dir.mkdir(parents=True, exist_ok=True)
        target = pdf_dir / case["filepath"].replace("/", "_")
        if target.exists():
            return target
        try:
            response = self.session.get(STORAGE + case["filepath"], timeout=(10, 60), allow_redirects=False,
                                        headers={"Authorization": None}, stream=True)
            if response.status_code != 200:
                raise DiscoveryError(f"Download returned HTTP {response.status_code}")
            body = b""
            for chunk in response.iter_content(1024 * 256):
                body += chunk
                if len(body) > MAX_PDF_BYTES:
                    raise DiscoveryError("PDF is too large")
        except OSError as exc:
            raise DiscoveryError(f"Download failed: {type(exc).__name__}") from None
        if not body.startswith(b"%PDF-"):
            raise DiscoveryError("Download is not a PDF")
        target.write_bytes(body)
        return target


def first_pages(pdf, pages=3, limit=3000):
    """Text of the first pages, for screening only (no OCR: scanned complaints show as empty)."""
    try:
        out = subprocess.run([tool("pdftotext"), "-f", "1", "-l", str(pages), "-enc", "UTF-8", str(pdf), "-"],
                             check=True, capture_output=True, text=True, errors="replace", timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(clean([out])[0].split())[:limit]


def case_number(excerpt):
    match = CASE_NUMBER.search(excerpt)
    return match[1] if match else None
