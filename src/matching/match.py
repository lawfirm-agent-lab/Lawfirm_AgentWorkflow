"""Match one expert to one case, then rank the cases for that expert.

The model proposes a tier, reasons and an email. Code then enforces:
- a reason must reference a verified expert item (E id) and a verified case point (I or X id);
- "high" needs at least one direct match left after that check, "medium" at least one match,
  otherwise the tier drops a level at a time;
- no email below "medium";
- a case without readable document text is "insufficient" and is never sent to the model.
"""
import re
from datetime import date

from src.llm.output import InvalidOutput, text
from src.matching.profiles import choice, load, objects, strings

TIERS = ("high", "medium", "low", "none")
ORDER = {"high": 0, "medium": 1, "low": 2, "insufficient": 3, "none": 4}
LABELS = {"high": "High", "medium": "Medium", "low": "Low", "none": "Not relevant", "insufficient": "Insufficient information"}
STRENGTHS = ("direct", "related")
# Wording the email must not use unless the document itself says expertise is needed.
NEED_CLAIMS = re.compile(r"\b(you|your (team|client|case)|the case|this case) (will )?(need|needs|require|requires)\b"
                         r"|\bneeds? an expert\b|\brequires? (an )?expert\b", re.I)


def parse_match(raw):
    data = load(raw)
    email = data.get("email")
    if email is not None and not isinstance(email, dict):
        raise InvalidOutput("email must be an object or null")
    # The prompt asks for 300 characters; models run over, and a cut-off sentence reads badly.
    return {"tier": choice(data.get("tier"), TIERS, "tier"), "summary": text(data.get("summary"), 600, "summary"),
            "matches": [{"expert_item": text(m.get("expert_item"), 8, "expert_item"),
                         "case_point": text(m.get("case_point"), 8, "case_point"),
                         "strength": choice(m.get("strength"), STRENGTHS, "strength"),
                         "reason": text(m.get("reason"), 300, "reason")} for m in objects(data, "matches", 6)],
            "gaps": strings(data, "gaps", 3, 200),
            "email": email and {"to_party": text(email.get("to_party"), 120, "to_party", required=False),
                                "subject": text(email.get("subject"), 120, "subject"),
                                "body": (email.get("body") or "").strip()[:1800] or None}}


def enforce(parsed, expert, case):
    """Drop reasons that cite unknown ids, then make the tier consistent with what is left."""
    items = {i["id"]: i for i in expert["items"]}
    # Non-technical issues (willfulness, damages law) are for lawyers, not a technical expert.
    points = {p["id"]: p for p in [i for i in case["issues"] if i["technical"]] + case["expertise"]}
    matches, notes = [], []
    for m in parsed["matches"]:
        if m["expert_item"] in items and m["case_point"] in points:
            matches.append({**m, "expert": items[m["expert_item"]], "point": points[m["case_point"]]})
        else:
            notes.append(f"Dropped a reason citing {m['expert_item']}/{m['case_point']} (unknown or non-technical)")
    tier = parsed["tier"]
    if tier == "high" and not any(m["strength"] == "direct" for m in matches):
        tier = "medium"
        notes.append("Lowered from high: no verified direct match")
    if tier == "medium" and not matches:
        tier = "low"
        notes.append("Lowered from medium: no verified match")
    email = parsed["email"] if tier in ("high", "medium") and parsed["email"] and parsed["email"]["body"] else None
    stated = any(p["basis"] == "stated" for p in case["expertise"])
    if email and not stated and NEED_CLAIMS.search(email["body"]):
        notes.append("Email says the case needs an expert, but the document does not: edit before sending")
    return {**parsed, "tier": tier, "model_tier": parsed["tier"], "matches": matches, "email": email, "notes": notes}


def insufficient(reason):
    return {"tier": "insufficient", "summary": reason, "matches": [], "gaps": [], "email": None, "notes": []}


def score(match, profile, case, today=None):
    """Order within a tier: relevance (verified evidence, expertise the document itself asks for)
    plus importance (how technical the case is, how recently it was filed). Every part is shown."""
    today = today or date.today()
    direct = sum(m["strength"] == "direct" for m in match["matches"])
    related = len(match["matches"]) - direct
    stated = any(m["point"].get("basis") == "stated" for m in match["matches"])
    issues = (profile or {}).get("issues") or []
    technical = round(sum(i["technical"] for i in issues) / len(issues), 1) if issues else 0
    try:
        age = (today - date.fromisoformat(case.get("filed") or "")).days
    except ValueError:
        age = None
    recent = 1 if age is not None and age <= 90 else 0.5 if age is not None and age <= 365 else 0
    parts = {"evidence": 2 * direct + related, "stated": 1 if stated else 0, "technical": technical, "recent": recent}
    return {"total": round(sum(parts.values()), 1), "parts": parts, "direct": direct, "related": related, "age_days": age}


def rank_key(result):
    """Tier first, then the score; ties by name so the order is stable."""
    return ORDER[result["match"]["tier"]], -result["score"]["total"], result["case"]["name"]


def rank(results):
    return sorted(results, key=rank_key)
