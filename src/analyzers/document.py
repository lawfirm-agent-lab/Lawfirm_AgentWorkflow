"""Document analyzer: a verified, page-cited briefing on the full text of one filed document.

It complements the entry analyzer, which decides importance from the docket entry. Its result
is a dict stored under Analysis.details["document"] and shown in the digest; any failure
returns a status and no claims, so delivery never depends on it. Calls share the llm_calls
audit trail, cache and daily token budget with the entry analyzer.
"""
from src.analyzers.llm import LLMAnalyzer, Unavailable
from src.llm.document import document_text, parse_document, verify_document

PROMPT_VERSION = "document_v1"


class DocumentAnalyzer(LLMAnalyzer):
    def __init__(self, store, client, llm_settings, limits, **options):
        super().__init__(store, client, llm_settings, limits, name="document", prompt_version=PROMPT_VERSION, **options)
        self.tag, self.parse_output, self.output_limit = "document", parse_document, limits.max_document_output_tokens
        self.stats["corrected_pages"] = 0

    def analyze(self, event, pages):
        """pages: page texts in order (see src.llm.document.pages_from_*)."""
        source, used, truncated = document_text(event.payload, pages, self.limits.max_document_chars)
        meta = {"model": self.llm.model, "prompt_version": self.prompt_version, "pages": len(pages),
                "pages_read": used, "truncated": truncated}
        try:
            parsed = self.model_output(event.event_id, source)
        except Unavailable as exc:
            self.stats["fallbacks"] += 1
            return {**meta, "status": exc.status}
        verified, dropped, corrected = verify_document(parsed, pages[:used])
        self.stats["dropped_claims"] += len(dropped)
        self.stats["corrected_pages"] += corrected
        # Dropped claims are kept for audits and prompt tuning; they are never shown to recipients.
        return {**meta, "status": "ok", "dropped_claims": len(dropped), "dropped": dropped,
                "corrected_pages": corrected, **verified}
