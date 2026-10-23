"""Digest rendering: structure, links, escaping and fallbacks."""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.core.config import Limits
from src.core.models import Analysis, Event
from src.core.ratelimit import RequestBudget
from src.notify.render import pdf_url, render_digest
from src.sources.courtlistener import CourtListenerSource, SourceError
from src.store.pipeline import PipelineStore

NOW = datetime(2026, 10, 22, 23, 17, tzinfo=timezone.utc)
HIGH = Analysis("high", "court_order_candidate", reasons=("x",))
LOW = Analysis("low", "other")
INFO = {1: {"case_name": "In re: OpenAI, Inc. Copyright Infringement Litigation", "docket_number": "1:25-md-03143",
            "court": "S.D.N.Y.", "absolute_url": "/docket/1/in-re-openai/"},
        2: {"case_name": "FTC v. Amazon.com Inc", "docket_number": "2:23-cv-01495", "court": "W.D. Wash.",
            "absolute_url": "/docket/2/ftc-v-amazon/"}}


def event(docket, entry, number, description, kind="added", before=None, docs=None):
    payload = {"entry_number": number, "date_filed": "2026-10-22", "description": description,
               "recap_documents": docs or []}
    return Event(f"e{entry}", "courtlistener", docket, entry, kind, payload, before)


class RenderTests(unittest.TestCase):
    def test_orders_first_with_case_names_and_links(self):
        items = [(event(1, 11, 1235, "LETTER MOTION for extension."), LOW),
                 (event(2, 21, 612, "MINUTE ORDER: conference reset.", "modified", {"description": "MINUTE ORDER: old."}), HIGH),
                 (event(1, 10, 1234, "ORDER granting motion to compel."), HIGH)]
        subject, text, rich = render_digest(items, INFO, NOW)
        self.assertEqual(subject, "[2 court orders] FTC v. Amazon.com Inc +1 case — Oct 22")
        self.assertTrue(text.startswith("2 court orders · 1 other filing · 2 cases    Oct 22, 2026, 4:17 PM PT"))
        self.assertLess(text.index("COURT ORDERS"), text.index("OTHER ACTIVITY"))
        self.assertLess(text.index("ORDER granting"), text.index("LETTER MOTION"))
        self.assertIn("1:25-md-03143 · S.D.N.Y.", text)
        self.assertIn("#612 · Filed Oct 22, 2026 · UPDATED · description changed", text)
        self.assertIn("https://www.courtlistener.com/docket/1/in-re-openai/?entry_gte=1234&entry_lte=1234", text)
        self.assertIn('href="https://www.courtlistener.com/docket/2/ftc-v-amazon/?entry_gte=612&amp;entry_lte=612"', rich)
        self.assertNotIn("api/rest", text + rich)

    def test_updates_only_and_fallbacks(self):
        subject, text, _ = render_digest([(event(9, 90, None, "Notice of appearance."), LOW)], {}, NOW)
        self.assertEqual(subject, "[1 update] Docket 9 — Oct 22")
        self.assertNotIn("COURT ORDERS", text)
        self.assertIn("#? · Oct 22, 2026 · NEW", text)
        self.assertIn("https://www.courtlistener.com/docket/9/\n", text)

    def test_escaping_and_header_safety(self):
        info = {1: {"case_name": "Evil\r\nBcc: x@example.com <b>", "absolute_url": None}}
        subject, text, rich = render_digest([(event(1, 1, 1, "<script>alert(1)</script> ORDER"), HIGH)], info, NOW)
        self.assertNotIn("\n", subject)
        self.assertNotIn("<script>", rich)
        self.assertIn("&lt;script&gt;", rich)
        self.assertNotIn("<b>", rich)
        self.assertIn("<script>alert(1)</script> ORDER", text)

    def test_pdf_links_are_restricted(self):
        good = {"is_available": True, "filepath_local": "recap/gov.uscourts.nysd.1/gov.uscourts.nysd.1.10.0.pdf"}
        self.assertEqual(pdf_url({"recap_documents": [{**good, "attachment_number": 2}, {**good, "filepath_local": good["filepath_local"].replace("10.0", "10.main")}]}),
                         "https://storage.courtlistener.com/recap/gov.uscourts.nysd.1/gov.uscourts.nysd.1.10.main.pdf")
        for bad in ({**good, "is_available": False}, {**good, "filepath_local": "javascript:alert(1)"},
                    {**good, "filepath_local": "recap/../../etc/passwd.pdf"}, {**good, "filepath_local": None}):
            with self.subTest(bad=bad):
                self.assertIsNone(pdf_url({"recap_documents": [bad]}))
        _, text, rich = render_digest([(event(1, 1, 1, "ORDER", docs=[good]), HIGH)], INFO, NOW)
        self.assertIn("PDF: https://storage.courtlistener.com/recap/", text)
        self.assertIn(">PDF</a>", rich)

    def test_document_only_change_label(self):
        before = {"description": "ORDER"}
        _, text, _ = render_digest([(event(1, 1, 1, "ORDER", "modified", before), HIGH)], INFO, NOW)
        self.assertIn("UPDATED · documents or details changed", text)


