"""The cloud log report (src.matching.logs): durations, CVs missing a daily check, failures."""
import unittest
from datetime import datetime, timezone

from src.matching import logs

NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)


def run(cv, started, state="done", daily=True, error=None):
    return {"run_id": f"r-{cv}", "cv": cv, "owner": None, "state": state, "started": started, "finished": "2026-11-05T11:35:12+00:00",
            "daily": daily, "error": error, "stats": {"calls": 9, "cached": 2}, "usage": {"courtlistener": 4, "web_searches": 2},
            "candidates": 12, "cases": 3, "tiers": {"high": 1, "medium": 2}}


DATA = {"since": "2026-11-03T18:00:00", "now": "2026-11-05T18:00:00",
        "schedule": [{"at": "2026-11-05T11:30:00.2+00:00", "status": "succeeded", "message": "1 row"},
                     {"at": "2026-11-04T11:30:00.2+00:00", "status": "failed", "message": "permission denied"}],
        "dispatches": [{"at": "2026-11-05T11:30:01+00:00", "status": 204, "error": None},
                       {"at": "2026-11-05T12:00:01+00:00", "status": 401, "error": None}],
        "runs": [run("a.pdf", "2026-11-05T11:31:00+00:00"),
                 run("b.pdf", "2026-11-05T11:33:00+00:00", "failed", error="LLM call failed: timeout")],
        "requests": [{"created": "2026-11-05T09:00:00+00:00", "cv": "up.pdf", "owner": "advisor", "state": "failed",
                      "error": "InvalidOutput: defendants"}],
        "pipeline": [{"started": "2026-11-05T06:32:22+00:00", "status": "failed",
                      "report": {"stages": {"detect": {"status": "failed", "error": "ReadTimeout"}, "analyze": {"status": "ok"}}}},
                     {"started": "2026-11-05T12:32:22+00:00", "status": "ok", "report": {}}],
        "cvs": [{"name": "a.pdf", "active": True, "saved_at": "2026-11-01T00:00:00+00:00"},
                {"name": "b.pdf", "active": True, "saved_at": "2026-11-01T00:00:00+00:00"},
                {"name": "c.pdf", "active": True, "saved_at": "2026-11-01T00:00:00+00:00"},
                {"name": "new.pdf", "active": True, "saved_at": "2026-11-05T10:00:00+00:00"},
                {"name": "off.pdf", "active": False, "saved_at": "2026-11-01T00:00:00+00:00"}]}


class LogTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(logs.took("2026-11-05T11:31:00+00:00", "2026-11-05T11:35:12+00:00"), "4m12s")
        self.assertEqual(logs.took("2026-11-05T11:31:00", "2026-11-05T11:31:09Z"), "9s")
        self.assertEqual(logs.took("2026-11-05T11:31:00", None), "-")

    def test_a_cv_saved_long_enough_ago_without_a_check_is_flagged(self):
        self.assertEqual(logs.unchecked(DATA, NOW), ["c.pdf"])  # new.pdf is too new, off.pdf is stopped.

    def test_report_shows_what_went_wrong(self):
        text = logs.report(DATA, NOW)
        self.assertIn("2026-11-04 11:30  failed  permission denied", text)
        self.assertIn("GitHub dispatches (last hours only): 2, 1 not accepted", text)
        self.assertIn("a.pdf (admin) · daily · done · 4m12s · 9 AI calls (2 cached) · 4 CourtListener, 2 web · 12 found, 3 read [1 high, 2 medium]", text)
        self.assertIn("error: LLM call failed: timeout", text)
        self.assertIn("no daily check in the last 24 hours: c.pdf", text)
        self.assertIn("up.pdf (advisor): InvalidOutput: defendants", text)
        self.assertIn("Docket monitor runs: 2, 1 failed\n  2026-11-05 06:32  detect: ReadTimeout", text)


if __name__ == "__main__":
    unittest.main()
