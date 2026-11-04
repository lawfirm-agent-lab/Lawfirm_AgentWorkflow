"""CourtListener incremental source and the persistent request budget; network is mocked."""
import unittest
from unittest.mock import MagicMock

import requests
from urllib.parse import urlencode

from src.core.config import Limits
from src.core.ratelimit import RequestBudget
from src.sources.courtlistener import CourtListenerSource, QuotaExhausted, SourceError, Unavailable
from src.store.pipeline import PipelineStore

SINCE, UNTIL = "2026-10-23T10:00:00+00:00", "2026-10-23T12:00:00+00:00"
PARAMS = {"docket": 7, "date_modified__gte": SINCE, "date_modified__lt": UNTIL, "order_by": "date_modified,id"}
NEXT = "https://www.courtlistener.com/api/rest/v4/docket-entries/?" + urlencode(PARAMS) + "&cursor=abc"


def row(i, modified="2026-10-23T11:00:00Z"):
    return {"id": i, "date_modified": modified}


def response(results=(), next_url=None, status=200):
    return MagicMock(status_code=status, json=lambda: {"results": list(results), "next": next_url})


class BudgetBase(unittest.TestCase):
    def make_store(self):
        return PipelineStore(":memory:")

    def setUp(self):
        self.store = self.make_store()
        self.addCleanup(self.store.close)
        self.t = 1800000000.0
        self.slept = []

    def budget(self, **limits):
        def sleep(seconds):
            self.slept.append(seconds)
            self.t += seconds
        return RequestBudget(self.store, Limits(**limits), clock=lambda: self.t, sleep=sleep)


class SourceTests(BudgetBase):
    def fetch(self, *responses, max_pages=5):
        session = MagicMock()
        session.get.side_effect = list(responses)
        source = CourtListenerSource(session, self.budget(), max_pages)
        return source.fetch_docket_changes(7, SINCE, UNTIL), session

    def test_unchanged_docket_costs_one_request(self):
        result, session = self.fetch(response())
        self.assertEqual((result.rows, result.complete, result.cursor), ([], True, UNTIL))
        self.assertEqual(session.get.call_args.kwargs["params"], PARAMS)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_follows_validated_pages_with_pacing(self):
        result, session = self.fetch(response([row(1)], NEXT), response([row(2, "2026-10-23T11:30:00Z")]))
        self.assertEqual([r["id"] for r in result.rows], [1, 2])
        self.assertTrue(result.complete)
        self.assertEqual(session.get.call_args_list[1].args[0], NEXT)
        self.assertIsNone(session.get.call_args_list[1].kwargs["params"])
        self.assertEqual(self.slept, [13])

    def test_page_cap_returns_partial_cursor(self):
        result, _ = self.fetch(response([row(1, "2026-10-23T04:15:00-07:00")], NEXT), max_pages=1)
        self.assertFalse(result.complete)
        self.assertEqual(result.cursor, "2026-10-23T11:15:00+00:00")

    def test_rejects_unsafe_or_inconsistent_responses(self):
        cases = [
            response([row(1)], NEXT.replace("www.courtlistener.com", "evil.example")),
            response([row(1)], NEXT.replace("docket=7", "docket=8")),
            response([row(1, "2026-10-23T09:00:00Z")]),
            response([row(2), row(1)]),
            response([{"id": "1", "date_modified": SINCE}]),
            response([], NEXT),
            response(status=404),
            MagicMock(status_code=200, json=lambda: {"results": []}),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(SourceError):
                self.t += 100
                self.fetch(case)

    def test_timeout_or_server_error_is_tried_once_more(self):
        for first in (requests.ReadTimeout(), requests.ConnectionError(), response(status=502)):
            with self.subTest(first=first):
                self.t += 100
                result, session = self.fetch(first, response([row(1)]))
                self.assertEqual([r["id"] for r in result.rows], [1])
                self.assertEqual(session.get.call_count, 2)

    def test_unavailable_after_the_second_try(self):
        for responses, message in [((requests.ReadTimeout(), requests.ReadTimeout()), "ReadTimeout"),
                                   ((response(status=500), response(status=503)), "HTTP 503")]:
            with self.subTest(message=message), self.assertRaisesRegex(Unavailable, message):
                self.t += 100
                self.fetch(*responses)

    def test_retry_counts_against_the_budget(self):
        session = MagicMock()
        session.get.side_effect = [requests.ReadTimeout()]
        source = CourtListenerSource(session, self.budget(max_requests_per_run=1))
        with self.assertRaises(QuotaExhausted):
            source.fetch_docket_changes(7, SINCE, UNTIL)
        self.assertEqual(session.get.call_count, 1)

    def test_429_pauses_all_requests(self):
        with self.assertRaises(QuotaExhausted):
            self.fetch(response(status=429))
        self.t += 3600
        self.assertFalse(self.budget().acquire())

    def test_budget_exhaustion_keeps_rows_already_read(self):
        session = MagicMock()
        session.get.side_effect = [response([row(1)], NEXT)]
        source = CourtListenerSource(session, self.budget(max_requests_per_run=1))
        result = source.fetch_docket_changes(7, SINCE, UNTIL)
        self.assertEqual((len(result.rows), result.complete), (1, False))
        with self.assertRaises(QuotaExhausted):
            CourtListenerSource(session, self.budget(max_requests_per_run=0)).fetch_docket_changes(7, SINCE, UNTIL)


class BudgetTests(BudgetBase):
    def test_limits_are_enforced_across_runs(self):
        budget = self.budget()
        self.assertTrue(budget.acquire())
        self.assertTrue(budget.acquire())
        self.assertEqual(self.slept, [13])
        # Per-hour cap is 50 - 5 headroom; simulate prior usage in this hour.
        with self.store.transaction():
            for i in range(43):
                self.store.execute("INSERT INTO api_requests VALUES (?,?,?)", (f"r{i}", "courtlistener", self.t - 3000 + i))
        self.t += 100
        self.assertFalse(self.budget().acquire())
        self.t += 3600
        self.assertTrue(self.budget().acquire())

    def test_per_run_and_minute_caps(self):
        budget = self.budget(max_requests_per_run=2)
        self.assertTrue(budget.acquire() and budget.acquire())
        self.assertFalse(budget.acquire())
        budget = self.budget(per_minute=1, min_interval_seconds=0)
        self.assertFalse(budget.acquire())

    def test_block_never_shortens(self):
        budget = self.budget()
        budget.block(86400)
        budget.block(10)
        self.assertEqual(budget.blocked_until(), self.t + 86400)

    def test_future_ledger_refuses_instead_of_sleeping(self):
        with self.store.transaction():
            self.store.execute("INSERT INTO api_requests VALUES ('x','courtlistener',?)", (self.t + 1000,))
        self.assertFalse(self.budget().acquire())
        self.assertEqual(self.slept, [])

    def test_wake_up_just_short_of_the_interval_still_acquires(self):
        budget = self.budget()
        self.assertTrue(budget.acquire())
        budget.sleep = lambda seconds: setattr(self, "t", self.t + seconds - 0.001)  # wall-clock jitter
        self.assertTrue(budget.acquire())
        # A reservation by another run moments ago is still refused.
        with self.store.transaction():
            self.store.execute("INSERT INTO api_requests VALUES ('y','courtlistener',?)", (self.t + 20,))
        self.t += 22
        budget.sleep = lambda seconds: None
        self.assertFalse(budget.acquire())


if __name__ == "__main__":
    unittest.main()
