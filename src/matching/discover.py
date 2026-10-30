"""Find new cases for an expert: CV profile -> keyword searches -> CourtListener -> screening.

1. The model writes up to 3 keyword searches from the expert profile (case_search_v1).
2. Each search is one CourtListener request (RECAP document search): main complaint documents
   (document 1) filed in the last `days`, with a PDF available, best matches first.
3. Each candidate's PDF is downloaded from storage.courtlistener.com (public, no token, not
   counted against the API budget) and its first pages are extracted.
4. The model screens all candidates in one call (case_screen_v1); relevant ones, up to `keep`,
   become cases for the normal profile and match steps. Rejected ones are kept with the reason.

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
SITE = "https://www.courtlistener.com"
STORAGE = "https://storage.courtlistener.com/"
FILEPATH = re.compile(r"recap/gov\.uscourts\.([a-z0-9]+)\.\d+/[A-Za-z0-9._-]+\.pdf")
DOCKET_URL = re.compile(r"/docket/(\d+)/\d+/([a-z0-9-]+)/")
TERMS = re.compile(r'[A-Za-z0-9 "\'&/.-]+')
CASE_NUMBER = re.compile(r"Case:?\s+(\d:\d{2}-[a-z]{2}-\d{3,6})", re.I)
MAX_PDF_BYTES = 40 * 1024 * 1024


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
    return f"document_number:1 AND description:complaint AND ({terms})"


def parse_screen(raw):
    data = load(raw)
    return {item.get("id"): {"relevant": item.get("relevant") is True,
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
    """A search result as a case dict, or None if it is not a usable main complaint."""
    path, url = row.get("filepath_local") or "", row.get("absolute_url") or ""
    court, link = FILEPATH.fullmatch(path), DOCKET_URL.fullmatch(url)
    if not (row.get("is_available") and court and link and row.get("attachment_number") is None):
        return None
    return {"docket_id": int(link[1]), "name": title(link[2]), "court": court[1].upper(),
            "filed": row.get("entry_date_filed"), "url": f"{SITE}/docket/{link[1]}/{link[2]}/",
            "document_label": "Complaint (ECF No. 1)", "document_url": STORAGE + path,
            "description": " ".join(str(row.get("description") or "").split())[:600],
            "page_count": row.get("page_count"), "filepath": path}


class CourtListenerSearch:
    def __init__(self, session, budget, cache_dir, days=365, today=date.today):
        self.session, self.budget, self.cache_dir, self.days, self.today = session, budget, cache_dir, days, today
        cache_dir.mkdir(parents=True, exist_ok=True)

    def search(self, terms):
        """Returns (cases, cached). One API request unless the same search ran today."""
        params = {"type": "rd", "q": query(terms), "order_by": "score desc", "available_only": "on",
                  "filed_after": (self.today() - timedelta(days=self.days)).isoformat()}
        key = hashlib.sha256(json.dumps([params, self.today().isoformat()]).encode()).hexdigest()
        path = self.cache_dir / f"search-{key}.json"
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
            cached = True
        else:
            if not self.budget.acquire():
                raise DiscoveryError("CourtListener request budget for local runs is used up for now")
            response = self.session.get(SEARCH, params=params, timeout=(10, 30), allow_redirects=False)
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

    def download(self, case, pdf_dir):
        """The complaint PDF from public storage (no token sent), cached by path."""
        pdf_dir.mkdir(parents=True, exist_ok=True)
        target = pdf_dir / case["filepath"].replace("/", "_")
        if target.exists():
            return target
        response = self.session.get(STORAGE + case["filepath"], timeout=(10, 60), allow_redirects=False,
                                    headers={"Authorization": None}, stream=True)
        if response.status_code != 200:
            raise DiscoveryError(f"Download returned HTTP {response.status_code}")
        body = b""
        for chunk in response.iter_content(1024 * 256):
            body += chunk
            if len(body) > MAX_PDF_BYTES:
                raise DiscoveryError("PDF is too large")
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
