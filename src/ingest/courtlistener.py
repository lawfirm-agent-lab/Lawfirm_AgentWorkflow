"""Read CourtListener docket entries; this does not fetch document PDFs."""
from urllib.parse import urlsplit

import requests

URL = "https://www.courtlistener.com/api/rest/v4/docket-entries/"


def fetch_entries(docket_id: int, token: str) -> list[dict]:
    if not token.strip():
        raise ValueError("COURTLISTENER_TOKEN is required")
    entries, visited = [], set()
    url, params = URL, {"docket": docket_id}
    with requests.Session() as session:
        session.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})
        while url:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "www.courtlistener.com":
                raise ValueError("Unexpected pagination host; refusing to forward token")
            if url in visited:
                raise ValueError("Repeated pagination URL")
            visited.add(url)
            response = session.get(url, params=params, timeout=(10, 30), allow_redirects=False)
            if 300 <= response.status_code < 400:
                raise ValueError("Unexpected API redirect")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ValueError("API response does not contain a results list")
            if not all(isinstance(row, dict) for row in data["results"]):
                raise ValueError("API returned an invalid entry")
            entries.extend(data["results"])
            url, params = data.get("next"), None
            if url is not None and not isinstance(url, str):
                raise ValueError("Invalid pagination URL")
    return entries
