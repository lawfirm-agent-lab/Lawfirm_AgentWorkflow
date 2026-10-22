"""Bounded, read-only sampling of modification-time queries; no persistence."""
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import requests

BASE = "https://www.courtlistener.com/api/rest/v4/"


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result


def check_next(url, endpoint, params):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "www.courtlistener.com"
            or parsed.path != f"/api/rest/v4/{endpoint}/" or parsed.fragment):
        raise ValueError("Unsafe pagination")
    query = parse_qs(parsed.query)
    for key in ("date_modified__gte", "date_modified__lt", "order_by", "fields"):
        if query.get(key) != [params[key]]:
            raise ValueError("Pagination changed query")


def main():
    token = os.environ.get("COURTLISTENER_TOKEN", "").strip()
    if not token:
        print("FAIL: Missing COURTLISTENER_TOKEN. No requests sent.")
        return 1
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=10)
    start = end - timedelta(minutes=15)
    print(json.dumps({"window_start_utc": start.isoformat(), "window_end_utc_exclusive": end.isoformat()}))
    attempted = 0
    failed = False
    stop = False
    with requests.Session() as session:
        session.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})
        for endpoint, relation in (("docket-entries", "docket"), ("opinions", "cluster")):
            params = {"date_modified__gte": start.isoformat(), "date_modified__lt": end.isoformat(),
                      "order_by": "date_modified,id", "fields": f"id,date_created,date_modified,{relation}"}
            url = BASE + endpoint + "/"
            seen_urls, ids, relations = set(), set(), set()
            count = pages = duplicates = 0
            previous = None
            status = "unknown"
            try:
                for page in range(2):
                    if page:
                        check_next(url, endpoint, params)
                    if url in seen_urls:
                        raise ValueError("Repeated page")
                    seen_urls.add(url)
                    attempted += 1
                    response = session.get(url, params=params if page == 0 else None,
                                           timeout=(10, 30), allow_redirects=False)
                    print(f"GET {endpoint} page {page + 1}: HTTP {response.status_code}")
                    if response.status_code != 200:
                        stop = response.status_code in (401, 429)
                        raise ValueError("HTTP failure")
                    data = response.json()
                    if not isinstance(data, dict) or not isinstance(data.get("results"), list) or "next" not in data:
                        raise ValueError("Invalid pagination envelope")
                    next_url = data["next"]
                    if next_url is not None and (not isinstance(next_url, str) or not next_url):
                        raise ValueError("Invalid next page")
                    pages += 1
                    for row in data["results"]:
                        if not isinstance(row, dict) or type(row.get("id")) is not int:
                            raise ValueError("Invalid record")
                        modified = timestamp(row["date_modified"])
                        key = (modified, row["id"])
                        if not start <= modified < end or (previous is not None and key < previous):
                            raise ValueError("Time filter or ordering violation")
                        previous = key
                        count += 1
                        duplicates += int(row["id"] in ids)
                        ids.add(row["id"])
                        linked = row.get(relation)
                        if isinstance(linked, (int, str)):
                            relations.add(linked)
                    if next_url is None:
                        status = "sample_window_exhausted"
                        break
                    url = next_url
                    status = "partial_page_cap"
                if duplicates:
                    raise ValueError("Duplicate records across pages")
            except (requests.RequestException, ValueError, KeyError, TypeError):
                status = "failed_validation_or_request"
                failed = True
                print("FAIL: Request or validation failed; raw response and exception details withheld.")
            print(json.dumps({"endpoint": endpoint, "status": status, "pages_received": pages,
                              "records_checked": count, "unique_records": len(ids), "duplicates": duplicates,
                              "distinct_" + relation + "_references": len(relations),
                              "note": "Opinion clusters are not distinct cases. Empty results do not validate filtering."}))
            if stop:
                print("Stopping on authentication failure or throttling; no retries.")
                break
    print(f"Requests attempted: {attempted}/4. No database writes, files containing case data, or emails.")
    print("Window exhaustion is not proof of full-site coverage, snapshot consistency, or future throughput.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
