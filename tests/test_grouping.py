"""Compact "Other activity": grouping heavy filing days, taken from a real S.D.N.Y. digest."""
import unittest
from datetime import datetime, timezone

from src.core.models import Analysis, Event
from src.notify.grouping import classify, declarants, group, ranges
from src.notify.render import one_line, render_digest

REAL = [
    "***SEALED***DECLARATION of Davida Brook (VOL. 9) in Support re: 2119 Memorandum of Law in Opposition to Motion,. Document filed by The New York Times",
    "***SEALED*** MEMORANDUM OF LAW in Opposition re: (1759 in 1:25-md-03143-SHS-OTW) MOTION for Summary Judgment . . Document filed by Microsoft",
    "***SEALED***DECLARATION of Davida Brook (VOL. 10) in Support re: 2119 Memorandum of Law",
    "DECLARATION of Allison S. Blanco (Vol. 1) in Opposition re: (1696 in 1:25-md-03143-SHS-OTW) MOTION for Summary Judgment",
    "DECLARATION of Tom Goldstein in Opposition re: (1712 in 1:25-md-03143-SHS-OTW)",
    "***SEALED***DECLARATION of Davida Brook (VOL. 11) in Support re: 2119",
    "CERTIFICATE OF SERVICE of Sealed Declarations in Support of News Plaintiffs' Opposition",
    "CERTIFICATE OF SERVICE of Exhibits to Declaration of Antoinette Tease served on Microsoft",
    "LETTER MOTION to Seal OpenAI's Opposition to Plaintiffs' MSJs addressed to Judge Sidney H. Stein",
    "***SEALED***DECLARATION of Allison S. Blanco (Sealed Vol. 2) in Opposition re: (1696",
    "AFFIDAVIT of Lisa T. Simpson in Opposition re: (1759 in 1:25-md-03143-SHS-OTW)",
    "DECLARATION of MAILE YEATS-ROWE in Support re: (729 in 1:25-cv-04315-SHS-OTW",
    "MEMORANDUM OF LAW in Opposition re: (1696 in 1:25-md-03143-SHS-OTW) MOTION for Summary Judgment",
    "", "", "",
]


def entries():
    return [{"number": 2178 + i, "description": d} for i, d in enumerate(REAL)]


class GroupingTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify(REAL[0])[:2], ("declarations and affidavits", True))
        self.assertEqual(classify(REAL[8])[:2], ("letter motions", False))
        self.assertEqual(classify("ORDER granting motion")[0], None)
        self.assertNotIn("SEALED", classify(REAL[0])[2])

    def test_declarants_and_volumes(self):
        texts = [classify(d)[2] for d in REAL if classify(d)[0] == "declarations and affidavits"]
        self.assertEqual(declarants(texts), "Davida Brook (Vols. 9–11), Allison S. Blanco (Vols. 1–2), Tom Goldstein, "
                                            "Lisa T. Simpson, Maile Yeats-Rowe")
        self.assertEqual(ranges([9, 10, 11, 14, 16, 17]), "9–11, 14, 16–17")

    def test_heavy_day_groups(self):
        groups, singles, undescribed = group(entries())
        summary = [(g["label"], g["count"], g["sealed"]) for g in groups]
        self.assertEqual(summary, [("declarations and affidavits", 8, 4), ("certificates of service", 2, 0),
                                   ("memoranda of law", 2, 1)])
        self.assertEqual([e["number"] for e in singles], [2186])
        self.assertEqual(undescribed, 3)

    def test_word_boundary_truncation(self):
        self.assertEqual(one_line("Document filed by The New York Times Company", 30), "Document filed by The New…")
        self.assertEqual(one_line("short", 30), "short")


class HeavyDigestTests(unittest.TestCase):
    def test_low_subscriber_digest_is_compact(self):
        items = [(Event(f"e{i}", "courtlistener", 1, 100 + i, "added",
                        {"entry_number": 2178 + i, "date_filed": "2026-10-23", "description": d}), Analysis("low"))
                 for i, d in enumerate(REAL)]
        info = {1: {"case_name": "In Re: OpenAI", "docket_number": "1:25-md-03143", "court": "S.D.N.Y.",
                    "absolute_url": "/docket/1/in-re-openai/"}}
        subject, text, rich = render_digest(items, info, datetime(2026, 10, 24, tzinfo=timezone.utc), remaining=7)
        self.assertEqual(subject, "[16 updates] In Re: OpenAI — Oct 23")
        self.assertIn("16 other filings · 1 case · 7 more in the next digest", text)
        self.assertIn("  • 8 declarations and affidavits (4 sealed): Davida Brook (Vols. 9–11)", text)
        self.assertIn("  • 3 entries without a description yet", text)
        self.assertIn("View these 16 entries on CourtListener: https://www.courtlistener.com/docket/1/in-re-openai/"
                      "?entry_gte=2178&entry_lte=2193", text)
        self.assertEqual(text.count("Document filed by"), 0)
        self.assertLess(len(text.splitlines()), 25)
        self.assertIn("8 declarations and affidavits (4 sealed)", rich)


if __name__ == "__main__":
    unittest.main()
