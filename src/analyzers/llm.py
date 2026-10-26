"""LLM analyzer: rules first, then the model, then verification and a conservative merge.

The model may raise importance only with a verified evidence quote and never lowers what
the rules flagged. Any failure (budget, provider, empty or invalid output) returns the
rules result, so delivery never waits on the model. Every attempt is recorded in llm_calls.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.analyzers.rules import RulesAnalyzer
from src.core.models import Analysis, importance_rank
from src.llm.client import LLMError
from src.llm.output import InvalidOutput, parse, verify

PROMPTS = Path(__file__).resolve().parents[1] / "llm" / "prompts"
PROMPT_VERSION = "docket_entry_v4"


class Unavailable(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


def entry_text(payload, limit):
    """The exact text the model sees; verification checks quotes against this same text."""
    lines = [f"Entry number: {payload.get('entry_number')}", f"Date filed: {payload.get('date_filed')}",
             "Description: " + " ".join(str(payload.get("description") or "").split())]
    docs = sorted((d for d in payload.get("recap_documents") or [] if isinstance(d, dict)),
                  key=lambda d: d.get("attachment_number") or 0)
    for doc in docs:
        if isinstance(doc.get("description"), str) and doc["description"].strip():
            suffix = f"-{doc['attachment_number']}" if doc.get("attachment_number") else ""
            lines.append(f"Document {doc.get('document_number')}{suffix}: {' '.join(doc['description'].split())}")
    result = "\n".join(lines)
    body = next((d["plain_text"] for d in docs if isinstance(d.get("plain_text"), str) and d["plain_text"].strip()), "")
    room = limit - len(result) - len("\nDocument text (excerpt):\n")
    if body and room > 200:
        result += "\nDocument text (excerpt):\n" + " ".join(body.split())[:room]
    return result[:limit]


def merge(base, verified, llm):
    """Combine rules and verified model output. llm: status/model/prompt metadata."""
    details = {"rules": {"importance": base.importance, "category": base.category, "reasons": list(base.reasons)},
               "llm": dict(llm)}
    if verified is None:
        return Analysis(base.importance, base.category, None, base.reasons, details)
    proposed = verified["importance"]
    effective = proposed if verified["evidence"] or importance_rank(proposed) <= importance_rank(base.importance) else base.importance
    final = max(base.importance, effective, key=importance_rank)
    reasons = tuple(base.reasons)
    if importance_rank(final) > importance_rank(base.importance):
        reasons += (f'AI rated {final}: "{verified["evidence"]}"',)
    details["llm"].update({k: verified[k] for k in ("importance", "category", "court_issued", "action",
                                                    "deadlines", "evidence", "confidence")})
    details["llm"]["raised"] = final != base.importance
    return Analysis(final, verified["category"], verified["summary"], reasons, details)


class LLMAnalyzer:
    def __init__(self, store, client, llm_settings, limits, *, name="llm", fallback=None,
                 prompt_version=PROMPT_VERSION, now=lambda: datetime.now(timezone.utc)):
        self.store, self.client, self.llm, self.limits, self.now = store, client, llm_settings, limits, now
        self.name, self.prompt_version = name, prompt_version
        self.version = f"{prompt_version}:{llm_settings.model}"
        self.fallback = fallback or RulesAnalyzer()
        self.prompt = (PROMPTS / f"{prompt_version}.md").read_text(encoding="utf-8")
        self.tag, self.parse_output, self.output_limit = "entry", parse, limits.max_output_tokens
        self.stats = {"model": llm_settings.model, "calls": 0, "cached": 0, "fallbacks": 0, "invalid": 0,
                      "failed": 0, "budget_skipped": 0, "dropped_claims": 0, "raised": 0,
                      "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}

    def analyze(self, event):
        base = self.fallback.analyze(event)
        source = entry_text(event.payload, self.limits.max_input_chars)
        meta = {"model": self.llm.model, "prompt_version": self.prompt_version}
        try:
            parsed = self.model_output(event.event_id, source)
        except Unavailable as exc:
            self.stats["fallbacks"] += 1
            return merge(base, None, {**meta, "status": exc.status})
        verified, dropped = verify(parsed, source)
        self.stats["dropped_claims"] += dropped
        result = merge(base, verified, {**meta, "status": "ok", "dropped_claims": dropped})
        self.stats["raised"] += result.details["llm"]["raised"]
        return result

    def record(self, event_id, key, status, reply=None, error=None, text=None):
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO llm_calls(call_id,event_id,input_hash,provider,model,prompt_version,status,input_tokens,"
                "output_tokens,cached_tokens,latency_ms,response_text,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, event_id, key, self.llm.provider, self.llm.model, self.prompt_version, status,
                 reply.input_tokens if reply else 0, reply.output_tokens if reply else 0,
                 reply.cached_tokens if reply else 0, reply.latency_ms if reply else 0,
                 text if text is not None else (reply.text if reply else None), error, self.now().isoformat()))

    def tokens_today(self):
        start = self.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        row = self.store.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) AS n FROM llm_calls WHERE created_at>=?",
                                 (start,)).fetchone()
        return row["n"]

    def model_output(self, event_id, source):
        # The prompt text and provider options (e.g. thinking on or off) change the answer, so they are part of the key.
        key = hashlib.sha256(json.dumps([self.llm.provider, self.llm.model, self.prompt_version, self.prompt,
                                         self.llm.extra_body, source], sort_keys=True).encode()).hexdigest()
        cached = self.store.execute(
            "SELECT response_text FROM llm_calls WHERE input_hash=? AND model=? AND prompt_version=? AND status='ok' "
            "ORDER BY created_at DESC LIMIT 1", (key, self.llm.model, self.prompt_version)).fetchone()
        if cached:
            self.stats["cached"] += 1
            self.record(event_id, key, "cached", text=cached["response_text"])
            return self.parse_output(cached["response_text"])
        messages = [{"role": "system", "content": self.prompt},
                    {"role": "user", "content": f"<{self.tag}>\n{source}\n</{self.tag}>"}]
        estimate = (len(self.prompt) + len(source)) // 3 + self.output_limit
        for attempt in range(2):
            if self.stats["calls"] >= self.limits.max_calls_per_run or \
                    self.tokens_today() + estimate > self.limits.max_tokens_per_day:
                self.stats["budget_skipped"] += 1
                raise Unavailable("budget")
            self.stats["calls"] += 1
            try:
                reply = self.client.complete(messages, json_mode=True, max_tokens=self.output_limit)
            except LLMError as exc:
                self.record(event_id, key, "failed", error=str(exc))
                if exc.transient and attempt == 0:
                    continue
                self.stats["failed"] += 1
                raise Unavailable("failed") from None
            for field in ("input_tokens", "output_tokens", "cached_tokens"):
                self.stats[field] += getattr(reply, field)
            if not reply.text.strip():
                # The provider documents occasional empty JSON-mode replies; retry once (as for a dropped connection).
                self.record(event_id, key, "failed", reply, error="Empty response")
                if attempt == 0:
                    continue
                self.stats["failed"] += 1
                raise Unavailable("failed")
            try:
                parsed = self.parse_output(reply.text)
            except InvalidOutput as exc:
                self.stats["invalid"] += 1
                self.record(event_id, key, "invalid", reply, error=str(exc))
                raise Unavailable("invalid") from None
            self.record(event_id, key, "ok", reply)
            return parsed
        raise Unavailable("failed")
