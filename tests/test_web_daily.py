"""Daily checks: skipping cases already shown, the search window, and the Today page."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from src.matching import discover
from src.matching.run import find_cases
from src.web.app import create_app
from src.web.jobs import Jobs, write_json

ENV = {"LLM_PROVIDER": "openai_compatible", "LLM_BASE_URL": "http://localhost:1", "LLM_MODEL": "test-model",
       "COURTLISTENER_TOKEN": "test-token"}


def row(n):
    return {"docket_id": n, "name": f"Case {n}", "court": "NVD", "filed": "2026-10-29", "url": f"https://cl/docket/{n}/",
            "document_label": "Complaint", "document_url": None, "description": "COMPLAINT", "filepath": f"{n}.pdf",
            "has_pdf": n != 3, "page_count": None, "pacer_doc_id": f"0{n}"}


class FakeModel:
    stats = {"cached": 0}

    def __init__(self):
        self.screened = None

    def record(self, *args, **kwargs):
        pass

    def ask(self, prompt_name, user, parse, max_tokens, label=""):
        if prompt_name == "case_search_v1":
            return [{"label": "Casino", "terms": "casino"}]
        self.screened = user
        return {"C1": {"relevant": True, "unclear": False, "domains": [], "case_type": "Class action", "reason": "Fits."},
                "C2": {"relevant": True, "unclear": False, "domains": [], "case_type": "Patent", "reason": "Docket names a patent."}}


def daily_run(root, run_id, cv, started, urls):
    folder = root / run_id
    folder.mkdir(parents=True)
    write_json(folder / "status.json", {"state": "done", "cv": cv, "daily": True, "days": 1, "started": started,
                                        "steps": [], "error": None, "model": "m"})
    candidates = [{"name": f"Case {u}", "url": u, "court": "NVD", "filed": "2026-10-29", "found_by": "Casino",
                   "screen": {"relevant": i == 0}, "kept": i == 0} for i, u in enumerate(urls)]
    results = [{"case": {"name": "Case 1", "url": urls[0], "court": "NVD", "filed": "2026-10-29"}, "profile": None,
                "match": {"tier": "high", "summary": "Strong fit.", "matches": []}, "score": {"total": 4.5}}]
    write_json(folder / "result.json", [{"profile": {"name": "Jane Doe"}, "results": results,
                                         "discovery": {"since": "2026-10-29", "skipped": 0, "candidates": candidates}}])


class DailyTests(unittest.TestCase):
    def test_search_window_starts_days_before_today(self):
        search = discover.CourtListenerSearch(MagicMock(), MagicMock(), Path(tempfile.mkdtemp()), days=1,
                                              today=lambda: date(2026, 10, 31))
        self.assertEqual(search.since(), date(2026, 10, 30))

    def test_cases_already_shown_are_not_taken_again(self):
        search = MagicMock(days=1, **{"since.return_value": date(2026, 10, 30)})
        search.search.return_value = ([row(1), row(2), row(3)], False)
        search.download.side_effect = lambda case, pdf_dir: Path("/nonexistent") / case["filepath"]
        model = FakeModel()
        expert = {"name": "Jane", "headline": "", "domains": [], "items": []}
        cases, record = find_cases(model, search, expert, 6, Path("/nonexistent"), lambda item: None,
                                   skip={"https://cl/docket/1/"})
        self.assertEqual([c["url"] for c in cases], ["https://cl/docket/2/"])
        self.assertEqual(record["skipped"], 1)
        self.assertEqual(record["since"], "2026-10-30")
        self.assertEqual(search.download.call_count, 1)  # Case 3 has no PDF: screened on its docket text only.
        self.assertNotIn("Case 1", model.screened)
        self.assertIn('"complaint": "not available"', model.screened)
        no_pdf = next(c for c in record["candidates"] if c["url"] == "https://cl/docket/3/")
        self.assertEqual((no_pdf["kept"], no_pdf["cost"], record["cost"]), (False, 3.0, 3.0))

    def test_today_page_and_shown_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "cvs").mkdir()
            (data / "cvs" / "jane.pdf").write_bytes(b"%PDF-1.4")
            daily_run(data / "runs", "20261030-090000-aaaaaa", "jane.pdf", "2026-10-30T16:00:00+00:00",
                      ["https://cl/docket/1/", "https://cl/docket/2/"])
            daily_run(data / "runs", "20261030-100000-bbbbbb", "other.pdf", "2026-10-30T17:00:00+00:00",
                      ["https://cl/docket/3/"])
            jobs = Jobs(data / "runs", None, [], data, 1000, data, ENV)
            self.assertEqual(jobs.shown("jane.pdf"), {"https://cl/docket/1/", "https://cl/docket/2/"})
            client = create_app(ENV, data_dir=data).test_client()
            page = client.get("/today").get_data(as_text=True)
            self.assertIn("Strong fit.", page)
            self.assertIn("2 new complaints found", page)
            self.assertIn("1 screened out", page)
            bad = client.post("/daily", data={"sample": "jane.pdf", "days": "30"})
            self.assertIn("time+window", bad.headers["Location"])
            self.assertIn("Check new cases", client.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
