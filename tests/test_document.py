"""Full-document briefing: page input, schema, page-level verification, analyzer and rendering."""
import json
import unittest
from datetime import datetime, timezone

from src.analyzers.document import DocumentAnalyzer
from src.core.config import LLMLimits
from src.core.models import Analysis, Event
from src.llm.client import LLMError, LLMReply
from src.llm.config import LLMSettings
from src.llm.document import (Pages, document_text, pages_from_extraction, pages_from_plain_text,
                              parse_document, verify_document)
from src.llm.output import InvalidOutput
from src.notify.render import render_digest
from src.store.pipeline import PipelineStore

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
SETTINGS = LLMSettings("openai_compatible", "test-model", "https://llm.example", "key")
PAGES = ["JOINT REPORT. DraftKings requests a scheduling conference. Interactive Games does not believe one is necessary.",
         "The Parties will prepare a proposed Protective Order, which will be submitted on or before July 27, 2026. "
         "The ESI protocol will be submitted on or before July 27, 2026.",
         "Trial\nOn or around\nNovember 1, 2027,\nor at the Court's convenience\nOn or around July 17,\n2028,\n"
         "Depositions for inventors of the patents-in-\nsuit"]
ENTRY = {"entry_number": 39, "date_filed": "2026-07-15", "description": "REPORT of Rule 26(f) Planning Meeting.",
         "recap_documents": [{"plain_text": "\x0c".join(PAGES) + "\x0c"}]}


def output(**changes):
    data = {"document_type": "Joint Rule 26(f) report", "overview": "The parties report their discovery plan.",
            "key_points": [{"point": "Protective order due July 27.", "quote": "proposed Protective Order, which will be submitted", "page": 2}],
            "disputes": [{"issue": "Scheduling conference", "positions": [
                {"party": "DraftKings", "position": "Wants one.", "quote": "DraftKings requests a scheduling conference", "page": 1},
                {"party": "Interactive Games", "position": "Not needed.", "quote": "does not believe one is necessary", "page": 1}]}],
            "deadlines": [{"date": "2026-07-27", "what": "Submit protective order",
                           "quote": "submitted on or before July 27, 2026", "page": 2}],
            "proposed_dates": [{"event": "Trial", "party": "Interactive Games", "date": "2027-11-01",
                                "quote": "On or around November 1, 2027", "page": 3},
                               {"event": "trial", "party": "DraftKings", "date": "2028-07-17",
                                "quote": "On or around July 17, 2028", "page": 3}],
            "action": "Prepare the protective order.", "confidence": 0.8}
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
        return LLMReply(reply, input_tokens=3000, output_tokens=900, cached_tokens=0, latency_ms=5)


class InputTests(unittest.TestCase):
    def test_pages_from_sources(self):
        self.assertEqual(pages_from_plain_text("one\x0c two \x0c\x0c"), ["one", "two"])
        self.assertEqual(pages_from_plain_text(None), [])
        self.assertEqual(pages_from_extraction({"pages": [{"page": 1, "text": "a"}, {"page": 2, "text": "b"}]}), ["a", "b"])

    def test_document_text_marks_pages_and_truncates_whole_pages(self):
        text, used, truncated = document_text(ENTRY, PAGES, 10000)
        self.assertEqual((used, truncated), (3, False))
        self.assertIn("Description: REPORT of Rule 26(f) Planning Meeting.", text)
        self.assertLess(text.index("[Page 1]"), text.index("[Page 2]"))
        text, used, truncated = document_text(ENTRY, PAGES, 400)
        self.assertEqual((used, truncated), (2, True))
        self.assertNotIn("[Page 3]", text)
        self.assertIn("[Text truncated after page 2 of 3]", text)

    def test_markers_inside_pages_are_neutralized(self):
        text, _, _ = document_text(ENTRY, ["see [Page 9] below"], 1000)
        self.assertEqual(text.count("[Page "), 1)


