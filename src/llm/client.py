"""LLM provider adapters behind one interface (src.core.interfaces.LLMClient).

Not wired into the pipeline until Milestone 2. To support a provider whose API is not
OpenAI-compatible, write a class with complete() and register it under a new name.
"""
import requests

from src.core.config import ConfigError

PROVIDERS = {}


class LLMError(RuntimeError):
    """Provider failure. Messages never include keys, prompts or response bodies."""


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

    def complete(self, messages, *, json_mode=False):
        body = {"model": self.settings.model, "messages": messages, "temperature": 0}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = "Bearer " + self.settings.api_key
        try:
            response = self.session.post(self.settings.base_url + "/chat/completions", json=body, headers=headers,
                                         timeout=(10, self.settings.timeout_seconds), allow_redirects=False)
        except requests.RequestException as exc:
            raise LLMError(f"LLM request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise LLMError(f"LLM provider returned HTTP {response.status_code}")
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("Unexpected LLM response shape") from None
