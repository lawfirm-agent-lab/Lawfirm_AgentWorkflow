"""Runs for CVs uploaded on the results website, with a SQLite file standing in for the cloud database."""
import base64
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.matching.cloud import CloudRuns, open_store
from src.web import request
from tests.test_web_daily import ENV


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = self.root / "cloud.db"
        self.store = open_store({}, self.db)
        self.cloud = CloudRuns(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def queue(self, request_id="r1", save=0):
        with self.store.transaction():
            self.store.execute("INSERT INTO matching_requests (request_id, cv_name, pdf_base64, save, state, created_at, "
                               "updated_at, owner) VALUES (?,?,?,?,'queued','t','t','jane')",
                               (request_id, "jane.pdf", base64.b64encode(b"%PDF-1.4 cv").decode(), save))

    def run_request(self, request_id="r1", fail=False):
        seen = {}

        def fake_run(model, cv_paths, cases, base, limit, log, search, *args, **kwargs):
            seen["cv"] = Path(cv_paths[0]).read_bytes()
            seen["cases"], seen["find"] = cases, search is not None
            log({"stage": "cv", "label": "CV extraction", "detail": "", "cached": False})
            if fail:
                raise RuntimeError("model broke")
            return [{"profile": {"name": "Jane"}, "results": [], "discovery": {"candidates": []}}]

        model = MagicMock(trace=[], stats={"calls": 0})
        with patch.dict("os.environ", ENV), patch("src.web.jobs.Model", return_value=model), \
                patch("src.web.jobs.make_search") as search, patch("src.web.jobs.run", side_effect=fake_run), \
                redirect_stdout(io.StringIO()):
            code = request.main(["--id", request_id, "--sqlite", str(self.db)], data_dir=self.root / "runner")
        return code, seen, search

    def test_uploaded_cv_runs_once_and_is_saved_like_a_check(self):
        self.queue()
        code, seen, search = self.run_request()
        self.assertEqual(code, 0)
        self.assertEqual(seen["cv"], b"%PDF-1.4 cv")
        self.assertEqual((seen["cases"], seen["find"]), ([], True))  # A year of CourtListener, no fixed pool.
        self.assertEqual(search.call_args.args[2], 365)
        row = self.cloud.request("r1")
        self.assertEqual((row["state"], row["progress"], row["pdf_base64"]), ("done", "Finished", None))
        self.assertEqual([r["run_id"] for r in self.cloud.runs()], [row["run_id"]])
        owner = self.store.execute("SELECT owner FROM matching_runs").fetchone()["owner"]
        self.assertEqual(owner, "jane")  # The run belongs to whoever uploaded the CV.
        self.assertEqual(self.run_request()[0], 1)  # Not queued any more: not run again.

    def test_saved_cv_is_kept_and_failure_is_reported(self):
        self.queue(save=1)
        code, _, _ = self.run_request(fail=True)
        row = self.cloud.request("r1")
        self.assertEqual((code, row["state"]), (1, "failed"))
        self.assertIn("model broke", row["error"])
        self.assertIsNotNone(row["pdf_base64"])

    def test_cleanup_marks_an_unfinished_request_failed(self):
        self.queue()
        with redirect_stdout(io.StringIO()):
            request.main(["--id", "r1", "--sqlite", str(self.db), "--mark-failed", "stopped"])
        row = self.cloud.request("r1")
        self.assertEqual((row["state"], row["error"], row["pdf_base64"]), ("failed", "stopped", None))


if __name__ == "__main__":
    unittest.main()
