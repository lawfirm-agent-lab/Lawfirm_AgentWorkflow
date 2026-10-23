"""Per-docket incremental reads of CourtListener docket entries; never fetches PDFs.

One query per docket asks only for entries modified since the stored cursor, so an
unchanged docket costs a single request instead of a full snapshot.
"""
import re
from datetime import timezone
from urllib.parse import parse_qs, urlsplit

from src.core.models import SourceResult
from src.ingest.probe_incremental import timestamp

HOST = "www.courtlistener.com"
PATH = "/api/rest/v4/docket-entries/"
DOCKETS = "https://" + HOST + "/api/rest/v4/dockets/{}/"
COURTS = "https://" + HOST + "/api/rest/v4/courts/{}/"
COURT_ID = re.compile(r"[a-z0-9]{1,15}")


class SourceError(RuntimeError):
    """Request or validation failure. Messages never include tokens or response bodies."""


class QuotaExhausted(SourceError):
    """The request budget is spent or paused; retry on a later run."""


def utc(value):
    return timestamp(value).astimezone(timezone.utc).isoformat()


def clean(data, key):
    value = data.get(key)
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else None


def check_next(url, params):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != HOST or parsed.path != PATH or parsed.fragment:
        raise SourceError("Unsafe pagination URL")
    query = parse_qs(parsed.query)
    for key, value in params.items():
        if query.get(key) != [str(value)]:
            raise SourceError("Pagination changed the query")


class CourtListenerSource:
    name = "courtlistener"

    def __init__(self, session, budget, max_pages=5):
        self.session, self.budget, self.max_pages = session, budget, max_pages

    def fetch_docket_info(self, docket_id):
        """Case name, number, court and page path for display. One request per docket."""
        data = self._get(DOCKETS.format(docket_id), None, envelope=False)
        if data.get("id") != docket_id:
            raise SourceError("Docket response does not match the requested docket")
        text = lambda key: clean(data, key)
        path = text("absolute_url")
        if path and not path.startswith(f"/docket/{docket_id}/"):
            path = None  # Only trust page paths for this docket on this host.
        court_id = text("court_id")
        court_id = court_id if court_id and COURT_ID.fullmatch(court_id) else None
        return {"case_name": text("case_name") or text("case_name_short"), "docket_number": text("docket_number"),
                "court": text("court_citation_string") or (court_id or "").upper() or None,
                "court_id": court_id, "absolute_url": path}

    def fetch_court_info(self, court_id):
        """Display names for one court, e.g. citation 'S.D.N.Y.'. One request per court."""
        if not COURT_ID.fullmatch(court_id or ""):
            raise SourceError("Invalid court identifier")
        data = self._get(COURTS.format(court_id), None, envelope=False)
        if data.get("id") != court_id:
            raise SourceError("Court response does not match the requested court")
        return {"citation": clean(data, "citation_string") or clean(data, "short_name"),
                "full_name": clean(data, "full_name")}

    def _get(self, url, params, envelope=True):
        if not self.budget.acquire():
            raise QuotaExhausted("Request budget exhausted or paused")
        response = self.session.get(url, params=params, timeout=(10, 30), allow_redirects=False)
        if response.status_code == 429:
            self.budget.block()
            raise QuotaExhausted("HTTP 429; requests paused for 24 hours")
        if response.status_code != 200:
            raise SourceError(f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise SourceError("Response is not JSON") from None
        if not envelope:
            if not isinstance(data, dict):
                raise SourceError("Invalid response")
            return data
        if not isinstance(data, dict) or not isinstance(data.get("results"), list) or "next" not in data:
            raise SourceError("Invalid pagination envelope")
        if data["next"] is not None and (not isinstance(data["next"], str) or not data["next"]):
            raise SourceError("Invalid next page")
        return data

    def fetch_docket_changes(self, docket_id, since, until):
        params = {"docket": docket_id, "date_modified__gte": since, "date_modified__lt": until,
                  "order_by": "date_modified,id"}
        low, high = timestamp(since), timestamp(until)
        url, rows, previous, visited = "https://" + HOST + PATH, [], None, set()
        for page in range(self.max_pages):
            try:
                data = self._get(url, params if page == 0 else None)
            except QuotaExhausted:
                if not rows:
                    raise
                break  # Keep the pages already read; the cursor moves to the last row.
            for row in data["results"]:
                if not isinstance(row, dict) or type(row.get("id")) is not int or not isinstance(row.get("date_modified"), str):
                    raise SourceError("Invalid entry")
                try:
                    key = (timestamp(row["date_modified"]), row["id"])
                except ValueError:
                    raise SourceError("Invalid entry timestamp") from None
                if not low <= key[0] < high:
                    raise SourceError("Entry outside the requested window")
                if previous is not None and key <= previous:
                    raise SourceError("Entries are not in increasing order")
                previous = key
                rows.append(row)
            if data["next"] is None:
                return SourceResult(rows, True, until)
            if not data["results"]:
                raise SourceError("Empty page with a next link")
            check_next(data["next"], params)
            if data["next"] in visited:
                raise SourceError("Repeated pagination URL")
            visited.add(url := data["next"])
        # More pages remain. Resume from the last timestamp read; replayed rows are deduplicated.
        cursor = utc(rows[-1]["date_modified"])
        if timestamp(cursor) <= low:
            raise SourceError("Cursor cannot advance: too many entries share one timestamp")
        return SourceResult(rows, False, cursor)
