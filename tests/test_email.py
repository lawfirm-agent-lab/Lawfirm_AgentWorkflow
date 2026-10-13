import unittest
from src.analyze.classifier import classify
from src.notify.digest import build_digest
from src.notify.email import deliver
from src.store.db import Store


class EmailTests(unittest.TestCase):
    def test_rules_and_source(self):
        self.assertFalse(classify({"description": "Certificate of service filed."})["important"])
        row = {"id": 12, "entry_number": 5, "date_filed": "2026-10-10", "description": "ORDER setting hearing."}
        digest = build_digest(73132799, [row], today="2026-10-10")
        self.assertIn(row["description"], digest[1])
        self.assertIn("docket-entries/12/", digest[1])
        self.assertIsNone(build_digest(73132799, [{"id": 13, "description": "Receipt"}]))

    def test_send_once_and_uncertainty(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        cfg = {"EMAIL_FROM": "sender@example.com", "EMAIL_TO": "recipient@example.com"}
        sent = []
        deliver(store, 1, ("Subject", "Body"), "change:1", cfg, sent.append)
        deliver(store, 1, ("Subject", "Body"), "change:1", cfg, sent.append)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["To"], cfg["EMAIL_TO"])
        def fail(message):
            raise TimeoutError()
        with self.assertRaises(RuntimeError):
            deliver(store, 1, ("Subject", "Body"), "change:2", cfg, fail)
        self.assertIn("uncertain", deliver(store, 1, ("Subject", "Body"), "change:2", cfg, sent.append))
        self.assertEqual(len(sent), 1)


class TodayCourtTests(unittest.TestCase):
    def test_today_court_order(self):
        self.assertTrue(classify({"date_filed": "2026-10-10", "description": "ORDER granting motion."}, today="2026-10-10")["important"])

    def test_excludes_old_unknown_dates_and_party_filings(self):
        for row in [
            {"date_filed": "2026-10-09", "description": "ORDER setting trial."},
            {"description": "ORDER setting trial."},
            {"date_filed": "2026-10-10", "description": "MOTION for an order setting a deadline."},
            {"date_filed": "2026-10-10", "description": "Proposed ORDER filed by plaintiff."},
            {"date_filed": "2026-10-10", "description": "NOTICE of hearing filed by counsel."},
        ]:
            with self.subTest(row=row):
                self.assertFalse(classify(row, today="2026-10-10")["important"])

    def test_court_notice(self):
        self.assertTrue(classify({"date_filed": "2026-10-10", "description": "NOTICE FROM THE COURT: conference rescheduled."}, today="2026-10-10")["important"])
