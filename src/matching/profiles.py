"""Expert and case profiles: strict parsing of model output, then quote verification.

Every experience item, issue, expertise area and counsel entry carries a quote and a page.
A claim is kept only when its quote is found in the source (on the cited page, or once
elsewhere, as in src.llm.document). Kept claims get ids (E1, I1, X1) that the match step
must reference, so every reason shown traces back to verified source text.
"""
import json
import re

from src.llm.document import Pages, page_number
from src.llm.output import InvalidOutput, found, normalize, text

DOMAINS = ("Gaming & Gambling", "Cybersecurity & Forensics", "Software & Internet", "AI & Machine Learning",
           "Hardware & Electronics", "Telecom & Networking", "Fintech & Payments", "Life Sciences & Medical", "Other")
KINDS = ("role", "project", "patent", "publication", "testimony", "certification", "education", "skill", "award")
BASES = ("stated", "inferred")
UNNAMED = "Unnamed expert"


# Bullet glyphs (often private-use characters from symbol fonts) sit between list items in
# extracted CV text and break exact quote matching.
BULLETS = re.compile(r"[-•◦▪▫♦♠♣♥●○■□➢➤✓✔]")


def clean(pages):
    """Page texts as the model sees them and as quotes are verified against."""
    return [BULLETS.sub(" ", page) for page in pages]


def load(raw):
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise InvalidOutput("Not valid JSON") from None
    if not isinstance(data, dict):
        raise InvalidOutput("Not a JSON object")
    return data


def listed(data, field, limit):
    value = data.get(field) or []
    if not isinstance(value, list) or len(value) > 3 * limit:
        raise InvalidOutput(f"{field} must be a short list")
    return value[:limit]


def objects(data, field, limit):
    value = listed(data, field, limit)
    if not all(isinstance(item, dict) for item in value):
        raise InvalidOutput(f"{field} entries must be objects")
    return value


def strings(data, field, limit, length):
    return [text(v, length, field) for v in listed(data, field, limit) if isinstance(v, str) and v.strip()]


def domains(data):
    # Unknown labels are dropped rather than rejected: the list is a filter, not a claim.
    return [d for d in listed(data, "domains", 4) if d in DOMAINS]


def choice(value, allowed, field):
    if value not in allowed:
        raise InvalidOutput(f"{field} is not an allowed value")
    return value


def quoted(item):
    return {"quote": text(item.get("quote"), 300, "quote"), "page": page_number(item.get("page"))}


def parse_expert(raw):
    data = load(raw)
    name = data.get("name") if isinstance(data.get("name"), str) and data["name"].strip() else None
    return {"name": text(name, 80, "name", required=False) or UNNAMED, "headline": text(data.get("headline"), 200, "headline"),
            "domains": domains(data),
            "items": [{"kind": choice(i.get("kind"), KINDS, "kind"), "summary": text(i.get("summary"), 200, "summary"),
                       **quoted(i)} for i in objects(data, "items", 40)]}


def parse_case(raw):
    data = load(raw)
    return {"plaintiffs": strings(data, "plaintiffs", 6, 120), "defendants": strings(data, "defendants", 6, 120),
            "case_type": text(data.get("case_type"), 80, "case_type"), "domains": domains(data),
            "summary": text(data.get("summary"), 600, "summary"),
            "technology": strings(data, "technology", 8, 60),
            "issues": [{"issue": text(i.get("issue"), 200, "issue"), "technical": i.get("technical") is True, **quoted(i)}
                       for i in objects(data, "issues", 8)],
            "expertise": [{"area": text(i.get("area"), 80, "area"), "basis": choice(i.get("basis"), BASES, "basis"),
                           "why": text(i.get("why"), 200, "why"), **quoted(i)} for i in objects(data, "expertise", 6)],
            "counsel": [{"name": text(i.get("name"), 80, "name"), "firm": text(i.get("firm"), 120, "firm", required=False),
                         "party": text(i.get("party"), 120, "party", required=False), **quoted(i)}
                        for i in objects(data, "counsel", 12)]}


class Source:
    """Quote lookup over page texts: exact match via Pages, then the tolerant found() on the
    cited page (ellipsis between exact parts, pleading line numbers)."""

    def __init__(self, pages):
        self.pages, self.index = pages, Pages(pages)

    def check(self, item):
        page, reason = self.index.locate(item["quote"], item["page"])
        if page:
            return {**item, "page": page}, None
        cited = item["page"]
        if cited <= len(self.pages) and found(item["quote"], normalize(self.pages[cited - 1])):
            return item, None
        return None, reason


def verify(values, source, field, prefix, dropped):
    """Keep claims whose quotes are in the source; number them prefix1, prefix2, ..."""
    kept = []
    for item in values:
        checked, reason = source.check(item)
        if checked:
            kept.append({"id": f"{prefix}{len(kept) + 1}", **checked})
        else:
            dropped.append({"field": field, "quote": item["quote"], "page": item["page"], "reason": reason})
    return kept


def verify_expert(parsed, pages):
    dropped, source = [], Source(pages)
    return {**parsed, "items": verify(parsed["items"], source, "items", "E", dropped)}, dropped


def verify_case(parsed, pages):
    dropped, source = [], Source(pages)
    counsel = [{k: v for k, v in c.items() if k != "id"} for c in verify(parsed["counsel"], source, "counsel", "C", dropped)]
    return {**parsed, "issues": verify(parsed["issues"], source, "issues", "I", dropped),
            "expertise": verify(parsed["expertise"], source, "expertise", "X", dropped), "counsel": counsel}, dropped
