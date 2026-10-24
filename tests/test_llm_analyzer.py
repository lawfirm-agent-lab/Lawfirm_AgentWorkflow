"""LLM analyzer: schema, verification, merge policy, fallbacks, cache, budget and shadow mode."""
import json
import unittest
from datetime import datetime, timezone

from src.analyzers.llm import LLMAnalyzer, entry_text
from src.analyzers.registry import create_analyzer
from src.core.config import LLMLimits, Settings
from src.core.models import SHADOW_ANALYZER, Event
from src.llm.client import LLMError, LLMReply
from src.llm.config import LLMSettings
from src.llm.output import InvalidOutput, dates_in, parse, verify
from src.pipeline.analyze import analyze
from src.pipeline.route import latest_analysis
from src.store.pipeline import PipelineStore

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
SETTINGS = LLMSettings("openai_compatible", "test-model", "https://llm.example", "key")
ENTRY = {"entry_number": 7, "date_filed": "2026-10-22", "recap_documents": [],
         "description": "Clerk's Notes: Motion Hearing held. Court took the matter under advisement. "
                        "Response due by 11/5/2026."}


def output(**changes):
    data = {"importance": "high", "category": "scheduling", "court_issued": True,
            "summary": "The court heard the motion.", "action": "File response.",
            "deadlines": [{"date": "2026-11-05", "what": "Response", "quote": "Response due by 11/5/2026"}],
            "evidence": "Court took the matter under advisement", "confidence": 0.8}
    data.update(changes)
    return json.dumps(data)


class FakeClient:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def complete(self, messages, *, json_mode=False, max_tokens=None):
        self.calls.append((messages, json_mode, max_tokens))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMReply(reply, input_tokens=1000, output_tokens=100, cached_tokens=500, latency_ms=5)


class OutputTests(unittest.TestCase):
    def test_parse_rejects_schema_violations(self):
        self.assertEqual(parse(output())["deadlines"][0]["date"], "2026-11-05")
        for bad in ("not json", "[]", output(importance="urgent"), output(category="x"), output(confidence=2),
                    output(summary=""), output(deadlines=[{"date": "11/5/2026", "what": "x", "quote": "y"}]),
                    output(deadlines="soon")):
            with self.subTest(bad=bad), self.assertRaises(InvalidOutput) as error:
                parse(bad)
            self.assertNotIn("Clerk", str(error.exception))

    def test_dates_in_quote_formats(self):
        self.assertEqual(dates_in("by 11/5/2026, 1/6/27, 2026-12-01 and November 12th, 2026"),
                         {"2026-11-05", "2027-01-06", "2026-12-01", "2026-11-12"})
        self.assertEqual(dates_in("within 14 days; 13/45/2026"), set())

    def test_verify_drops_unsupported_claims(self):
        parsed = parse(output(evidence="The court granted the motion", deadlines=[
            {"date": "2026-11-05", "what": "ok", "quote": "Response  due by 11/5/2026"},
            {"date": "2026-11-06", "what": "wrong date", "quote": "Response due by 11/5/2026"},
            {"date": "2026-11-20", "what": "invented", "quote": "Reply due by 11/20/2026"}]))
        verified, dropped = verify(parsed, entry_text(ENTRY, 5000))
        self.assertEqual(dropped, 3)
        self.assertIsNone(verified["evidence"])
        self.assertEqual([d["date"] for d in verified["deadlines"]], ["2026-11-05"])

    def test_entry_text_truncates_document_text(self):
        payload = {**ENTRY, "recap_documents": [{"attachment_number": None, "document_number": 7,
                                                 "description": "Main", "plain_text": "word " * 10000}]}
        text = entry_text(payload, 2000)
        self.assertLessEqual(len(text), 2000)
        self.assertIn("Document 7: Main", text)
        self.assertIn("Document text (excerpt):", text)


