"""Pipeline stages end to end with a fake source and transport; no network or SMTP."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from src.admin import add_recipient, listing, set_active, subscribe
from src.analyzers.rules import RulesAnalyzer
from src.core.config import Settings
from src.core.models import SourceResult
from src.notifiers.email import EmailNotifier
from src.pipeline import detect as detect_module
from src.pipeline.runner import run
from src.sources.courtlistener import QuotaExhausted, SourceError
from src.store.db import Store
from src.store.pipeline import PipelineStore

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
DOCKET = 73132799


def text(message):
    return message.get_body(("plain",)).get_content()


def entry(i, description="ORDER setting hearing.", docket=DOCKET, modified="2026-10-23T11:00:00+00:00"):
    return {"id": i, "docket": f"https://www.courtlistener.com/api/rest/v4/dockets/{docket}/", "entry_number": i,
            "description": description, "date_filed": "2026-10-23", "date_modified": modified, "recap_documents": []}


class FakeSource:
    name = "courtlistener"

    def __init__(self):
        self.pages, self.calls, self.info_calls, self.court_calls = {}, [], [], []

    def fetch_docket_info(self, docket_id):
        self.info_calls.append(docket_id)
        return {"case_name": f"Case {docket_id}", "docket_number": "1:25-cv-00001", "court": "NYSD",
                "court_id": "nysd", "absolute_url": f"/docket/{docket_id}/case/"}

    def fetch_court_info(self, court_id):
        self.court_calls.append(court_id)
        return {"citation": "S.D.N.Y.", "full_name": "District Court, S.D. New York"}

    def fetch_docket_changes(self, docket_id, since, until):
        self.calls.append((docket_id, since, until))
        result = self.pages.get(docket_id, [])
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, SourceResult) else SourceResult(result, True, until)


class PipelineTests(unittest.TestCase):
    def make_store(self):
        return PipelineStore(":memory:")

    def setUp(self):
        self.store = self.make_store()
        self.addCleanup(self.store.close)
        self.source, self.sent = FakeSource(), []
        self.settings = Settings()
        self.transport = self.sent.append
        add_recipient(self.store, "Lawyer@Example.com", "A. Lawyer")
        subscribe(self.store, "lawyer@example.com", DOCKET)

    def run_all(self, now=NOW, stages=("detect", "analyze", "route", "deliver"), dry_run=False):
        notifier = EmailNotifier(self.store, {"EMAIL_FROM": "monitor@example.com"}, self.transport)
        detect_report = None
        report = {"stages": {}}
        if "detect" in stages:
            detect_report = detect_module.detect(self.store, self.source, self.settings, now)
            stages = [s for s in stages if s != "detect"]
        if stages:
            report = run(self.store, stages, self.settings, dry_run=dry_run, notifier=notifier,
                         analyzer=RulesAnalyzer(), now=now)
        return detect_report, report

    def write(self, sql, params=()):
        with self.store.transaction():
            self.store.execute(sql, params)

    def count(self, table, where="1=1", params=()):
        return self.store.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params).fetchone()["n"]

    def test_new_watch_starts_now_without_backfill(self):
        report, _ = self.run_all()
        watch = self.store.execute("SELECT * FROM docket_watch").fetchone()
        self.assertEqual(watch["cursor_at"], NOW.isoformat())
        self.assertEqual(self.source.calls, [])
        self.assertEqual(report["events"], 0)

    def test_watch_resumes_from_last_legacy_success(self):
        legacy_start = "2026-10-23T08:00:00+00:00"
        self.write("INSERT INTO runs(docket_id,source,started_at,status) VALUES (?,?,?,'success')",
                           (DOCKET, "api", legacy_start))
        self.run_all()
        self.assertEqual(self.source.calls[0][1], legacy_start)
        self.assertEqual(self.source.calls[0][2], (NOW - timedelta(seconds=120)).isoformat())

    def start_watch(self, cursor="2026-10-23T10:00:00+00:00"):
        self.write("INSERT INTO docket_watch(docket_id,cursor_at,created_at) VALUES (?,?,?)",
                           (DOCKET, cursor, cursor))

    def test_detect_analyze_route_deliver_once(self):
        self.start_watch()
        self.write("INSERT INTO entries VALUES (?,?,?,?)", (DOCKET, 1, "{}", "old-signature"))
        self.source.pages[DOCKET] = [entry(1, "ORDER amended."), entry(2), entry(3, "MOTION to dismiss by defendant.")]
        detected, report = self.run_all()
        self.assertEqual(detected["events"], 3)
        kinds = {r["entry_id"]: r["kind"] for r in self.store.execute("SELECT entry_id,kind FROM events")}
        self.assertEqual(kinds, {1: "modified", 2: "added", 3: "added"})
        self.assertEqual(report["stages"]["route"], {"routed": 2, "skipped": 1, "notifications": 2, "status": "ok"})
        self.assertEqual(report["stages"]["deliver"]["sent"], 1)
        self.assertEqual(len(self.sent), 1)
        message = self.sent[0]
        self.assertEqual(message["To"], "lawyer@example.com")
        self.assertTrue(message["Subject"].startswith(f"[2 priority items] Case {DOCKET} — Oct 23"))
        body = text(message)
        self.assertIn("ORDER amended.", body)
        self.assertIn("UPDATED · description changed", body)
        self.assertIn(f"https://www.courtlistener.com/docket/{DOCKET}/case/?entry_gte=2&entry_lte=2", body)
        self.assertIn("1:25-cv-00001 · S.D.N.Y.", body)
        self.assertNotIn("MOTION to dismiss", body)
        self.assertIn("Not legal advice", body)
        self.assertIn("ORDER amended.", message.get_body(("html",)).get_content())
        self.assertEqual(self.source.info_calls, [DOCKET])
        # Replaying the same page and rerunning creates no events and sends nothing new.
        detected, report = self.run_all(NOW + timedelta(hours=2))
        self.assertEqual(detected["events"], 0)
        self.assertEqual(report["stages"]["deliver"]["digests"], 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.count("notifications", "status='sent'"), 2)

    def test_docket_info_fetched_once_and_failures_are_soft(self):
        self.start_watch()
        self.run_all(stages=("detect",))
        self.run_all(NOW + timedelta(hours=2), stages=("detect",))
        self.assertEqual((self.source.info_calls, self.source.court_calls), ([DOCKET], ["nysd"]))
        subscribe(self.store, "lawyer@example.com", 2)
        self.source.fetch_court_info = lambda court_id: self.fail("court already cached")
        self.source.fetch_docket_info = lambda docket_id: (_ for _ in ()).throw(SourceError("HTTP 404"))
        report, _ = self.run_all(NOW + timedelta(hours=4), stages=("detect",))
        self.assertEqual(report["info_failed"], [{"docket_id": 2, "error": "HTTP 404"}])
        self.assertEqual(report["failed"], [])

    def test_entry_without_description_is_reported_once_complete(self):
        self.start_watch()
        self.source.pages[DOCKET] = [entry(1, description="")]
        report, _ = self.run_all(stages=("detect",))
        self.assertEqual((report["events"], report["awaiting_description"], self.count("entries")), (0, 1, 0))
        self.source.pages[DOCKET] = [entry(1, "ORDER setting trial.", modified="2026-10-23T11:30:00+00:00")]
        report, _ = self.run_all(NOW + timedelta(hours=1), stages=("detect",))
        self.assertEqual(report["events"], 1)
        self.assertEqual(self.store.execute("SELECT kind FROM events").fetchone()["kind"], "added")

    def test_digest_reports_items_left_for_next_run(self):
        from dataclasses import replace
        self.start_watch()
        subscribe(self.store, "lawyer@example.com", DOCKET, "low")
        self.source.pages[DOCKET] = [entry(i, "Notice of appearance.") for i in range(1, 6)]
        self.settings = replace(self.settings, max_events_per_digest=3)
        self.run_all()
        self.assertIn("2 more in the next digest", text(self.sent[0]))
        self.run_all(NOW + timedelta(hours=2), stages=("deliver",))
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("more in the next digest", text(self.sent[1]))

    def test_cursor_advances_only_after_commit(self):
        self.start_watch()
        bad = entry(2)
        bad["docket"] = "https://www.courtlistener.com/api/rest/v4/dockets/1/"
        self.source.pages[DOCKET] = [entry(1), bad]
        report, _ = self.run_all(stages=("detect",))
        self.assertEqual(report["failed"][0]["docket_id"], DOCKET)
        watch = self.store.execute("SELECT * FROM docket_watch").fetchone()
        self.assertEqual(watch["cursor_at"], "2026-10-23T10:00:00+00:00")
        self.assertIn("another docket", watch["last_error"])
        self.assertEqual((self.count("events"), self.count("entries")), (0, 0))
        self.source.pages[DOCKET] = [entry(1)]
        report, _ = self.run_all(stages=("detect",))
        self.assertEqual(report["events"], 1)
        watch = self.store.execute("SELECT * FROM docket_watch").fetchone()
        self.assertEqual(watch["cursor_at"], (NOW - timedelta(seconds=120)).isoformat())
        self.assertIsNone(watch["last_error"])

    def test_partial_result_keeps_last_success_and_moves_cursor(self):
        self.start_watch()
        self.source.pages[DOCKET] = SourceResult([entry(1)], False, "2026-10-23T11:00:00+00:00")
        report, _ = self.run_all(stages=("detect",))
        self.assertEqual(report["partial"], [DOCKET])
        watch = self.store.execute("SELECT * FROM docket_watch").fetchone()
        self.assertEqual(watch["cursor_at"], "2026-10-23T11:00:00+00:00")
        self.assertIsNone(watch["last_success_at"])

    def test_quota_stops_remaining_dockets(self):
        subscribe(self.store, "lawyer@example.com", 2)
        self.write("INSERT INTO docket_watch(docket_id,cursor_at,created_at) VALUES (2,?,?)",
                           ("2026-10-23T10:00:00+00:00", "x"))
        self.start_watch()
        self.source.pages[2] = QuotaExhausted("budget")
        report, _ = self.run_all(stages=("detect",))
        self.assertTrue(report["quota_stopped"])
        self.assertEqual(len(self.source.calls), 1)

    def test_content_flip_back_is_a_new_event(self):
        self.start_watch()
        for hour, text in enumerate(("ORDER one.", "ORDER two.", "ORDER one.")):
            self.source.pages[DOCKET] = [entry(1, text)]
            self.run_all(NOW + timedelta(hours=hour), stages=("detect",))
        self.assertEqual(self.count("events"), 3)

    def test_index_only_change_is_ignored(self):
        self.start_watch()
        self.source.pages[DOCKET] = [entry(1)]
        self.run_all(stages=("detect",))
        self.source.pages[DOCKET] = [entry(1, modified="2026-10-23T11:30:00+00:00")]
        report, _ = self.run_all(NOW + timedelta(hours=1), stages=("detect",))
        self.assertEqual((report["dockets"], report["events"]), (1, 0))

    def test_min_importance_and_inactive_recipients(self):
        self.start_watch()
        add_recipient(self.store, "all@example.com")
        subscribe(self.store, "all@example.com", DOCKET, "low")
        add_recipient(self.store, "gone@example.com")
        subscribe(self.store, "gone@example.com", DOCKET, "low")
        set_active(self.store, "gone@example.com", None, False)
        self.source.pages[DOCKET] = [entry(1), entry(2, "Certificate of service.")]
        self.run_all()
        self.assertEqual(sorted(m["To"] for m in self.sent), ["all@example.com", "lawyer@example.com"])
        counts = {m["To"]: text(m).count(" · NEW") for m in self.sent}
        self.assertEqual(counts, {"all@example.com": 2, "lawyer@example.com": 1})
        by_recipient = {m["To"]: text(m) for m in self.sent}
        self.assertIn("OTHER ACTIVITY", by_recipient["all@example.com"])
        self.assertNotIn("OTHER ACTIVITY", by_recipient["lawyer@example.com"])

    def test_uncertain_delivery_is_never_retried(self):
        self.start_watch()
        self.source.pages[DOCKET] = [entry(1)]
        attempts = []

        def fail(message):
            attempts.append(message)
            raise TimeoutError("synthetic")
        self.transport = fail
        _, report = self.run_all()
        self.assertEqual(report["stages"]["deliver"]["needs_review"], 1)
        self.assertEqual(report["status"], "failed")
        _, report = self.run_all(NOW + timedelta(hours=2))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.count("notifications", "status='needs_review'"), 1)

    def test_dry_run_changes_nothing(self):
        self.start_watch()
        self.source.pages[DOCKET] = [entry(1)]
        _, report = self.run_all(dry_run=True)
        self.assertEqual(report["stages"]["deliver"]["previews"][0]["items"], 1)
        self.assertNotIn("lawyer@example.com", json.dumps(report))
        self.assertEqual((self.sent, self.count("notifications", "status='pending'")), ([], 1))
        self.assertEqual(self.count("email_outbox"), 0)

    def test_analyzer_failure_retries_then_fails(self):
        self.start_watch()
        self.source.pages[DOCKET] = [entry(1)]
        self.run_all(stages=("detect",))

        class Broken:
            name, version = "broken", "0"

            def analyze(self, event):
                raise RuntimeError("provider said: secret prompt")
        for _ in range(3):
            report = run(self.store, ["analyze"], self.settings, analyzer=Broken(), now=NOW)
        self.assertEqual(report["stages"]["analyze"]["failed"], 1)
        row = self.store.execute("SELECT status,attempts,last_error FROM events").fetchone()
        self.assertEqual((row["status"], row["attempts"], row["last_error"]), ("failed", 3, "RuntimeError"))

    def test_stage_failure_does_not_block_later_stages(self):
        report = run(self.store, ["detect", "route"], self.settings, session=None, now=NOW,
                     budget=type("B", (), {"used": 0})())
        self.assertEqual(report["stages"]["route"]["status"], "ok")
        self.assertEqual(self.count("pipeline_runs", "status=?", (report["status"],)), 1)

    def test_admin_listing(self):
        text = listing(self.store)
        self.assertIn("lawyer@example.com (A. Lawyer) active", text)
        self.assertIn(f"docket {DOCKET} min_importance=medium active", text)
        with self.assertRaises(ValueError):
            subscribe(self.store, "nobody@example.com", DOCKET)
        with self.assertRaises(ValueError):
            add_recipient(self.store, "a@example.com, b@example.com")


from src.store.migrate import MIGRATIONS


class MigrationTests(unittest.TestCase):
    def test_court_migration_backfills_existing_rows(self):
        import shutil
        import tempfile
        from pathlib import Path
        from src.store.migrate import MIGRATIONS, apply_migrations
        store = Store(":memory:")
        self.addCleanup(store.close)
        with tempfile.TemporaryDirectory() as directory:
            for name in ("001_pipeline.sql", "002_docket_info.sql"):
                shutil.copy(MIGRATIONS / name, Path(directory) / name)
            apply_migrations(store, directory)
        with store.transaction():
            store.execute("INSERT INTO docket_info VALUES (1,'A v. B','1:26-cv-1','NYSD','/docket/1/a/','t')")
        self.assertEqual(apply_migrations(store)[0], "003_court_info")
        self.assertEqual(store.execute("SELECT court_id FROM docket_info").fetchone()["court_id"], "nysd")

    def test_reopen_is_idempotent_and_keeps_legacy_data(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            legacy = Store(path)
            legacy.apply(legacy.start(DOCKET), [entry(1)])
            legacy.close()
            for _ in range(2):
                store = PipelineStore(path)
                versions = [r["version"] for r in store.execute("SELECT version FROM schema_migrations")]
                self.assertEqual(versions, sorted(p.stem for p in MIGRATIONS.glob("*.sql")))
                self.assertEqual(store.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"], 1)
                store.close()


if __name__ == "__main__":
    unittest.main()
