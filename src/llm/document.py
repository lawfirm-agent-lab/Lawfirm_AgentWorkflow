"""Full-document briefing: page-marked input, strict output schema and page-level verification.

Pages come from CourtListener plain_text (one form feed per page) or from src.ingest.pdf.
Every claim must quote the document. verify_document() keeps a claim only when its quote is
on the page the model cited, or occurs exactly once elsewhere (the page is then corrected), so
every page shown is one where the quote really is. A date is kept only when the quote
contains it. What the claim says about the quote (which event, which party) is AI reading.
"""
import json
import re
from datetime import date

from src.llm.output import InvalidOutput, dates_in, normalize, text

MARKER = re.compile(r"\[Page \d+\]")
MAX_ITEMS = {"key_points": 5, "disputes": 6, "deadlines": 15, "proposed_dates": 40}


def pages_from_plain_text(value):
    """CourtListener plain_text ends each page with a form feed."""
    pages = [page.strip() for page in (value or "").split("\x0c")]
    while pages and not pages[-1]:
        pages.pop()
    return pages


def pages_from_extraction(result):
    return [page["text"] for page in result["pages"]]


def document_text(payload, pages, limit):
    """The exact input the model sees. Returns (text, pages included, truncated).

    Whole pages are included until the limit; a note tells the model where the text stops."""
    header = [f"Entry number: {payload.get('entry_number')}", f"Date filed: {payload.get('date_filed')}",
              "Description: " + " ".join(str(payload.get("description") or "").split()), f"Pages: {len(pages)}", ""]
    result, used = "\n".join(header), 0
    for number, page in enumerate(pages, 1):
        # A marker-like string inside the page would make page citations ambiguous.
        block = f"[Page {number}]\n{MARKER.sub('[page]', page)}\n"
        if len(result) + len(block) > limit and used:
            break
        result, used = result + block[:max(0, limit - len(result))], number
    truncated = used < len(pages)
    if truncated:
        result += f"[Text truncated after page {used} of {len(pages)}]\n"
    return result, used, truncated


def page_number(value):
    if type(value) is not int or value < 1:
        raise InvalidOutput("page must be a positive integer")
    return value


def calendar_date(value):
    try:
        return date.fromisoformat(value or "").isoformat()
    except (TypeError, ValueError):
        raise InvalidOutput("date must be YYYY-MM-DD") from None


def items(data, field):
    """A list field. Items over the limit are cut (the prompt asks for the most important first);
    far longer lists mean the model ignored the instructions."""
    value = data.get(field) or []
    if not isinstance(value, list) or len(value) > 3 * MAX_ITEMS[field]:
        raise InvalidOutput(f"{field} must be a short list")
    value = value[:MAX_ITEMS[field]]
    if not all(isinstance(item, dict) for item in value):
        raise InvalidOutput(f"{field} entries must be objects")
    return value


def cited(item, **fields):
    """Parse the fields of one claim plus its quote and page."""
    result = {name: parse_field(item.get(name)) for name, parse_field in fields.items()}
    return {**result, "quote": text(item.get("quote"), 300, "quote"), "page": page_number(item.get("page"))}


def parse_document(raw):
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise InvalidOutput("Not valid JSON") from None
    if not isinstance(data, dict):
        raise InvalidOutput("Not a JSON object")
    short = lambda field, limit: lambda value: text(value, limit, field)
    disputes = []
    for dispute in items(data, "disputes"):
        positions = dispute.get("positions")
        if not isinstance(positions, list) or not 1 <= len(positions) <= 4 or not all(isinstance(p, dict) for p in positions):
            raise InvalidOutput("dispute positions must be a short list of objects")
        disputes.append({"issue": text(dispute.get("issue"), 80, "issue"),
                         "positions": [cited(p, party=short("party", 60), position=short("position", 200)) for p in positions]})
    confidence = data.get("confidence")
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        raise InvalidOutput("confidence must be between 0 and 1")
    return {"document_type": text(data.get("document_type"), 80, "document_type"),
            "overview": text(data.get("overview"), 400, "overview"),
            "key_points": [cited(i, point=short("point", 200)) for i in items(data, "key_points")],
            "disputes": disputes,
            "deadlines": [cited(i, date=calendar_date, what=short("what", 200)) for i in items(data, "deadlines")],
            "proposed_dates": [cited(i, event=short("event", 120), party=short("party", 60), date=calendar_date)
                               for i in items(data, "proposed_dates")],
            "action": text(data.get("action"), 200, "action", required=False),
            "confidence": float(confidence)}


def searchable(value):
    """normalize(), and ignore hyphens so a word broken across lines ("patents-in-\nsuit") still matches."""
    return re.sub(r"-\s", "-", normalize(value)).replace("-", "")


class Pages:
    """Searchable page texts joined into one string, with page offsets."""

    def __init__(self, pages):
        self.starts, parts, offset = [], [], 0
        for page in pages:
            value = searchable(page)
            self.starts.append(offset)
            parts.append(value)
            offset += len(value) + 1
        self.text = " ".join(parts)

    def page_at(self, position):
        return max(i for i, start in enumerate(self.starts) if start <= position) + 1

    def locate(self, quote, hint=None):
        """(page, None) for a quote on the cited page, or found once elsewhere; else (None, reason)."""
        needle, found = searchable(quote), []
        start = self.text.find(needle) if needle else -1
        while start >= 0:
            found.append(start)
            start = self.text.find(needle, start + 1)
        pages = {self.page_at(f) for f in found}
        if hint in pages:
            return hint, None
        if not found:
            return None, "quote not found"
        return (pages.pop(), None) if len(found) == 1 else (None, "quote on other pages, more than once")


def verify_document(parsed, pages):
    """Return (verified output, dropped claims with the reason, number of corrected pages)."""
    index, dropped, corrected = Pages(pages), [], 0

    def keep(field, item, dated=False):
        nonlocal corrected
        page, reason = index.locate(item["quote"], item["page"])
        if page and dated and item["date"] not in dates_in(item["quote"]):
            reason = "date not in quote"
        if reason:
            dropped.append({"field": field, "quote": item["quote"], "page": item["page"], "reason": reason})
            return None
        corrected += page != item["page"]
        return {**item, "page": page}

    def kept(field, values, dated=False):
        return [item for item in (keep(field, v, dated) for v in values) if item]

    disputes = []
    for dispute in parsed["disputes"]:
        positions = kept("disputes", dispute["positions"])
        if positions:
            disputes.append({**dispute, "positions": positions})
    result = {**parsed, "key_points": kept("key_points", parsed["key_points"]), "disputes": disputes,
              "deadlines": kept("deadlines", parsed["deadlines"], dated=True),
              "proposed_dates": kept("proposed_dates", parsed["proposed_dates"], dated=True)}
    return result, dropped, corrected
