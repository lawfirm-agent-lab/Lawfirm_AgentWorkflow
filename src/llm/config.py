"""Provider-neutral LLM settings. The model is always configuration, never code.

LLM_PROVIDER  adapter name, e.g. openai_compatible (DeepSeek, OpenAI, Qwen, Kimi,
              OpenRouter, Ollama, vLLM ...). Add other adapters in src/llm/client.py.
LLM_BASE_URL  provider endpoint, e.g. https://api.deepseek.com
LLM_MODEL     model name exactly as the provider spells it
LLM_API_KEY   secret; optional only for local endpoints
LLM_EXTRA_BODY  optional JSON object merged into each request for provider-specific
              switches, e.g. {"thinking": {"type": "disabled"}} for DeepSeek
"""
import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from src.core.config import ConfigError

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    timeout_seconds: int = 60
    extra_body: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env=os.environ):
        values = {k: env.get(k, "").strip() for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY")}
        missing = [k for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL") if not values[k]]
        if missing:
            raise ConfigError("Missing LLM settings: " + ", ".join(missing))
        url = urlsplit(values["LLM_BASE_URL"])
        local = url.hostname in LOCAL_HOSTS
        if url.scheme != "https" and not (local and url.scheme == "http"):
            raise ConfigError("LLM_BASE_URL must use https (plain http only for localhost)")
        if url.username or url.password or url.query or url.fragment:
            raise ConfigError("LLM_BASE_URL must not contain credentials, a query or a fragment")
        if not values["LLM_API_KEY"] and not local:
            raise ConfigError("Missing LLM settings: LLM_API_KEY")
        try:
            timeout = int(env.get("LLM_TIMEOUT_SECONDS", "60"))
        except ValueError:
            raise ConfigError("LLM_TIMEOUT_SECONDS must be an integer") from None
        if not 1 <= timeout <= 600:
            raise ConfigError("LLM_TIMEOUT_SECONDS must be between 1 and 600")
        try:
            extra = json.loads(env.get("LLM_EXTRA_BODY", "").strip() or "{}")
        except ValueError:
            raise ConfigError("LLM_EXTRA_BODY must be a JSON object") from None
        if not isinstance(extra, dict) or set(extra) & {"model", "messages"}:
            raise ConfigError("LLM_EXTRA_BODY must be a JSON object without model or messages")
        return cls(values["LLM_PROVIDER"], values["LLM_MODEL"], values["LLM_BASE_URL"].rstrip("/"),
                   values["LLM_API_KEY"], timeout, extra)
