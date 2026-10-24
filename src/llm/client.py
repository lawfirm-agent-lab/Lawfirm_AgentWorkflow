"""LLM provider adapters behind one interface (src.core.interfaces.LLMClient).

Not wired into the pipeline until Milestone 2. To support a provider whose API is not
OpenAI-compatible, write a class with complete() and register it under a new name.
"""
import time
from dataclasses import dataclass

import requests

from src.core.config import ConfigError

PROVIDERS = {}


class LLMError(RuntimeError):
    """Provider failure. Messages never include keys, prompts or response bodies.
    transient: a dropped connection, timeout or 5xx, worth one retry."""

    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class LLMReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0


def register(name):
    def decorator(cls):
        PROVIDERS[name] = cls
        return cls
    return decorator


def create_client(settings, session=None):
    if settings.provider not in PROVIDERS:
        raise ConfigError(f"Unknown LLM_PROVIDER {settings.provider!r}; available: {', '.join(sorted(PROVIDERS))}")
    return PROVIDERS[settings.provider](settings, session)


@register("openai_compatible")
class OpenAICompatibleClient:
    """Chat Completions API, served by DeepSeek, OpenAI and many other providers."""

    def __init__(self, settings, session=None):
        self.settings = settings
        self.session = session or requests.Session()

    def complete(self, messages, *, json_mode=False, max_tokens=None):
        body = {**self.settings.extra_body, "model": self.settings.model, "messages": messages, "temperature": 0}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = "Bearer " + self.settings.api_key
        started = time.monotonic()
        try:
            response = self.session.post(self.settings.base_url + "/chat/completions", json=body, headers=headers,
                                         timeout=(10, self.settings.timeout_seconds), allow_redirects=False)
        except requests.RequestException as exc:
            transient = isinstance(exc, (requests.ConnectionError, requests.Timeout))
            raise LLMError(f"LLM request failed: {type(exc).__name__}", transient) from None
        if response.status_code != 200:
            raise LLMError(f"LLM provider returned HTTP {response.status_code}", response.status_code >= 500)
        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("Unexpected LLM response shape") from None
        if choice.get("finish_reason") == "length":
            # Cut-off JSON would only fail parsing; name the real cause (thinking tokens count too).
            raise LLMError("LLM output reached max_tokens")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        count = lambda *keys: next((usage[k] for k in keys if type(usage.get(k)) is int), 0)
        return LLMReply(text, count("prompt_tokens"), count("completion_tokens"),
                        count("prompt_cache_hit_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
                        int((time.monotonic() - started) * 1000))