class AnalyzerTests(unittest.TestCase):
    def make_store(self):
        return PipelineStore(":memory:")

    def setUp(self):
        self.store = self.make_store()
        self.addCleanup(self.store.close)

    def analyzer(self, *replies, **limits):
        self.client = FakeClient(*replies)
        return LLMAnalyzer(self.store, self.client, SETTINGS, LLMLimits(**limits), now=lambda: NOW)

    def event(self, payload=ENTRY, ident="e1"):
        return Event(ident, "courtlistener", 1, 7, "added", payload)

    def calls(self, status=None):
        sql = "SELECT COUNT(*) AS n FROM llm_calls" + (" WHERE status=?" if status else "")
        return self.store.execute(sql, (status,) if status else ()).fetchone()["n"]

    def test_verified_evidence_raises_importance(self):
        analyzer = self.analyzer(output())
        result = analyzer.analyze(self.event())
        self.assertEqual((result.importance, result.summary), ("high", "The court heard the motion."))
        self.assertEqual(result.details["rules"]["importance"], "low")
        self.assertTrue(result.details["llm"]["raised"])
        self.assertIn("AI rated high", result.reasons[-1])
        self.assertEqual(len(result.details["llm"]["deadlines"]), 1)
        messages, json_mode, max_tokens = self.client.calls[0]
        self.assertTrue(json_mode)
        self.assertIn("<entry>", messages[1]["content"])
        self.assertIn("json", messages[0]["content"])
        self.assertEqual(self.calls("ok"), 1)

    def test_unverified_evidence_cannot_raise(self):
        result = self.analyzer(output(evidence="Invented sentence")).analyze(self.event())
        self.assertEqual(result.importance, "low")
        self.assertFalse(result.details["llm"]["raised"])
        self.assertEqual(result.details["llm"]["dropped_claims"], 1)

    def test_model_never_lowers_rule_high(self):
        payload = {**ENTRY, "description": "ORDER setting trial for 1/6/2027."}
        result = self.analyzer(output(importance="ignore", evidence=None, deadlines=[])).analyze(self.event(payload))
        self.assertEqual(result.importance, "high")
        self.assertEqual(result.details["llm"]["importance"], "ignore")

    def test_failures_fall_back_to_rules(self):
        cases = {"failed": [LLMError("LLM provider returned HTTP 500")], "invalid": ["{\"importance\": 1}"],
                 "failed-empty": ["", "  "]}
        for name, replies in cases.items():
            with self.subTest(name=name):
                result = self.analyzer(*replies).analyze(self.event(ident=name))
                self.assertEqual(result.importance, "low")
                self.assertIsNone(result.summary)
                self.assertEqual(result.details["llm"]["status"], name.split("-")[0])

    def test_empty_reply_retried_once(self):
        analyzer = self.analyzer("", output())
        self.assertEqual(analyzer.analyze(self.event()).importance, "high")
        self.assertEqual((analyzer.stats["calls"], self.calls("failed"), self.calls("ok")), (2, 1, 1))

    def test_identical_input_uses_cache(self):
        analyzer = self.analyzer(output())
        analyzer.analyze(self.event(ident="a"))
        result = analyzer.analyze(self.event(ident="b"))
        self.assertEqual(result.importance, "high")
        self.assertEqual((analyzer.stats["calls"], analyzer.stats["cached"], self.calls("cached")), (1, 1, 1))

    def test_budgets(self):
        analyzer = self.analyzer(output(), max_calls_per_run=1)
        analyzer.analyze(self.event(ident="a"))
        other = {**ENTRY, "description": ENTRY["description"] + " More."}
        self.assertEqual(analyzer.analyze(self.event(other, "b")).details["llm"]["status"], "budget")
        analyzer = self.analyzer(output(), max_tokens_per_day=1000)
        self.assertEqual(analyzer.analyze(self.event(other, "c")).details["llm"]["status"], "budget")
        self.assertEqual(self.client.calls, [])


class ShadowAndRegistryTests(unittest.TestCase):
    ENV = {"LLM_PROVIDER": "openai_compatible", "LLM_MODEL": "m", "LLM_BASE_URL": "https://llm.example",
           "LLM_API_KEY": "k"}

    def make_store(self):
        return PipelineStore(":memory:")

    def setUp(self):
        self.store = self.make_store()
        self.addCleanup(self.store.close)
        with self.store.transaction():
            self.store.execute("INSERT INTO events(event_id,source,docket_id,entry_id,kind,payload_json,detected_at,status,updated_at) "
                               "VALUES ('e1','courtlistener',1,7,'added',?,'t','detected','t')", (json.dumps(ENTRY),))

    def test_shadow_results_never_drive_routing(self):
        analyzer = create_analyzer("rules+shadow", self.ENV, store=self.store, settings=Settings())
        analyzer.shadow.client = FakeClient(output())
        report = analyze(self.store, analyzer)
        self.assertEqual((report["analyzed"], report["llm"]["role"], report["llm"]["calls"]), (1, "shadow", 1))
        rows = {r["analyzer"]: r["importance"] for r in self.store.execute("SELECT analyzer,importance FROM analyses")}
        self.assertEqual(rows, {"rules": "low", SHADOW_ANALYZER: "high"})
        self.assertEqual(latest_analysis(self.store, "e1").importance, "low")

    def test_shadow_failure_does_not_block(self):
        analyzer = create_analyzer("rules+shadow", self.ENV, store=self.store, settings=Settings())
        analyzer.shadow.analyze = lambda event: (_ for _ in ()).throw(RuntimeError("boom"))
        report = analyze(self.store, analyzer)
        self.assertEqual((report["analyzed"], report["shadow_failed"]), (1, 1))

    def test_llm_primary_drives_routing(self):
        analyzer = create_analyzer("llm", self.ENV, store=self.store, settings=Settings())
        analyzer.client = FakeClient(output())
        analyze(self.store, analyzer)
        self.assertEqual(latest_analysis(self.store, "e1").importance, "high")


class EvalRunnerTests(unittest.TestCase):
    def test_rules_only_report_and_metrics(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import eval.run_eval as run_eval
        self.assertEqual(run_eval.scores([("high", "high"), ("high", "low"), ("low", "high")], {"high"}),
                         {"precision": 0.5, "recall": 0.5, "tp": 1, "fp": 1, "fn": 1})
        item = {"id": 1, "docket_id": 1, "entry": ENTRY, "label_status": "draft", "deadlines_complete": True,
                "label": {"importance": "high", "court_issued": True, "category": "order", "deadlines": []}}
        with tempfile.TemporaryDirectory() as directory, patch.object(run_eval, "ROOT", Path(directory)), \
                patch("builtins.print") as printed:
            labels = Path(directory) / "labeled.jsonl"
            labels.write_text(json.dumps(item) + "\n" + json.dumps({**item, "id": 2, "label_status": "excluded"}) + "\n")
            code = run_eval.main(["--labels", str(labels), "--db", str(Path(directory) / "e.db"), "--rules-only"])
            text = printed.call_args.args[0]
        self.assertEqual(code, 0)
        self.assertIn("Items: 1", text)
        self.assertIn("| rules | n/a | 0% (0/1)", text)


if __name__ == "__main__":
    unittest.main()
