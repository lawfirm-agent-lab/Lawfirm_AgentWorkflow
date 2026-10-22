import contextlib
import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from unittest.mock import MagicMock, patch

from src.ingest.probe_incremental import main


class IncrementalProbeTests(unittest.TestCase):
    def run_probe(self, handler, token="test-private-token"):
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.side_effect = handler
        output = io.StringIO()
        with patch.dict(os.environ, {"COURTLISTENER_TOKEN": token}), patch(
            "src.ingest.probe_incremental.requests.Session", return_value=session
        ), contextlib.redirect_stdout(output):
            result = main()
        self.assertNotIn("test-private-token", output.getvalue())
        return result, session, output.getvalue()

    def test_page_cap(self):
        def reply(url, **kwargs):
            params = kwargs["params"]
            next_url = url + "?" + urlencode(params) + "&cursor=next" if params else url + "2"
            return MagicMock(status_code=200, json=lambda: {"results": [], "next": next_url})
        result, session, output = self.run_probe(reply)
        self.assertEqual(result, 0)
        self.assertEqual(session.get.call_count, 4)
        self.assertEqual(output.count('"status": "partial_page_cap"'), 2)
        for call in session.get.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_unsafe_next_not_followed(self):
        result, session, _ = self.run_probe(lambda *a, **k: MagicMock(
            status_code=200, json=lambda: {"results": [], "next": "https://evil.example/"}))
        self.assertEqual(result, 1)
        self.assertEqual(session.get.call_count, 2)

    def test_throttle_stops(self):
        result, session, _ = self.run_probe(lambda *a, **k: MagicMock(status_code=429))
        self.assertEqual(result, 1)
        self.assertEqual(session.get.call_count, 1)

    def test_outside_window_fails(self):
        row = {"id": 1, "date_modified": "2000-01-01T00:00:00Z"}
        result, _, _ = self.run_probe(lambda *a, **k: MagicMock(
            status_code=200, json=lambda: {"results": [row], "next": None}))
        self.assertEqual(result, 1)

    def test_valid_records(self):
        def reply(url, **kwargs):
            relation = "docket" if "docket-entries" in url else "cluster"
            row = {"id": 1, "date_modified": kwargs["params"]["date_modified__gte"], relation: 42}
            return MagicMock(status_code=200, json=lambda: {"results": [row], "next": None})
        result, session, output = self.run_probe(reply)
        self.assertEqual(result, 0)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(output.count('"status": "sample_window_exhausted"'), 2)

    def test_no_token(self):
        result, session, _ = self.run_probe(None, token="")
        self.assertEqual(result, 1)
        session.get.assert_not_called()