class OutputTests(unittest.TestCase):
    def test_parse_accepts_schema_and_cuts_long_lists(self):
        parsed = parse_document(output())
        self.assertEqual(parsed["proposed_dates"][1]["date"], "2028-07-17")
        many = [{"point": f"p{i}", "quote": "x", "page": 1} for i in range(8)]
        self.assertEqual(len(parse_document(output(key_points=many))["key_points"]), 5)

    def test_parse_rejects_schema_violations(self):
        bad_point = lambda **c: [{"point": "p", "quote": "q", "page": 1, **c}]
        for bad in ("not json", "[]", output(overview=""), output(confidence=3), output(key_points="x"),
                    output(key_points=bad_point(page=0)), output(key_points=bad_point(page=True)),
                    output(key_points=bad_point(quote="")), output(key_points=bad_point() * 16),
                    output(deadlines=[{"date": "7/27/2026", "what": "x", "quote": "y", "page": 1}]),
                    output(disputes=[{"issue": "x", "positions": []}])):
            with self.subTest(bad=bad[:60]), self.assertRaises(InvalidOutput) as error:
                parse_document(bad)
            self.assertNotIn("DraftKings", str(error.exception))

    def test_verify_keeps_quotes_on_cited_page_and_corrects_pages(self):
        parsed = parse_document(output(key_points=[
            {"point": "cited right", "quote": "proposed Protective Order", "page": 2},
            {"point": "page corrected", "quote": "Interactive Games does not believe", "page": 3},
            {"point": "hyphen break", "quote": "inventors of the patents-in-suit", "page": 3},
            {"point": "invented", "quote": "The court granted the motion", "page": 1}]))
        verified, dropped, corrected = verify_document(parsed, PAGES)
        self.assertEqual([(p["point"], p["page"]) for p in verified["key_points"]],
                         [("cited right", 2), ("page corrected", 1), ("hyphen break", 3)])
        self.assertEqual(corrected, 1)
        self.assertEqual([d["reason"] for d in dropped], ["quote not found"])
        # Table cells read in order: both sides' trial dates verify.
        self.assertEqual([d["date"] for d in verified["proposed_dates"]], ["2027-11-01", "2028-07-17"])

    def test_verify_dates_and_repeated_quotes(self):
        parsed = parse_document(output(deadlines=[
            {"date": "2026-07-27", "what": "repeats on its page", "quote": "on or before July 27, 2026", "page": 2},
            {"date": "2026-07-28", "what": "wrong date", "quote": "on or before July 27, 2026", "page": 2},
            {"date": "2026-07-27", "what": "repeats elsewhere", "quote": "on or before July 27, 2026", "page": 1}]))
        verified, dropped, _ = verify_document(parsed, PAGES)
        self.assertEqual([d["what"] for d in verified["deadlines"]], ["repeats on its page"])
        self.assertEqual([d["reason"] for d in dropped], ["date not in quote", "quote on other pages, more than once"])

    def test_dispute_without_verified_positions_is_dropped(self):
        parsed = parse_document(output(disputes=[{"issue": "x", "positions": [
            {"party": "A", "position": "p", "quote": "invented", "page": 1}]}]))
        verified, dropped, _ = verify_document(parsed, PAGES)
        self.assertEqual((verified["disputes"], len(dropped)), ([], 1))

    def test_pages_locate(self):
        index = Pages(["alpha beta", "beta gamma"])
        self.assertEqual(index.locate("gamma"), (2, None))
        self.assertEqual(index.locate("beta", 2), (2, None))
        self.assertEqual(index.locate("beta", 3), (None, "quote on other pages, more than once"))
        self.assertEqual(index.locate("delta", 1), (None, "quote not found"))


