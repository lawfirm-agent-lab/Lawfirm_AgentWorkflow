"""Search agent: find a complaint that CourtListener has no PDF for on other websites.

find_cases calls Finder.find(case) for complaints with no PDF on CourtListener that screening
judged relevant, then for those it could not judge ("unclear"), at most MAX_CASES per CV. An
unclear one with a verified copy is screened again on it. For each:

0. One CourtListener docket request adds the docket number and court (counted in its budget).
1. A web search (Tavily) with strong identifiers only: the parties, "complaint", the docket number.
2. The model picks the results likely to be this very case (elsewhere_pick_v1). Results are
   untrusted data; the model only ranks them.
3. Code fetches the picks and keeps one only if its text names both parties and either carries
   the docket number or the filing year. A page that passes is also searched for links to the
   complaint PDF, which are checked the same way.
4. A verified complaint PDF is read and matched like any other case, labelled with its source. A
   verified web page only (a press release, an article) is read the same way but labelled as not
   the complaint, and its match is never above Medium. Nothing verified: the PACER cost stays.

Fetching is limited to public http(s) addresses, 10 MB, PDF or HTML, and at most 3 redirects,
each checked. Results and files are cached by day and address. Nothing here sends the CV.
"""
import hashlib
import ipaddress
import json
import re
import socket
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from src.llm.output import InvalidOutput, text
from src.matching import discover
from src.matching.profiles import choice, load, objects

TAVILY = "https://api.tavily.com/search"
MAX_CASES = 4  # cases searched per CV and run (relevant ones first, then unclear ones)
MAX_PICKS = 3  # results fetched per case
MAX_LINKS = 2  # complaint links followed from a verified page
MAX_BYTES = 10 * 1024 * 1024
KINDS = ("complaint", "press release", "news", "other")
# Words that do not tell one party from another.
COMMON = {"llc", "inc", "corp", "corporation", "company", "co", "ltd", "lp", "llp", "plc", "the", "and", "of", "et", "al",
          "united", "states", "america", "usa", "group", "holdings", "international", "services", "doe", "does", "john",
          "jane", "people", "state", "county", "city", "plaintiff", "plaintiffs", "defendant", "defendants", "individually",
          "behalf", "others", "similarly", "situated", "all"}
NUMBER = re.compile(r"\b(?:\d:)?(\d{2})[-‐–]?([a-z]{2,4})[-‐–]?0*(\d{1,6})\b", re.I)


class ElsewhereError(RuntimeError):
    """Search or fetch failure. Messages never include keys or response bodies."""


def parties(name):
    """Distinctive words of each side of 'A v. B', lower case; a side may have none."""
    sides = re.split(r"\s+v\.?\s+", name or "", maxsplit=1)
    words = lambda side: [w for w in re.findall(r"[a-z0-9]+", side.lower()) if len(w) > 2 and w not in COMMON]
    return [words(s) for s in sides] if len(sides) == 2 else [words(sides[0])]


def number_key(value):
    """'1:26-cv-01234-ABC' -> ('26', 'cv', '1234'); None when it is not a federal docket number."""
    found = NUMBER.search(value or "")
    return (found[1], found[2].lower(), found[3]) if found else None


def verify(body, case):
    """How a fetched text ties to the case: {"ok", "by"}. Both parties must be named (a side with
    no distinctive word is skipped, but one side must be checkable), and the docket number or the
    filing year must appear."""
    lower = body.lower()
    named = [side for side in parties(case["name"]) if side]
    if not named:
        return {"ok": False, "by": "the case name has no distinctive word"}
    for side in named:
        hits = sum(1 for w in side if re.search(rf"\b{re.escape(w)}\b", lower))
        if hits < min(2, len(side)):
            return {"ok": False, "by": f"does not name {' '.join(side)}"}
    key = number_key(case.get("docket_number"))
    if key and any((m[1], m[2].lower(), m[3]) == key for m in NUMBER.finditer(body)):
        return {"ok": True, "by": "docket number and parties"}
    year = (case.get("filed") or "")[:4]
    if year and year in body:
        return {"ok": True, "by": "parties and filing year"}
    return {"ok": False, "by": "neither the docket number nor the filing year"}


class _Page(HTMLParser):
    """Visible text and links of an HTML page."""

    def __init__(self):
        super().__init__()
        self.text, self.links, self.skip, self.href = [], [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        elif tag == "a":
            self.href, self.anchor = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.href:
            self.links.append((self.href, " ".join(" ".join(self.anchor).split())))
            self.href = None

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)
            if self.href:
                self.anchor.append(data)


