"""Cloud copy of saved CVs and daily checks, with a SQLite file standing in for the cloud database."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.matching.cloud import CloudRuns, open_store
from src.web import daily
from src.web.app import create_app
from src.web.jobs import Jobs
from tests.test_web_daily import ENV, daily_run


class CloudTests(unittest.TestCase):
    def open(self):
        return open_store({}, self.db)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = self.root / "cloud.db"
        self.store = self.open()
        self.cloud = CloudRuns(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_saved_cv_round_trip(self):
        self.cloud.save_cv("jane.pdf", b"%PDF-1.4 old")
        self.cloud.save_cv("jane.pdf", b"%PDF-1.4 new")  # Saving again replaces it.
        self.cloud.save_cv("../escape.pdf", b"%PDF-1.4")
        self.assertEqual(self.cloud.write_cvs(self.root / "cvs"), ["jane.pdf"])
        self.assertEqual((self.root / "cvs" / "jane.pdf").read_bytes(), b"%PDF-1.4 new")
        self.assertFalse((self.root / "escape.pdf").exists())

    def test_runs_copied_both_ways_and_shown_cases_follow(self):
        laptop, runner = self.root / "laptop", self.root / "runner"
        daily_run(runner, "20261030-090000-aaaaaa", "jane.pdf", "2026-10-30T16:00:00+00:00",
                  ["https://cl/docket/1/", "https://cl/docket/2/"])
        self.cloud.save_run(runner / "20261030-090000-aaaaaa")
        self.cloud.save_run(runner / "20261030-090000-aaaaaa")  # Saving again is harmless.
        self.assertEqual(self.cloud.pull(laptop), 1)
        self.assertEqual(self.cloud.pull(laptop), 0)  # Runs already here are not copied again.
        jobs = Jobs(laptop, None, [], self.root, 1000, self.root, ENV)
        self.assertEqual(jobs.shown("jane.pdf"), {"https://cl/docket/1/", "https://cl/docket/2/"})

    def test_today_page_shows_cloud_checks(self):
        daily_run(self.root / "runner", "20261030-090000-aaaaaa", "jane.pdf", "2026-10-30T16:00:00+00:00",
                  ["https://cl/docket/1/"])
        self.cloud.save_run(self.root / "runner" / "20261030-090000-aaaaaa")
        cloud = CloudRuns(connect=self.open)
        page = create_app(ENV, data_dir=self.root / "laptop", cloud=cloud).test_client().get("/today")
        text = page.get_data(as_text=True)
        self.assertIn("Strong fit.", text)
        self.assertIn("1 new copied just now", text)


class CloudDailyTests(CloudTests):
    """The daily command against a SQLite file (--sqlite); not rerun on PostgreSQL."""

    def test_cloud_daily_check_reads_cvs_and_saves_runs(self):
        self.cloud.save_cv("jane.pdf", b"%PDF-1.4")
        data = self.root / "runner"

        def fake_run(model, cv_paths, cases, *args, skip=(), **kwargs):
            return [{"profile": {"name": "Jane"}, "results": [], "discovery": {"candidates": [], "skipped": len(skip)}}]

        model = MagicMock(trace=[], stats={"calls": 0})
        with patch.dict("os.environ", ENV), patch("src.web.jobs.Model", return_value=model), \
                patch("src.web.jobs.make_search") as search, patch("src.web.jobs.run", side_effect=fake_run), \
                redirect_stdout(io.StringIO()) as out:
            code = daily.main(["--cloud", "--sqlite", str(self.db), "--days", "2"], data_dir=data)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIs(search.call_args.args[3].backend, "sqlite")  # Requests count in the cloud ledger.
        runs = self.cloud.runs()
        self.assertEqual([(r["cv_name"], r["state"]) for r in runs], [("jane.pdf", "done")])

    def test_cloud_daily_check_without_cvs_fails(self):
        with redirect_stdout(io.StringIO()) as out:
            code = daily.main(["--cloud", "--sqlite", str(self.db)], data_dir=self.root / "runner")
        self.assertEqual(code, 1)
        self.assertIn("No saved CV in the cloud database", out.getvalue())


if __name__ == "__main__":
    unittest.main()
