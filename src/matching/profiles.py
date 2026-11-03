"""Expert and case profiles: strict parsing of model output, then quote verification.

Every experience item, issue, expertise area and counsel entry carries a quote and a page.
A claim is kept only when its quote is found in the source (on the cited page, or once
elsewhere, as in src.llm.document). Kept claims get ids (E1, I1, X1) that the match step
must reference, so every reason shown traces back to verified source text.

Lawyers' email addresses are found by code, not by the model: every address in the document is
listed, and a lawyer gets the one whose user name contains their surname (preferring one that
starts with their first initial), since signature blocks often list the addresses apart from the
names. Addresses no lawyer claims are kept as "emails", for a person to choose from.
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
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "esq"}


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
    """The first `limit` entries. A longer list is cut, not rejected: a complaint can really name
    dozens of defendants."""
    value = data.get(field) or []
    if not isinstance(value, list):
        raise InvalidOutput(f"{field} must be a list")
    return value[:limit]


def parties(data, field, limit=6):
    """Party names, the first `limit` of them, then "and N others" when the list is longer."""
    names = strings(data, field, 500, 120)
    return names[:limit] + ([f"and {len(names) - limit} others"] if len(names) > limit else [])


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
    return {"plaintiffs": parties(data, "plaintiffs"), "defendants": parties(data, "defendants"),
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


def letters(value):
    return re.sub(r"[^a-z]", "", value.lower())


def emails_in(pages):
    """Distinct addresses in document order, each with the first page it appears on."""
    seen = {}
    for number, page in enumerate(pages, 1):
        for match in EMAIL.finditer(page):
            seen.setdefault(match.group().lower(), number)
    return [{"email": address, "page": page} for address, page in seen.items()]


def counsel_email(name, emails):
    words = [w for w in (letters(part) for part in name.split()) if w and w not in NAME_SUFFIXES]
    if not words or len(words[-1]) < 3:
        return None
    first, last = words[0], words[-1]
    hits = [e for e in emails if last in letters(e["email"].split("@")[0])]
    hits.sort(key=lambda e: not letters(e["email"].split("@")[0]).startswith(first[0]))
    return hits[0] if hits else None


def add_emails(profile, pages):
    """Each counsel entry gets "email" and "email_page" (None when not found); the profile gets
    "emails": the other addresses in the document (at most 20)."""
    emails, used, counsel = emails_in(pages), set(), []
    for c in profile["counsel"]:
        hit = counsel_email(c["name"], emails)
        if hit:
            used.add(hit["email"])
        counsel.append({**c, "email": hit and hit["email"], "email_page": hit and hit["page"]})
    return {**profile, "counsel": counsel, "emails": [e for e in emails if e["email"] not in used][:20]}
