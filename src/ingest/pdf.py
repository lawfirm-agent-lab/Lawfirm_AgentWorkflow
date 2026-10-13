"""Local PDF text extraction with per-page Tesseract fallback.

Run: python -m src.ingest.pdf document.pdf --output-dir data/ocr
External dependencies: Poppler and Tesseract (English language data by default).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


class ExtractionError(RuntimeError):
    """The document could not be completely processed."""


def _run(args, timeout):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env={**os.environ, "LC_ALL": "C"}).stdout
    except FileNotFoundError as exc:
        raise ExtractionError(f"Missing executable: {args[0]}. Install Poppler and Tesseract.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"{args[0]} exceeded the {timeout}s timeout") from exc
    except subprocess.CalledProcessError as exc:
        raise ExtractionError(f"{args[0]} failed: {exc.stderr.strip()[:500]}") from exc


def extract_pdf(path, *, language="eng", force_ocr=False, min_text_chars=40,
                max_pages=200, max_bytes=50 * 1024 * 1024, timeout=120,
                source_url=None):
    """Return page-numbered text; never modify the input or contact a service.

    Sparse text pages use OCR. Force OCR for mixed image/text pages or a bad
    existing text layer. Blank OCR results are explicitly flagged for review.
    Any tool failure aborts the result instead of silently omitting a page.
    """
    if min_text_chars < 0 or max_pages < 1 or max_bytes < 1 or timeout <= 0:
        raise ValueError("Invalid extraction limits")
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", language):
        raise ValueError("Use language codes such as eng or eng+chi_sim")
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size > max_bytes:
        raise ExtractionError("Input is not a regular file or exceeds the size limit")
    with path.open("rb") as stream:
        if b"%PDF-" not in stream.read(1024):
            raise ExtractionError("Input does not have a PDF header")
        stream.seek(0)
        hasher = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
        digest = hasher.hexdigest()
    info = _run(["pdfinfo", str(path)], timeout)
    match = re.search(r"^Pages:\s+(\d+)\s*$", info, re.M)
    if not match or not 1 <= int(match[1]) <= max_pages:
        raise ExtractionError("Invalid page count or document exceeds the page limit")
    pages = []
    with tempfile.TemporaryDirectory(prefix="docket-ocr-") as directory:
        for number in range(1, int(match[1]) + 1):
            text = "" if force_ocr else _run(
                ["pdftotext", "-f", str(number), "-l", str(number), "-layout",
                 "-enc", "UTF-8", str(path), "-"], timeout).strip("\x0c\n\r ")
            method = "text"
            if force_ocr or sum(c.isalnum() for c in text) < min_text_chars:
                prefix = str(Path(directory) / "page")
                _run(["pdftoppm", "-f", str(number), "-l", str(number),
                      "-singlefile", "-r", "300", "-scale-to", "5000", "-png",
                      str(path), prefix], timeout)
                ocr = _run(["tesseract", prefix + ".png", "stdout", "-l", language], timeout).strip()
                # Preserve a sparse but useful text layer if OCR returns nothing.
                if ocr or not text:
                    text, method = ocr, "ocr"
                else:
                    method = "text_fallback"
            warnings = []
            if method == "text_fallback":
                warnings.append("OCR returned no text; retained the existing text layer.")
            if not text:
                warnings.append("No text detected; page may be blank or require manual review.")
            pages.append({"page": number, "method": method, "text": text, "warnings": warnings})
    return {"schema_version": 1, "source_file": str(path), "source_url": source_url,
            "sha256": digest, "page_count": len(pages), "language": language,
            "pages": pages}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data/ocr"))
    parser.add_argument("--language", default="eng")
    parser.add_argument("--force-ocr", action="store_true")
    parser.add_argument("--source-url", help="Reference metadata only; never fetched")
    parser.add_argument("--max-pages", type=int, default=200)
    args = parser.parse_args(argv)
    try:
        result = extract_pdf(args.pdf, language=args.language, force_ocr=args.force_ocr,
                             source_url=args.source_url, max_pages=args.max_pages)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        # A unique result directory keeps previous outputs intact on failure/re-run.
        with tempfile.TemporaryDirectory(dir=args.output_dir, prefix=".extract-") as staging:
            root = Path(staging)
            (root / "document.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            (root / "document.txt").write_text("\n\n".join(
                f"--- Page {p['page']} ({p['method']}) ---\n{p['text']}" for p in result["pages"]), encoding="utf-8")
            destination = args.output_dir / ("document-" + result["sha256"][:12] + "-" + root.name.removeprefix(".extract-"))
            root.rename(destination)
        print(f"Extracted {result['page_count']} pages: {destination.resolve()}")
        return 0
    except (ExtractionError, OSError, ValueError) as exc:
        print(f"PDF extraction failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
