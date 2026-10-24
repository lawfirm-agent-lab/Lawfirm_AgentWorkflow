"""Strict parsing and source verification of model output. The model is never trusted.

parse() rejects anything outside the schema. verify() keeps a claim only when its quote
is found in the text the model was given, and a deadline only when its date appears in
that quote. Summary and action cannot be checked mechanically and are labelled as AI text.
"""
import json
import re
from datetime import date

from src.core.models import IMPORTANCE

CATEGORIES = ("order", "judgment", "scheduling", "hearing_notice", "motion", "brief", "letter",
              "discovery", "stipulation", "notice", "administrative", "other")
MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b")
ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
WORDS = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)


class InvalidOutput(ValueError):
    """Model output does not match the schema. Message never includes the output itself."""


def text(value, limit, field, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidOutput(f"{field} must be a non-empty string")
    return " ".join(value.split())[:limit]


def parse(raw):
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise InvalidOutput("Not valid JSON") from None
    if not isinstance(data, dict):
        raise InvalidOutput("Not a JSON object")
    if data.get("importance") not in IMPORTANCE:
        raise InvalidOutput("importance is not an allowed value")
    if data.get("category") not in CATEGORIES:
        raise InvalidOutput("category is not an allowed value")
    deadlines = data.get("deadlines") or []
    if not isinstance(deadlines, list) or len(deadlines) > 10:
        raise InvalidOutput("deadlines must be a short list")
    parsed = []
    for item in deadlines:
        if not isinstance(item, dict):
            raise InvalidOutput("deadline must be an object")
        try:
            day = date.fromisoformat(item.get("date") or "")
        except (TypeError, ValueError):
            raise InvalidOutput("deadline date must be YYYY-MM-DD") from None
        parsed.append({"date": day.isoformat(), "what": text(item.get("what"), 120, "deadline what"),
                       "quote": text(item.get("quote"), 300, "deadline quote")})
    confidence = data.get("confidence")
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        raise InvalidOutput("confidence must be between 0 and 1")
    return {"importance": data["importance"], "category": data["category"],
            "court_issued": data.get("court_issued") is True,
            "summary": text(data.get("summary"), 300, "summary"),
            "action": text(data.get("action"), 200, "action", required=False),
            "deadlines": parsed, "evidence": text(data.get("evidence"), 300, "evidence", required=False),
            "confidence": float(confidence)}


def normalize(value):
    value = value.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    value = value.replace("–", "-").replace("—", "-")
    return " ".join(value.lower().split())


def dates_in(quote):
    found = set()
    for month, day, year in NUMERIC.findall(quote):
        year = int(year) + (2000 if len(year) == 2 else 0)
        found.add((year, int(month), int(day)))
    for year, month, day in ISO.findall(quote):
        found.add((int(year), int(month), int(day)))
    for month, day, year in WORDS.findall(quote):
        found.add((int(year), MONTHS[month[:3].lower()], int(day)))
    result = set()
    for year, month, day in found:
        try:
            result.add(date(year, month, day).isoformat())
        except ValueError:
            pass
    return result


def verify(parsed, source):
    """Return (verified output, number of dropped claims)."""
    haystack, dropped = normalize(source), 0
    evidence = parsed["evidence"]
    if evidence and normalize(evidence) not in haystack:
        evidence, dropped = None, dropped + 1
    deadlines = []
    for item in parsed["deadlines"]:
        if normalize(item["quote"]) in haystack and item["date"] in dates_in(item["quote"]):
            deadlines.append(item)
        else:
            dropped += 1
    return {**parsed, "evidence": evidence, "deadlines": deadlines}, dropped
