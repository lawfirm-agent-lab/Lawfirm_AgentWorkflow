"""Provider-neutral LLM settings. The model is always configuration, never code.

LLM_PROVIDER  adapter name, e.g. openai_compatible (DeepSeek, OpenAI, Qwen, Kimi,
              OpenRouter, Ollama, vLLM ...). Add other adapters in src/llm/client.py.
LLM_BASE_URL  provider endpoint, e.g. https://api.deepseek.com
LLM_MODEL     model name exactly as the provider spells it
LLM_API_KEY   secret; optional only for local endpoints
LLM_EXTRA_BODY  optional JSON object merged into each request for provider-specific
              switches, e.g. {"thinking": {"type": "disabled"}} for DeepSeek

Evaluation tools can switch to a named profile (PROFILES) without touching LLM_*; the
profile's key lives in its own variable, e.g. OPENROUTER_API_KEY.
"""
import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from src.core.config import ConfigError

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# name -> (base URL, variable holding its API key)
PROFILES = {"openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY")}


def profile_env(env, profile=None, model=None, extra_body=None):
    """A copy of env that targets a named profile and/or model. Provider-specific switches
    (LLM_EXTRA_BODY) are not carried over to another provider unless given again."""
    env = dict(env)
    if profile:
        if profile not in PROFILES:
            raise ConfigError(f"Unknown LLM profile {profile!r}; available: {', '.join(PROFILES)}")
        if not model:
            raise ConfigError("Name the model to use with an LLM profile")
        base_url, key_name = PROFILES[profile]
        if not env.get(key_name, "").strip():
            raise ConfigError(f"Missing {key_name}")
        env.update(LLM_PROVIDER="openai_compatible", LLM_BASE_URL=base_url, LLM_API_KEY=env[key_name], LLM_EXTRA_BODY="")
    if model:
        env["LLM_MODEL"] = model
    if extra_body is not None:
        env["LLM_EXTRA_BODY"] = extra_body
    return env


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
