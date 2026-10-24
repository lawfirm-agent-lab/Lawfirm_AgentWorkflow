"""Extension points. Add a new data source, analyzer, channel or model by implementing one.

Each pipeline stage depends only on these protocols, so an implementation can later be
replaced by a more autonomous agent without touching the other stages.
"""
from typing import Protocol

from src.core.models import Analysis, Event, SourceResult


class Source(Protocol):
    name: str

    def fetch_docket_changes(self, docket_id: int, since: str, until: str) -> SourceResult:
        """Return entries modified in [since, until), oldest first. Raise on failure; never guess."""

    def fetch_docket_info(self, docket_id: int) -> dict:
        """Return display metadata: case_name, docket_number, court, court_id, absolute_url (any may be None)."""

    def fetch_court_info(self, court_id: str) -> dict:
        """Return court display names: citation (e.g. 'S.D.N.Y.') and full_name (either may be None)."""


class Analyzer(Protocol):
    name: str
    version: str

    def analyze(self, event: Event) -> Analysis:
        """Score one event. Must be deterministic enough to audit; raise on failure."""


class Notifier(Protocol):
    name: str

    def send(self, recipient: str, subject: str, body: str, key: str, html: str | None = None) -> str:
        """Deliver once per key. body is plain text; html is an optional rich alternative.
        Return 'sent', or 'needs_review' when delivery is uncertain."""


class LLMClient(Protocol):
    def complete(self, messages: list[dict], *, json_mode: bool = False, max_tokens: int | None = None):
        """Return an LLMReply (text plus token usage) for chat-style messages."""
