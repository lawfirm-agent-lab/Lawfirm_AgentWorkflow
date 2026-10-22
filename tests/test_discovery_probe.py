import contextlib
import io
import os
import unittest
from unittest.mock import MagicMock, patch

from src.ingest.probe_discovery import main


class DiscoveryProbeTests(unittest.TestCase):
    def run_probe(self, responses, token="private-test-token"):
        session = MagicMock()
        session.__enter__.return_value = session
        session.request.side_effect = responses
        output = io.StringIO()
        with patch.dict(os.environ, {"COURTLISTENER_TOKEN": token}), patch(
            "src.ingest.probe_discovery.requests.Session", return_value=session
        ), contextlib.redirect_stdout(output):
            result = main()
        self.assertNotIn("private-test-token", output.getvalue())
        return result, session, output.getvalue()

    def response(self, code, data=None):
        response = MagicMock(status_code=code)
        response.json.return_value = data
        return response

    def test_three_metadata_requests_without_redirects(self):
        metadata = {"filters": {"date_modified": {"lookup_types": ["gte", "lt"]}}, "ordering": ["id", "date_modified"]}
        result, session, output = self.run_probe([
            self.response(200, {"current_usage": []}),
            self.response(200, metadata), self.response(200, metadata),
        ])
        self.assertEqual(result, 0)
        self.assertEqual(session.request.call_count, 3)
        self.assertEqual([call.args[0] for call in session.request.call_args_list], ["GET", "OPTIONS", "OPTIONS"])
        for call in session.request.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertIn("date_modified", output)

    def test_throttle_stops_without_retry(self):
        result, session, _ = self.run_probe([self.response(429)])
        self.assertEqual(result, 1)
        self.assertEqual(session.request.call_count, 1)

    def test_missing_token_sends_no_requests(self):
        result, session, _ = self.run_probe([], token="")
        self.assertEqual(result, 1)
        session.request.assert_not_called()

    def test_raw_error_body_not_printed(self):
        response = self.response(403)
        response.text = "private-test-token"
        result, session, _ = self.run_probe([response] * 3)
        self.assertEqual(result, 1)
        self.assertEqual(session.request.call_count, 3)
