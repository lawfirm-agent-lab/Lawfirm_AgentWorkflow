"""Inspect API capabilities with at most three requests; never fetch case content."""
import json
import os

import requests

BASE = "https://www.courtlistener.com/api/rest/v4/"
CHECKS = (("GET", "api-usage"), ("OPTIONS", "docket-entries"), ("OPTIONS", "opinions"))
FIELDS = ("id", "date_created", "date_modified", "date_filed")


def describe(endpoint, data):
    if endpoint == "api-usage":
        usage = data.get("current_usage")
        if not isinstance(usage, list):
            raise ValueError("Missing usage metadata")
        return {"limits": [
            {key: row[key] for key in ("scope", "rate", "used", "limit", "remaining", "blocked") if key in row}
            for row in usage if isinstance(row, dict)
        ]}
    filters = data.get("filters")
    ordering = data.get("ordering")
    return {
        "filters_metadata_available": isinstance(filters, dict),
        "candidate_filters": {
            name: {key: filters[name][key] for key in ("type", "lookup_types") if key in filters[name]}
            for name in FIELDS
            if isinstance(filters, dict) and isinstance(filters.get(name), dict)
        },
        "ordering_metadata_available": isinstance(ordering, list),
        "candidate_ordering": [name for name in FIELDS if isinstance(ordering, list) and name in ordering],
        "note": "Metadata only. Filter behavior, data access, volume and completeness are not verified.",
    }


def main():
    token = os.environ.get("COURTLISTENER_TOKEN", "").strip()
    if not token:
        print("FAIL: Missing COURTLISTENER_TOKEN. No requests sent.")
        return 1
    failed = False
    attempted = 0
    with requests.Session() as session:
        session.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})
        # Requests defaults to no retries. Redirects are disabled; each check is one request.
        for method, endpoint in CHECKS:
            attempted += 1
            try:
                response = session.request(method, BASE + endpoint + "/", timeout=(10, 30), allow_redirects=False)
                print(f"{method} {endpoint}: HTTP {response.status_code}")
                if response.status_code != 200:
                    failed = True
                    if response.status_code in (401, 429):
                        print("Stopping on authentication failure or rate limit. No retries.")
                        break
                    continue
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Invalid metadata")
                # Never print raw response bodies, credentials or exception messages.
                print(json.dumps(describe(endpoint, data), ensure_ascii=True))
            except (requests.RequestException, ValueError):
                failed = True
                print("FAIL: Request or metadata parsing failed. Details omitted to protect credentials.")
    print(f"Requests attempted: {attempted}/3. No database writes or emails performed.")
    print("A successful probe does not certify full-site coverage or sustainable ingestion capacity.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