class DocketInfoTests(unittest.TestCase):
    def fetch(self, data, docket_id=5):
        store = PipelineStore(":memory:")
        self.addCleanup(store.close)
        session = MagicMock()
        session.get.return_value = MagicMock(status_code=200, json=lambda: data)
        result = CourtListenerSource(session, RequestBudget(store, Limits())).fetch_docket_info(docket_id)
        self.assertEqual(session.get.call_args.args[0], f"https://www.courtlistener.com/api/rest/v4/dockets/{docket_id}/")
        return result

    def test_valid_info(self):
        info = self.fetch({"id": 5, "case_name": " A v. B ", "docket_number": "1:26-cv-1", "court_id": "nysd",
                           "absolute_url": "/docket/5/a-v-b/"})
        self.assertEqual(info, {"case_name": "A v. B", "docket_number": "1:26-cv-1", "court": "NYSD",
                                "court_id": "nysd", "absolute_url": "/docket/5/a-v-b/"})

    def test_untrusted_values_are_dropped(self):
        info = self.fetch({"id": 5, "case_name": None, "case_name_short": "Short", "court_citation_string": "S.D.N.Y.",
                           "absolute_url": "https://evil.example/docket/5/"})
        self.assertEqual((info["case_name"], info["court"], info["absolute_url"]), ("Short", "S.D.N.Y.", None))
        with self.assertRaises(SourceError):
            self.fetch({"id": 6})
        self.assertIsNone(self.fetch({"id": 5, "court_id": "../evil"})["court_id"])

    def court(self, data, court_id="nysd"):
        store = PipelineStore(":memory:")
        self.addCleanup(store.close)
        session = MagicMock()
        session.get.return_value = MagicMock(status_code=200, json=lambda: data)
        source = CourtListenerSource(session, RequestBudget(store, Limits()))
        return source.fetch_court_info(court_id), session

    def test_court_info(self):
        info, session = self.court({"id": "nysd", "citation_string": "S.D.N.Y.", "short_name": "S.D. New York",
                                    "full_name": "District Court, S.D. New York"})
        self.assertEqual(info, {"citation": "S.D.N.Y.", "full_name": "District Court, S.D. New York"})
        self.assertEqual(session.get.call_args.args[0], "https://www.courtlistener.com/api/rest/v4/courts/nysd/")
        self.assertEqual(self.court({"id": "nysd", "short_name": "S.D. New York"})[0]["citation"], "S.D. New York")
        for data, court_id in (({"id": "cand"}, "nysd"), ({"id": "x"}, "x/../y"), ({"id": ""}, "")):
            with self.subTest(court_id=court_id), self.assertRaises(SourceError):
                self.court(data, court_id)


if __name__ == "__main__":
    unittest.main()
