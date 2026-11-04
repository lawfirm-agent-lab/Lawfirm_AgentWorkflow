"""Search agent: verification, safe fetching, the pick step and how found complaints join the run."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.llm.output import InvalidOutput
from src.matching import elsewhere
from src.matching.run import find_cases

CASE = {"name": "Federal Trade Commission v. Digital Forensics Corporation LLC", "filed": "2026-10-06",
        "docket_id": 71234567, "court": "", "description": "Complaint for permanent injunction"}
COMPLAINT = ("UNITED STATES DISTRICT COURT Case 1:26-cv-04321 Document 1 FEDERAL TRADE COMMISSION, Plaintiff, v. "
             "DIGITAL FORENSICS CORPORATION, LLC, Defendant. COMPLAINT FOR PERMANENT INJUNCTION")
PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]
PRIVATE = [(2, 1, 6, "", ("10.0.0.5", 443))]


def response(status=200, body=b"", kind="text/html", location=None):
    headers = {"Content-Type": kind, **({"Location": location} if location else {})}
    return MagicMock(status_code=status, headers=headers, iter_content=lambda size: [body])


class VerifyTests(unittest.TestCase):
    def test_parties_and_docket_number(self):
        self.assertEqual(elsewhere.parties(CASE["name"]), [["federal", "trade", "commission"], ["digital", "forensics"]])
        self.assertEqual(elsewhere.number_key("1:26-cv-04321-JMF"), ("26", "cv", "4321"))
        case = {**CASE, "docket_number": "1:26-cv-04321"}
        self.assertEqual(elsewhere.verify(COMPLAINT, case), {"ok": True, "by": "docket number and parties"})
        # Without the docket number the filing year must appear.
        self.assertFalse(elsewhere.verify(COMPLAINT, CASE)["ok"])
        self.assertEqual(elsewhere.verify(COMPLAINT + " Dated: October 6, 2026", CASE)["by"], "parties and filing year")

    def test_rejects_a_page_about_only_one_party_or_another_case(self):
        case = {**CASE, "docket_number": "1:26-cv-04321"}
        self.assertFalse(elsewhere.verify("Digital Forensics Corporation reviews and pricing 2026", case)["ok"])
        other = COMPLAINT.replace("04321", "00777").replace("FEDERAL TRADE COMMISSION", "JOHN SMITH")
        self.assertFalse(elsewhere.verify(other, case)["ok"])

    def test_a_side_without_distinctive_words_is_skipped(self):
        case = {"name": "United States v. Globex Payments Inc", "filed": "2026-10-01", "docket_number": "3:26-cr-00012"}
        self.assertTrue(elsewhere.verify("U.S. v. GLOBEX PAYMENTS, INC. No. 3:26-cr-12", case)["ok"])

    def test_html_text_and_links(self):
        text, links = elsewhere.html_page(b"<p>FTC sues <b>Digital Forensics</b></p><script>alert(1)</script>"
                                          b"<a href='/files/complaint.pdf'>Complaint</a>")
        self.assertEqual(text, "FTC sues Digital Forensics Complaint")
        self.assertEqual(links, [("/files/complaint.pdf", "Complaint")])


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.session = MagicMock()
        self.web = elsewhere.WebSearch(self.session, "tvly-test", self.dir, today=lambda: date(2026, 11, 4))

    def test_search_sends_the_key_as_a_header_and_caches_the_day(self):
        self.session.post.return_value = MagicMock(status_code=200, json=lambda: {"results": [
            {"title": "FTC Sues  Digital Forensics", "url": "https://www.ftc.gov/news/x", "content": "The FTC filed..."},
            {"title": "no url"}]})
        rows, cached = self.web.search('"Digital Forensics Corporation LLC" complaint')
        self.assertEqual((rows[0]["title"], len(rows), cached), ("FTC Sues Digital Forensics", 1, False))
        call = self.session.post.call_args
        self.assertEqual(call.kwargs["headers"], {"Authorization": "Bearer tvly-test"})
        self.assertNotIn("tvly-test", json.dumps(call.kwargs["json"]))
        self.assertEqual(self.web.search('"Digital Forensics Corporation LLC" complaint'), (rows, True))
        self.assertEqual(self.session.post.call_count, 1)
        self.session.post.return_value = MagicMock(status_code=401)
        with self.assertRaisesRegex(elsewhere.ElsewhereError, "HTTP 401"):
            self.web.search("other")

    def test_only_public_addresses(self):
        for url in ("file:///etc/passwd", "http://user:pw@example.com/", "ftp://example.com/a.pdf"):
            with self.subTest(url=url), self.assertRaises(elsewhere.ElsewhereError):
                elsewhere.public(url)
        with patch("socket.getaddrinfo", return_value=PRIVATE), self.assertRaises(elsewhere.ElsewhereError):
            elsewhere.public("https://intranet.example/")

    def test_redirects_are_checked_and_files_limited(self):
        with patch("socket.getaddrinfo", side_effect=[PUBLIC, PRIVATE]):
            self.session.get.return_value = response(302, location="http://internal/secret")
            with self.assertRaisesRegex(elsewhere.ElsewhereError, "Not a public"):
                self.web.fetch("https://example.com/a")
        with patch("socket.getaddrinfo", return_value=PUBLIC):
            self.session.get.return_value = response(body=b"x" * (elsewhere.MAX_BYTES + 1))
            with self.assertRaisesRegex(elsewhere.ElsewhereError, "10 MB"):
                self.web.fetch("https://example.com/big")
            self.session.get.return_value = response(body=b"PK zip", kind="application/zip")
            with self.assertRaisesRegex(elsewhere.ElsewhereError, "Neither"):
                self.web.fetch("https://example.com/file.zip")
            self.session.get.return_value = response(body=b"%PDF-1.7 complaint", kind="application/octet-stream")
            self.assertEqual(self.web.fetch("https://example.com/c.pdf")[0], "pdf")
            self.session.get.side_effect = AssertionError("served from the cache")
            self.assertEqual(self.web.fetch("https://example.com/c.pdf")[0], "pdf")

    def test_pick_parsing(self):
        raw = json.dumps({"picks": [{"id": "R1", "same_case": True, "kind": "complaint", "reason": "PDF with both parties."},
                                    {"id": "R2", "same_case": False, "kind": "news", "reason": "Other case."}]})
        self.assertEqual([p["id"] for p in elsewhere.parse_picks(raw)], ["R1"])
        with self.assertRaises(InvalidOutput):
            elsewhere.parse_picks(json.dumps({"picks": [{"id": "R1", "same_case": True, "kind": "blog", "reason": "x"}]}))


class FakeWeb:
    """Results: a news page about another case, the agency's press release linking to the complaint."""

    PAGES = {
        "https://news.example/old": ("html", b"<p>Digital Forensics Corporation settles 2019 suit</p>"),
        "https://www.ftc.gov/news/ftc-sues": ("html", b"<p>Federal Trade Commission sues Digital Forensics Corporation. "
                                                      b"October 6, 2026</p><a href='/system/files/complaint.pdf'>Complaint</a>"),
        "https://www.ftc.gov/system/files/complaint.pdf": ("pdf", b"%PDF-1.7"),
    }

    def __init__(self, pages=None):
        self.pages, self.queries = pages or self.PAGES, []

    def search(self, query, results=5):
        self.queries.append(query)
        return [{"title": t, "url": u, "snippet": ""} for t, u in
                [("Old suit", "https://news.example/old"), ("FTC sues", "https://www.ftc.gov/news/ftc-sues")]], False

    def fetch(self, url):
        if url not in self.pages:
            raise elsewhere.ElsewhereError("HTTP 404")
        kind, body = self.pages[url]
        return kind, body, url


