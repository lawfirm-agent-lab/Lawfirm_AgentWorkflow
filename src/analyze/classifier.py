"""Conservative court-origin candidates from descriptions, not verified authorship."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

TIMEZONE = "America/Los_Angeles"
COURT_PATTERNS = [
    r"^(?:(?:minute|text|electronic|amended|scheduling|standing|final)\s+)*(?:order|judgment)\b",
    r"^memorandum(?:\s+opinion)?\s+and\s+order\b",
    r"^(?:notice|notification)\s+(?:by|from)\s+(?:the\s+)?court\b",
    r"^notice\s+of\s+(?:hearing|conference|trial)\b.*\b(?:issued|entered)\s+by\s+(?:the\s+)?court\b",
]


def classify(entry, today=None, historical=False):
    target = today or datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    if not historical and entry.get("date_filed") != target:
        return {"important": False, "reasons": []}
    text = entry.get("description", "").strip()
    matches = [m.group(0) for pattern in COURT_PATTERNS
               if (m := re.search(pattern, text, re.I))]
    reasons = [f"Court-origin candidate: description starts with '{match}'" for match in matches]
    if matches and not historical:
        reasons.insert(0, f"Filed today: {target} (reference timezone: {TIMEZONE})")
    return {"important": bool(matches), "reasons": reasons}
