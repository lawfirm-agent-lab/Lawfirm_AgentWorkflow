"""Data passed between pipeline stages. Stages exchange these, never raw table rows."""
import json
from dataclasses import dataclass, field

# Ordered from least to most important; subscriptions compare by position.
IMPORTANCE = ("ignore", "low", "medium", "high")


def importance_rank(level):
    if level not in IMPORTANCE:
        raise ValueError(f"Unknown importance: {level!r}")
    return IMPORTANCE.index(level)


@dataclass(frozen=True)
class SourceResult:
    """Rows modified in [since, cursor). complete=False means more rows may remain."""
    rows: list
    complete: bool
    cursor: str


@dataclass(frozen=True)
class Event:
    """One detected content version of a docket entry. kind: added (first seen) or modified."""
    event_id: str
    source: str
    docket_id: int
    entry_id: int
    kind: str
    payload: dict
    before: dict | None = None
    source_modified_at: str | None = None

    @classmethod
    def from_row(cls, row):
        return cls(row["event_id"], row["source"], row["docket_id"], row["entry_id"], row["kind"],
                   json.loads(row["payload_json"]),
                   json.loads(row["before_json"]) if row["before_json"] else None,
                   row["source_modified_at"])


@dataclass(frozen=True)
class Analysis:
    """Analyzer output. Every analyzer, rule-based or LLM, returns this same shape."""
    importance: str
    category: str | None = None
    summary: str | None = None
    reasons: tuple = ()
    details: dict = field(default_factory=dict)

    def __post_init__(self):
        importance_rank(self.importance)

    def to_json(self):
        return json.dumps({"importance": self.importance, "category": self.category, "summary": self.summary,
                           "reasons": list(self.reasons), "details": self.details},
                          sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        return cls(data["importance"], data.get("category"), data.get("summary"),
                   tuple(data.get("reasons", ())), data.get("details", {}))
