"""Expert-case matching: parsing, quote verification, tier rules, ranking and case discovery."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from src.llm.output import InvalidOutput
from src.matching import discover, profiles
from src.matching import match as matching
from src.matching.report import render

CV_PAGES = ["Jane Doe\nDirector, Fraud Detection Engineering (2012 - 2020)\n Led chargeback analytics",
            "Expert witness: Testimony & report in Acme v. Globex"]
CASE_PAGES = ["Acme sues Globex.", "The Globex App performs each step of claim 1 using a fraud score.",
              "Respectfully submitted, John Roe, Roe LLP, Attorneys for Acme"]


def expert():
    raw = json.dumps({"name": "Jane Doe", "headline": "Payments fraud engineer.", "domains": ["Fintech & Payments", "Bogus"],
                      "items": [{"kind": "role", "summary": "Led fraud detection.", "quote": "Director, Fraud Detection Engineering", "page": 1},
                                {"kind": "testimony", "summary": "Testified.", "quote": "Testimony & report in Acme v. Globex", "page": 1},
                                {"kind": "skill", "summary": "Invented.", "quote": "not in the cv", "page": 2}]})
    return profiles.verify_expert(profiles.parse_expert(raw), CV_PAGES)


def case():
    raw = json.dumps({"plaintiffs": ["Acme"], "defendants": ["Globex"], "case_type": "Patent infringement",
                      "domains": ["Fintech & Payments"], "summary": "Acme says Globex copies its fraud scoring.",
                      "technology": ["fraud scoring"],
                      "issues": [{"issue": "Does the app score fraud as claimed?", "technical": True,
                                  "quote": "performs each step of claim 1", "page": 2},
                                 {"issue": "Was infringement willful?", "technical": False, "quote": "Acme sues Globex.", "page": 1}],
                      "expertise": [{"area": "Fraud scoring", "basis": "inferred", "why": "Claims turn on scoring.",
                                     "quote": "using a fraud score", "page": 2}],
                      "counsel": [{"name": "John Roe", "firm": "Roe LLP", "party": "Acme", "quote": "John Roe, Roe LLP", "page": 3}]})
    profile, dropped = profiles.verify_case(profiles.parse_case(raw), CASE_PAGES)
    return {**profile, "pages": 3, "pages_read": 3}, dropped


def match_raw(tier="high", strength="direct", ids=("E1", "I1"), body="Dear Counsel, my work may be relevant."):
    return json.dumps({"tier": tier, "summary": "Fits.", "gaps": [],
                       "matches": [{"expert_item": ids[0], "case_point": ids[1], "strength": strength, "reason": "Because."}],
                       "email": {"to_party": "Acme", "subject": "Expert support", "body": body}})


class ProfileTests(unittest.TestCase):
    def test_expert_items_are_verified_and_numbered(self):
        profile, dropped = expert()
        self.assertEqual([i["id"] for i in profile["items"]], ["E1", "E2"])
        self.assertEqual(profile["items"][1]["page"], 2)  # quote found once on another page: page corrected
        self.assertEqual(profile["domains"], ["Fintech & Payments"])
        self.assertEqual([d["reason"] for d in dropped], ["quote not found"])

    def test_case_profile_keeps_verified_points(self):
        profile, dropped = case()
        self.assertEqual([i["id"] for i in profile["issues"]], ["I1", "I2"])
        self.assertEqual(profile["expertise"][0]["id"], "X1")
        self.assertEqual(profile["counsel"][0]["firm"], "Roe LLP")
        self.assertEqual(dropped, [])

    def test_bullets_are_cleaned(self):
        self.assertEqual(profiles.clean([" Led •  team"]), ["  Led    team"])

    def test_rejects_bad_output(self):
        for raw in ("not json", "[]", json.dumps({"name": "x", "headline": "y", "items": [{"kind": "hobby"}]})):
            with self.subTest(raw=raw), self.assertRaises(InvalidOutput):
                profiles.parse_expert(raw)


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.expert, self.case = expert()[0], case()[0]

    def enforce(self, raw):
        return matching.enforce(matching.parse_match(raw), self.expert, self.case)

    def test_verified_direct_match_keeps_high(self):
        result = self.enforce(match_raw())
        self.assertEqual(result["tier"], "high")
        self.assertEqual(result["matches"][0]["expert"]["quote"], "Director, Fraud Detection Engineering")
        self.assertIsNotNone(result["email"])

    def test_tier_is_lowered_without_support(self):
        self.assertEqual(self.enforce(match_raw(strength="related"))["tier"], "medium")
        lowered = self.enforce(match_raw(ids=("E9", "I1")))
        self.assertEqual((lowered["tier"], lowered["email"]), ("low", None))
        # A non-technical issue (willfulness) is not a technical match.
        self.assertEqual(self.enforce(match_raw(ids=("E1", "I2")))["matches"], [])

    def test_email_claiming_need_is_flagged(self):
        result = self.enforce(match_raw(body="Your case needs an expert like me."))
        self.assertTrue(any("needs an expert" in n for n in result["notes"]))

    def test_score_and_rank(self):
        high = self.enforce(match_raw())
        today = date(2026, 10, 30)
        fresh = matching.score(high, self.case, {"filed": "2026-09-30"}, today)
        self.assertEqual(fresh["parts"], {"evidence": 2, "stated": 0, "technical": 0.5, "recent": 1})
        old = matching.score(high, self.case, {"filed": "2024-01-01"}, today)
        results = [{"case": {"name": "B"}, "profile": self.case, "match": high, "score": old},
                   {"case": {"name": "C"}, "profile": None, "match": matching.insufficient("x"), "score": old},
                   {"case": {"name": "A"}, "profile": self.case, "match": high, "score": fresh}]
        self.assertEqual([r["case"]["name"] for r in matching.rank(results)], ["A", "B", "C"])
        html = render([{"profile": self.expert, "results": matching.rank(results), "discovery": None}], "now", "m")
        self.assertIn("Insufficient information", html)
        self.assertIn("Score 3.5", html)


class DiscoveryTests(unittest.TestCase):
    def test_search_terms_are_sanitized(self):
        raw = json.dumps({"searches": [{"label": "Ok", "terms": 'casino or "slot machine"'},
                                       {"label": "Bad", "terms": "x) OR description:*"},
                                       {"label": "Unbalanced", "terms": '"slot'}]})
        self.assertEqual(discover.parse_searches(raw), [{"label": "Ok", "terms": 'casino OR "slot machine"'}])
        with self.assertRaises(InvalidOutput):
            discover.parse_searches(json.dumps({"searches": [{"label": "Bad", "terms": "a:b"}]}))

    def test_candidate_from_search_row(self):
        row = {"absolute_url": "/docket/75035063/1/aristocrat-interactive-sa-rl-v-new-starbreeze-publishing-ab/",
               "filepath_local": "recap/gov.uscourts.nvd.185756/gov.uscourts.nvd.185756.1.0.pdf",
               "is_available": True, "attachment_number": None, "entry_date_filed": "2026-10-28", "description": "COMPLAINT"}
        found = discover.candidate(row)
        self.assertEqual((found["docket_id"], found["court"]), (75035063, "NVD"))
        self.assertTrue(found["name"].startswith("Aristocrat Interactive Sa Rl v. New"))
        self.assertTrue(found["has_pdf"])
        self.assertIsNone(discover.candidate({**row, "attachment_number": 2}))
        unsafe = discover.candidate({**row, "filepath_local": "../../etc/passwd"})
        self.assertEqual((unsafe["has_pdf"], unsafe["filepath"], unsafe["document_url"]), (False, None, None))

    def test_candidate_without_pdf(self):
        row = {"absolute_url": "/docket/75050001/1/clay-v-discover-products-inc/", "filepath_local": "", "is_available": False,
               "attachment_number": None, "entry_date_filed": "2026-10-30", "pacer_doc_id": "047012345",
               "description": "COMPLAINT against Discover Products Inc. (Filing fee $405 receipt number AFLMDC-25785759)"}
        found = discover.candidate(row)
        self.assertEqual((found["has_pdf"], found["court"], found["pacer_doc_id"]), (False, "FLMD", "047012345"))
        self.assertEqual(discover.pacer_cost(None), 3.0)
        self.assertEqual(discover.pacer_cost(12), 1.2)
        self.assertEqual(discover.pacer_cost(70), 3.0)

    def test_screen_unclear(self):
        raw = json.dumps({"candidates": [{"id": "C1", "relevant": "unclear", "domains": [], "case_type": "Patent", "reason": "?"},
                                         {"id": "C2", "relevant": True, "domains": [], "case_type": "Patent", "reason": "Fits."}]})
        screened = discover.parse_screen(raw)
        self.assertEqual((screened["C1"]["relevant"], screened["C1"]["unclear"]), (False, True))
        self.assertEqual((screened["C2"]["relevant"], screened["C2"]["unclear"]), (True, False))

    def test_diverse_limits_one_case_type(self):
        cases = [{"n": i, "screen": {"case_type": "Class action" if i < 5 else "Patent"}} for i in range(7)]
        self.assertEqual([c["n"] for c in discover.diverse(cases, 5)], [0, 1, 2, 5, 6])

    def test_search_uses_budget_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            session, budget = MagicMock(), MagicMock(**{"acquire.return_value": True})
            session.get.return_value = MagicMock(status_code=200, json=lambda: {"results": []})
            search = discover.CourtListenerSearch(session, budget, Path(directory), today=lambda: date(2026, 10, 30))
            self.assertEqual(search.search("casino"), ([], False))
            self.assertEqual(search.search("casino"), ([], True))
            self.assertEqual(budget.acquire.call_count, 1)
            session.get.side_effect = TimeoutError("read timed out")
            with self.assertRaises(discover.DiscoveryError):
                search.search("dice")
            budget.acquire.return_value = False
            with self.assertRaises(discover.DiscoveryError):
                search.search("poker")


if __name__ == "__main__":
    unittest.main()