class AnalyzerTests(unittest.TestCase):
    def make_store(self):
        return PipelineStore(":memory:")

    def setUp(self):
        self.store = self.make_store()
        self.addCleanup(self.store.close)

    def analyzer(self, *replies, settings=SETTINGS, **limits):
        self.client = FakeClient(*replies)
        return DocumentAnalyzer(self.store, self.client, settings, LLMLimits(**limits), now=lambda: NOW)

    def event(self):
        return Event("e39", "courtlistener", 1, 39, "added", ENTRY)

    def test_briefing_is_verified_and_audited(self):
        analyzer = self.analyzer(output(), max_document_output_tokens=1234)
        result = analyzer.analyze(self.event(), PAGES)
        self.assertEqual((result["status"], result["pages"], result["pages_read"], result["truncated"]), ("ok", 3, 3, False))
        self.assertEqual(result["dropped_claims"], 0)
        self.assertEqual(len(result["proposed_dates"]), 2)
        messages, json_mode, max_tokens = self.client.calls[0]
        self.assertTrue(json_mode)
        self.assertEqual(max_tokens, 1234)
        self.assertIn("<document>", messages[1]["content"])
        self.assertIn("[Page 3]", messages[1]["content"])
        row = self.store.execute("SELECT prompt_version, status FROM llm_calls").fetchone()
        self.assertEqual((row["prompt_version"], row["status"]), ("document_v1", "ok"))

    def test_failures_return_status_without_claims(self):
        for name, replies in {"failed": [LLMError("HTTP 500")], "invalid": ["{}"]}.items():
            with self.subTest(name=name):
                result = self.analyzer(*replies).analyze(self.event(), PAGES)
                self.assertEqual(result["status"], name)
                self.assertNotIn("key_points", result)

    def test_budget_and_cache(self):
        self.assertEqual(self.analyzer(max_tokens_per_day=10).analyze(self.event(), PAGES)["status"], "budget")
        self.analyzer(output()).analyze(self.event(), PAGES)
        analyzer = self.analyzer()
        self.assertEqual(analyzer.analyze(self.event(), PAGES)["status"], "ok")
        self.assertEqual(analyzer.stats["cached"], 1)

    def test_provider_options_are_part_of_the_cache_key(self):
        self.analyzer(output()).analyze(self.event(), PAGES)
        thinking = LLMSettings("openai_compatible", "test-model", "https://llm.example", "key",
                               extra_body={"thinking": {"type": "enabled"}})
        analyzer = self.analyzer(output(), settings=thinking)
        analyzer.analyze(self.event(), PAGES)
        self.assertEqual((analyzer.stats["calls"], analyzer.stats["cached"]), (1, 0))


class RenderTests(unittest.TestCase):
    def briefing(self, **changes):
        parsed = parse_document(output(**changes))
        verified, dropped, corrected = verify_document(parsed, PAGES)
        return {"status": "ok", "pages": 3, "pages_read": 3, "truncated": False, **verified}

    def render(self, document, entry_deadlines=()):
        details = {"llm": {"status": "ok", "deadlines": list(entry_deadlines), "action": "Entry action."},
                   "document": document}
        item = (Event("e39", "courtlistener", 1, 39, "added", {"entry_number": 39, "date_filed": "2026-07-15",
                                                                "description": "REPORT", "recap_documents": []}),
                Analysis("medium", "other", "Entry summary.", (), details))
        return render_digest([item], {}, NOW)

    def test_document_briefing_in_both_views(self):
        _, text, rich = self.render(self.briefing(), [
            {"date": "2026-07-27", "what": "dup", "quote": "x"}, {"date": "2026-08-01", "what": "entry only", "quote": "y"}])
        self.assertIn("AI summary (Joint Rule 26(f) report): The parties report their discovery plan.", text)
        self.assertNotIn("Entry summary.", text)
        self.assertIn("    • Protective order due July 27. (p. 2)", text)
        self.assertIn("        DraftKings: Wants one. (p. 1)", text)
        self.assertIn("    • Trial — Interactive Games: Nov 1, 2027 · DraftKings: Jul 17, 2028 (p. 3)", text)
        self.assertIn("Deadline: Jul 27, 2026 — Submit protective order", text)
        self.assertNotIn("— dup", text)
        self.assertIn("— entry only", text)
        self.assertIn("Action: Prepare the protective order.", text)
        self.assertIn("appears on the page shown (p.)", text)
        self.assertIn("<b>Proposed dates</b>", rich)
        self.assertIn("p. 3", rich)

    def test_document_text_is_escaped_and_truncation_noted(self):
        document = {**self.briefing(overview="<script>x</script> & more"), "truncated": True, "pages_read": 2, "pages": 9}
        _, text, rich = self.render(document)
        self.assertNotIn("<script>", rich)
        self.assertIn("&lt;script&gt;", rich)
        self.assertIn("Read pages 1–2 of 9; the rest was not analyzed.", text)

    def test_failed_briefing_falls_back_to_entry_reading(self):
        _, text, _ = self.render({"status": "failed"})
        self.assertIn("AI summary: Entry summary.", text)
        self.assertIn("Action: Entry action.", text)
        self.assertNotIn("page shown (p.)", text)


if __name__ == "__main__":
    unittest.main()