class PickModel:
    stats = {"cached": 0}

    def __init__(self, picks):
        self.picks, self.trace, self.asked = picks, [], None

    def record(self, step, label, **data):
        self.trace.append(step)

    def ask(self, prompt_name, user, parse, max_tokens, label=""):
        self.asked = user
        return parse(json.dumps({"picks": self.picks}))


def pick(n, kind="press release"):
    return {"id": f"R{n}", "same_case": True, "kind": kind, "reason": "Same parties."}


class FinderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.courtlistener = MagicMock(**{"docket.return_value": {"docket_number": "1:26-cv-04321", "court": "DDC"}})
        patcher = patch("src.matching.elsewhere.public")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("src.matching.discover.first_pages", return_value=COMPLAINT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def finder(self, picks, web=None):
        self.web = web or FakeWeb()
        return elsewhere.Finder(PickModel(picks), self.web, self.courtlistener)

    def test_follows_the_press_release_to_the_verified_complaint(self):
        case = dict(CASE)
        found = self.finder([pick(1, "news"), pick(2)]).find(case, self.dir)
        self.assertEqual((found["verified"], found["kind"], found["source"]), (True, "complaint", "ftc.gov"))
        self.assertEqual(found["url"], "https://www.ftc.gov/system/files/complaint.pdf")
        self.assertEqual((found["via"], found["matched_by"]), ("https://www.ftc.gov/news/ftc-sues", "docket number and parties"))
        self.assertTrue(Path(case["web_pdf"]).exists())
        self.assertEqual((case["docket_number"], case["court"]), ("1:26-cv-04321", "DDC"))
        self.assertEqual(self.web.queries, ['"Digital Forensics Corporation LLC" Federal Trade Commission complaint 2026',
                                            '"1:26-cv-04321" complaint'])

    def test_a_verified_page_alone_is_kept_as_a_web_page(self):
        pages = {k: v for k, v in FakeWeb.PAGES.items() if not k.endswith(".pdf")}
        case = dict(CASE)
        found = self.finder([pick(2)], FakeWeb(pages)).find(case, self.dir)
        self.assertEqual((found["kind"], found["matched_by"]), ("web page", "parties and filing year"))
        self.assertIn("Digital Forensics", Path(case["web_text"]).read_text(encoding="utf-8"))
        self.assertNotIn("web_pdf", case)

    def test_the_models_pick_alone_is_not_enough(self):
        case = dict(CASE)
        found = self.finder([pick(1, "complaint")]).find(case, self.dir)  # The 2019 article: wrong case.
        self.assertFalse(found["verified"])
        self.assertEqual(found["tried"], [{"url": "https://news.example/old",
                                           "result": "rejected: does not name federal trade commission"}])
        self.assertNotIn("web_pdf", case)
        self.assertNotIn("web_text", case)

    def test_no_docket_number_still_searches(self):
        self.courtlistener.docket.side_effect = elsewhere.discover.DiscoveryError("budget")
        case = dict(CASE)
        found = self.finder([]).find(case, self.dir)
        self.assertEqual((len(found["queries"]), found["verified"]), (1, False))


class FindCasesTests(unittest.TestCase):
    def test_a_complaint_found_on_the_web_is_read_with_its_source(self):
        search = MagicMock(days=365, **{"since.return_value": date(2025, 11, 4)})
        search.search.return_value = ([{**CASE, "url": "https://cl/docket/7/", "has_pdf": False, "page_count": None,
                                        "document_label": "Complaint", "document_url": None, "pacer_doc_id": None}], False)
        model = MagicMock(stats={"cached": 0})
        model.ask.side_effect = [[{"label": "Forensics", "terms": "forensics"}],
                                 {"C1": {"relevant": True, "unclear": False, "domains": [], "case_type": "FTC Act",
                                         "reason": "Digital forensics."}}]

        def find(case, pdf_dir):
            case["web_pdf"] = "/tmp/web-1.pdf"
            return {"verified": True, "kind": "complaint", "source": "ftc.gov", "url": "https://www.ftc.gov/c.pdf",
                    "matched_by": "docket number and parties", "results": 2}

        steps = []
        cases, record = find_cases(model, search, {"name": "Jane", "headline": "", "domains": [], "items": []}, 6,
                                   Path("/nonexistent"), steps.append, finder=MagicMock(find=find))
        self.assertEqual(len(cases), 1)
        self.assertEqual((cases[0]["pdf"], cases[0]["document_label"], cases[0]["from_web"]),
                         ("/tmp/web-1.pdf", "Complaint (from ftc.gov)", "complaint"))
        self.assertEqual(record["candidates"][0]["elsewhere"]["source"], "ftc.gov")
        self.assertIn("found complaint on ftc.gov", steps[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
