import tempfile
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from src.ingest.pdf import ExtractionError, extract_pdf, _run, main, tool


class PdfTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "input.pdf"
        self.path.write_bytes(b"%PDF-1.4\nmock document")

    @patch("src.ingest.pdf._run")
    def test_mixed_pages_preserve_order_and_source(self, run):
        native = "The Court orders the parties to appear for a status conference."
        run.side_effect = ["Pages: 2\n", native, "", "", "Scanned court order"]
        result = extract_pdf(self.path, source_url="https://example.org/order.pdf")
        self.assertEqual([p['method'] for p in result['pages']], ['text', 'ocr'])
        self.assertEqual([p['page'] for p in result['pages']], [1, 2])
        self.assertEqual(result['pages'][0]['text'], native)
        self.assertEqual(result['pages'][1]['text'], 'Scanned court order')
        self.assertEqual(result['source_url'], 'https://example.org/order.pdf')

    @patch("src.ingest.pdf._run")
    def test_force_ocr_and_blank_warning(self, run):
        run.side_effect = ["Pages: 1\n", "", ""]
        result = extract_pdf(self.path, force_ocr=True)
        self.assertEqual(result['pages'][0]['method'], 'ocr')
        self.assertTrue(result['pages'][0]['warnings'])
        self.assertFalse(any(c.args[0][0] == 'pdftotext' for c in run.call_args_list))

    @patch("src.ingest.pdf._run")
    def test_sparse_text_survives_empty_ocr(self, run):
        run.side_effect = ["Pages: 1\n", "Exhibit A", "", ""]
        page = extract_pdf(self.path)['pages'][0]
        self.assertEqual(page['text'], 'Exhibit A')
        self.assertEqual(page['method'], 'text_fallback')
        self.assertTrue(page['warnings'])

    @patch("src.ingest.pdf._run", return_value="Pages: 201\n")
    def test_page_limit(self, run):
        with self.assertRaises(ExtractionError):
            extract_pdf(self.path)
        self.assertEqual(run.call_count, 1)

    def test_bad_header_and_size(self):
        with self.assertRaises(ExtractionError):
            extract_pdf(self.path, max_bytes=1)
        self.path.write_text('not a PDF')
        with self.assertRaises(ExtractionError):
            extract_pdf(self.path)

    @patch("src.ingest.pdf.subprocess.run")
    def test_tool_errors(self, run):
        for error in [FileNotFoundError(), subprocess.TimeoutExpired('tool', 1),
                      subprocess.CalledProcessError(1, 'tool', stderr='broken PDF')]:
            run.side_effect = error
            with self.assertRaises(ExtractionError):
                _run(['tool'], 1)

    @patch("src.ingest.pdf._run", side_effect=ExtractionError("OCR failed"))
    def test_failure_does_not_publish(self, run):
        output = self.path.parent / 'results'
        self.assertEqual(main([str(self.path), '--output-dir', str(output)]), 1)
        self.assertFalse(output.exists())

    @patch("src.ingest.pdf._run")
    def test_cli_publishes_both_outputs(self, run):
        run.side_effect = ["Pages: 1\n", "A court order containing enough text for direct extraction."]
        output = self.path.parent / 'results'
        self.assertEqual(main([str(self.path), '--output-dir', str(output)]), 0)
        results = list(output.iterdir())
        self.assertEqual(len(results), 1)
        self.assertTrue((results[0] / 'document.json').exists())
        self.assertIn('Page 1', (results[0] / 'document.txt').read_text())

    def test_tools_found_outside_path(self):
        # Some editor terminals start without Homebrew on PATH; the tools must still be found.
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'pdfinfo'
            executable.write_text('#!/bin/sh\n')
            executable.chmod(0o755)
            with patch('src.ingest.pdf.shutil.which', return_value=None), \
                    patch('src.ingest.pdf.TOOL_DIRS', (directory,)):
                self.assertEqual(tool('pdfinfo'), str(executable))
                self.assertEqual(tool('missing-tool'), 'missing-tool')
