"""Compact "Other activity" by filing type, so a heavy filing day reads as a few lines.

Deterministic and rule-based: no model calls. A group of one keeps its own line; larger
groups become a count, a sealed count, and for declarations the declarants and volumes.
"""
import re

SEALED = re.compile(r"\*{2,}\s*sealed\s*\*{2,}", re.I)
# (pattern at the start of the description, plural label). First match wins.
KINDS = (
    (r"(declaration|affidavit)s?\b", "declarations and affidavits"),
    (r"memorand(um|a)\b", "memoranda of law"),
    (r"certificate of service\b", "certificates of service"),
    (r"(response|reply|sur-?reply|opposition)\b", "responses and replies"),
    (r"letter motion\b", "letter motions"),
    (r"letter\b", "letters"),
    (r"notice of appearance\b", "notices of appearance"),
    (r"(motion|application)\b", "motions"),
    (r"redaction\b", "redactions"),
    (r"(transcript|notice of filing of official transcript)\b", "transcripts"),
    (r"stipulation\b", "stipulations"),
    (r"exhibits?\b", "exhibits"),
    (r"notice\b", "notices"),
)
# A name ends before "(", " in ", " re:", ", ", " filed", ". Document" or a final period; a
# middle initial such as "S." is part of the name.
DECLARANT = re.compile(r"^(?:declaration|affidavit)s?\s+of\s+(.+?)"
                       r"(?=\s*\(|\s+in\s+|\s+re:|\s+filed\b|\.\s+document\b|,|\.?\s*$)", re.I)
VOLUME = re.compile(r"\((sealed\s+)?vol(?:ume)?\.?\s*(\d+)\)", re.I)
MAX_NAMES = 5


def classify(description):
    """Return (kind, sealed, cleaned description). kind is None for unrecognised filings."""
    sealed = bool(SEALED.search(description))
    text = " ".join(SEALED.sub(" ", description).split())
    for pattern, label in KINDS:
        if re.match(pattern, text, re.I):
            return label, sealed, text
    return None, sealed, text


def title(name):
    return name.title() if name.isupper() else name


def ranges(numbers):
    numbers, parts, start = sorted(set(numbers)), [], None
    for i, n in enumerate(numbers):
        start = n if start is None else start
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            parts.append(f"{start}" if start == n else f"{start}–{n}")
            start = None
    return ", ".join(parts)


def declarants(texts):
    people = {}
    for text in texts:
        match = DECLARANT.match(text)
        if not match:
            continue
        name = title(" ".join(match.group(1).split()))
        people.setdefault(name, []).extend(int(v) for _, v in VOLUME.findall(text))
    parts = []
    for name, volumes in list(people.items())[:MAX_NAMES]:
        label = f"Vol{'s' * (len(set(volumes)) > 1)}. {ranges(volumes)}" if volumes else ""
        parts.append(f"{name} ({label})" if label else name)
    if len(people) > MAX_NAMES:
        parts.append(f"and {len(people) - MAX_NAMES} more")
    return ", ".join(parts)


def group(entries):
    """entries: rendered entry dicts. Returns (groups, singles, undescribed count).

    groups: [{"label", "count", "sealed", "detail"}] for kinds with two or more entries.
    singles: entries shown on their own line (unrecognised kinds and groups of one).
    """
    buckets, singles, undescribed = {}, [], 0
    for entry in entries:
        if not entry["description"]:
            undescribed += 1
            continue
        kind, sealed, text = classify(entry["description"])
        if kind is None:
            singles.append(entry)
        else:
            buckets.setdefault(kind, []).append((entry, sealed, text))
    groups = []
    for kind, members in buckets.items():
        if len(members) == 1:
            singles.append(members[0][0])
            continue
        detail = declarants([text for _, _, text in members]) if kind == "declarations and affidavits" else ""
        groups.append({"label": kind, "count": len(members), "sealed": sum(sealed for _, sealed, _ in members),
                       "detail": detail})
    groups.sort(key=lambda g: (-g["count"], g["label"]))
    singles.sort(key=lambda e: (e["number"] is None, e["number"] or 0))
    return groups, singles, undescribed


def summary_line(group_):
    sealed = f" ({group_['sealed']} sealed)" if group_["sealed"] else ""
    detail = f": {group_['detail']}" if group_["detail"] else ""
    return f"{group_['count']} {group_['label']}{sealed}{detail}"