def html_page(raw):
    page = _Page()
    page.feed(raw.decode("utf-8", errors="replace"))
    return " ".join(" ".join(page.text).split()), page.links


def public(url):
    """Raise unless url is http(s) on a host that resolves only to public addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ElsewhereError("Not a public web address")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except (OSError, UnicodeError):
        raise ElsewhereError("Host not found") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise ElsewhereError("Not a public web address")


def parse_picks(raw):
    data = load(raw)
    picks = []
    for item in objects(data, "picks", 10):
        if isinstance(item.get("id"), str) and item.get("same_case") is True:
            picks.append({"id": item["id"], "kind": choice(item.get("kind"), KINDS, "kind"),
                          "reason": text(item.get("reason"), 200, "reason")})
    return picks


class WebSearch:
    """Tavily search and safe downloads, cached under cache_dir."""

    def __init__(self, session, key, cache_dir, today=date.today):
        self.session, self.key, self.cache_dir, self.today = session, key, cache_dir, today
        self.calls = 0  # Tavily requests made (cached answers not counted)
        cache_dir.mkdir(parents=True, exist_ok=True)

    def search(self, query, results=5):
        """Returns ([{title, url, snippet}], cached). One API call unless the query ran today."""
        name = hashlib.sha256(json.dumps([query, results, self.today().isoformat()]).encode()).hexdigest()
        path = self.cache_dir / f"web-{name}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")), True
        self.calls += 1
        try:
            response = self.session.post(TAVILY, json={"query": query, "max_results": results, "search_depth": "basic"},
                                         headers={"Authorization": f"Bearer {self.key}"}, timeout=(10, 30),
                                         allow_redirects=False)
        except OSError as exc:
            raise ElsewhereError(f"Web search failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise ElsewhereError(f"Web search returned HTTP {response.status_code}")
        try:
            rows = [{"title": " ".join(str(r.get("title") or "").split())[:200], "url": r["url"],
                     "snippet": " ".join(str(r.get("content") or "").split())[:500]}
                    for r in response.json()["results"] if isinstance(r, dict) and isinstance(r.get("url"), str)]
        except (ValueError, KeyError, TypeError):
            raise ElsewhereError("Unexpected web search response") from None
        path.write_text(json.dumps(rows), encoding="utf-8")
        return rows, False

    def fetch(self, url):
        """(kind, body, final url): kind "pdf" or "html". Public addresses only, at most 10 MB."""
        name = hashlib.sha256(url.encode()).hexdigest()
        for kind in ("pdf", "html"):
            path = self.cache_dir / f"file-{name}.{kind}"
            if path.exists():
                return kind, path.read_bytes(), url
        current = url
        for _ in range(4):
            public(current)
            try:
                response = self.session.get(current, timeout=(10, 30), allow_redirects=False, stream=True,
                                            headers={"User-Agent": "Mozilla/5.0 (compatible; ExpertMatch/1.0; research)",
                                                     "Accept": "application/pdf,text/html;q=0.9"})
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                    current = urljoin(current, response.headers["Location"])
                    continue
                if response.status_code != 200:
                    raise ElsewhereError(f"HTTP {response.status_code}")
                body = b""
                for chunk in response.iter_content(1024 * 256):
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise ElsewhereError("File is larger than 10 MB")
            except OSError as exc:
                raise ElsewhereError(f"Download failed: {type(exc).__name__}") from None
            kind = "pdf" if body.startswith(b"%PDF-") else "html" if "html" in response.headers.get("Content-Type", "") else None
            if kind is None:
                raise ElsewhereError("Neither a PDF nor a web page")
            (self.cache_dir / f"file-{name}.{kind}").write_bytes(body)
            return kind, body, current
        raise ElsewhereError("Too many redirects")


class Finder:
    """The search agent for one run. courtlistener: the run's CourtListenerSearch (docket numbers)."""

    def __init__(self, model, web, courtlistener, log=print):
        self.model, self.web, self.courtlistener, self.log = model, web, courtlistener, log

    def queries(self, case):
        sides = re.split(r"\s+v\.?\s+", case["name"], maxsplit=1)
        defendant = sides[-1]
        plaintiff = sides[0] if len(sides) == 2 else ""
        out = [f'"{defendant}" {plaintiff} complaint {(case.get("filed") or "")[:4]}'.strip()]
        if case.get("docket_number"):
            out.append(f'"{case["docket_number"]}" complaint')
        return [" ".join(q.split()) for q in out]

    def find(self, case, pdf_dir):
        """The answer stored as case["elsewhere"]; sets case["web_pdf"] or case["web_text"] when
        something verified is found, so the run reads it."""
        if case.get("docket_id") and not case.get("docket_number"):
            try:
                info = self.courtlistener.docket(case["docket_id"])
                case["docket_number"] = info.get("docket_number")
                case["court"] = case.get("court") or info.get("court")
            except discover.DiscoveryError:
                pass  # Search without it; verification then needs the filing year.
        results, queries = [], self.queries(case)
        for q in queries:
            try:
                rows, cached = self.web.search(q)
            except ElsewhereError as exc:
                self.model.record("web_search", case["name"], query=q, error=str(exc))
                continue
            self.model.record("web_search", case["name"], query=q, cached=cached, results=rows)
            results += [r for r in rows if r["url"] not in {x["url"] for x in results}]
        answer = {"queries": queries, "results": len(results), "url": None, "verified": False}
        if not results:
            return {**answer, "note": "The web search found nothing."}
        listing = [{"id": f"R{n}", **r} for n, r in enumerate(results, 1)]
        case_view = {k: case.get(k) for k in ("name", "court", "docket_number", "filed", "description")}
        try:
            picks = self.model.ask("elsewhere_pick_v1", f"<case>\n{json.dumps(case_view, ensure_ascii=False)}\n</case>\n"
                                   f"<results>\n{json.dumps(listing, ensure_ascii=False, indent=1)}\n</results>",
                                   parse_picks, 1500, label=case["name"])
        except InvalidOutput:
            picks = []
        by_id = {r["id"]: r for r in listing}
        # Complaint PDFs first, then press releases, then the rest, in the model's order otherwise.
        picks = sorted([p for p in picks if p["id"] in by_id], key=lambda p: KINDS.index(p["kind"]))[:MAX_PICKS]
        tried, page_found = [], None
        for pick in picks:
            url = by_id[pick["id"]]["url"]
            found = self.check(url, case, pdf_dir, tried)
            if found and found["kind"] == "pdf":
                return {**answer, **found["answer"]}
            if found and page_found is None:
                page_found = found
                for link in found["links"][:MAX_LINKS]:
                    linked = self.check(link, case, pdf_dir, tried)
                    if linked and linked["kind"] == "pdf":
                        return {**answer, **linked["answer"], "via": found["answer"]["url"]}
        if page_found:
            pdf_dir.mkdir(parents=True, exist_ok=True)
            path = pdf_dir / f"web-{hashlib.sha256(page_found['answer']['url'].encode()).hexdigest()[:16]}.txt"
            path.write_text(page_found["text"], encoding="utf-8")
            case["web_text"] = str(path)
            return {**answer, **page_found["answer"]}
        return {**answer, "note": "No result could be verified as this case.", "tried": tried}

    def check(self, url, case, pdf_dir, tried):
        """Fetch and verify one address; None when it fails or does not match the case."""
        if url in [t["url"] for t in tried]:
            return None
        try:
            public(url)
            kind, body, final = self.web.fetch(url)
        except ElsewhereError as exc:
            tried.append({"url": url, "result": str(exc)})
            return None
        source = (urlsplit(final).hostname or "").removeprefix("www.")
        if kind == "pdf":
            pdf_dir.mkdir(parents=True, exist_ok=True)
            path = pdf_dir / f"web-{hashlib.sha256(final.encode()).hexdigest()[:16]}.pdf"
            path.write_bytes(body)
            content = discover.first_pages(path, pages=5, limit=30000)
            links = []
        else:
            content, raw_links = html_page(body)
            links = [urljoin(final, href) for href, label in raw_links
                     if ".pdf" in href.lower() and "complaint" in (href + " " + label).lower()]
        check = verify(content, case)
        tried.append({"url": final, "result": f"{'verified' if check['ok'] else 'rejected'}: {check['by']}"})
        if not check["ok"]:
            return None
        if kind == "pdf":
            if "complaint" not in content.lower():
                tried[-1]["result"] = "rejected: not a complaint"
                return None
            case["web_pdf"] = str(path)
        answer = {"url": final, "source": source, "verified": True, "matched_by": check["by"],
                  "kind": "complaint" if kind == "pdf" else "web page"}
        return {"kind": kind, "answer": answer, "links": list(dict.fromkeys(links)), "text": content}
