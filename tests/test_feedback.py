"""Feedback statistics and exports (src.matching.feedback), and the prompt fingerprint on runs."""
import csv
import io
import unittest

from src.matching import feedback
from src.matching.run import prompt_version


def row(verdict, tier="high", who="advisor", case="Case A", current=True, kind="rating", reason=None, should_be=None,
        source="courtlistener", contact=False, note=None):
    return {"who": who, "run_id": "r1", "case_index": 0, "case_name": case, "case_url": "https://cl/docket/1/",
            "cv": "jane.pdf", "kind": kind, "verdict": verdict, "reason": reason, "should_be": should_be,
            "would_contact": contact, "note": note, "current": current, "at": "2026-11-05T10:00:00",
            "snapshot": {"tier": tier, "source": source, "model": "deepseek-flash", "prompt_version": "ab12cd34",
                         "screen": {"relevant": True, "reason": "Fits, \"quoted\", with a comma."}}}


ROWS = [row("up", contact=True), row("down", "high", case="Case B", reason="not_relevant", should_be="none",
                                    note="Pro se, no tech issue"),
        row("up", "medium", case="Case B", current=False),  # Replaced by the down above.
        row("down", "medium", case="Case C", reason="wrong_level", should_be="low", source="web web page"),
        row("gmail", kind="email"), row("copy", kind="email")]


class FeedbackTests(unittest.TestCase):
    def test_summary_counts_current_ratings_only(self):
        s = feedback.summarize(ROWS)
        self.assertEqual((s["ratings"], s["useful"], s["would_contact"], s["emailed"], s["changed"]), (3, 33, 1, 1, 1))
        self.assertEqual(s["by_tier"], {"high": {"n": 2, "useful": 50}, "medium": {"n": 1, "useful": 0}})
        self.assertEqual(s["by_source"]["Web page only"], {"n": 1, "useful": 0})
        self.assertEqual(s["reasons"], {"Not relevant to this expert": 1, "Relevant, but the level is wrong": 1})
        self.assertEqual(s["levels"], {"high -> none": 1, "medium -> low": 1})
        self.assertEqual([r["case_name"] for r in s["attention"]], ["Case B"])
        self.assertEqual(list(s["by_version"]), ["deepseek-flash / prompts ab12cd34"])
        text = feedback.report(s)
        self.assertIn("3 ratings, 33% useful", text)
        self.assertIn("Needs attention (High, not useful): Case B for jane.pdf by advisor - Not relevant to this expert: Pro se", text)
        self.assertEqual(feedback.report(feedback.summarize([])), "No ratings yet.")

    def test_csv_has_every_row_with_its_snapshot(self):
        lines = list(csv.DictReader(io.StringIO(feedback.csv_text(ROWS))))
        self.assertEqual(len(lines), len(ROWS))
        self.assertEqual(list(lines[0]), list(feedback.COLUMNS))
        self.assertEqual((lines[1]["tier"], lines[1]["reason"], lines[1]["screen_reason"]),
                         ("high", "not_relevant", 'Fits, "quoted", with a comma.'))

    def test_prompt_version_is_a_stable_fingerprint(self):
        self.assertRegex(prompt_version(), r"^[0-9a-f]{8}$")
        self.assertEqual(prompt_version(), prompt_version())


if __name__ == "__main__":
    unittest.main()
