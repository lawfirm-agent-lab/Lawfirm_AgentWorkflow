"""Pipeline settings from config.yaml plus environment overrides. Never prints secrets."""
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

ROOT = Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    """Configuration problem with a message that is safe to print."""


@dataclass(frozen=True)
class Limits:
    """CourtListener allows 5/minute, 50/hour, 125/day per account (rolling windows).
    Headroom keeps capacity for manual tools sharing the same token."""
    per_minute: int = 5
    per_hour: int = 50
    per_day: int = 125
    headroom_per_hour: int = 5
    headroom_per_day: int = 25
    min_interval_seconds: int = 13
    max_requests_per_run: int = 20
    max_pages_per_docket: int = 5


@dataclass(frozen=True)
class LLMLimits:
    """Spend controls for the LLM analyzer. Exceeding any of them falls back to rules."""
    max_calls_per_run: int = 40
    max_tokens_per_day: int = 400000
    max_input_chars: int = 12000
    max_output_tokens: int = 800
    max_document_chars: int = 60000
    max_document_output_tokens: int = 4000


@dataclass(frozen=True)
class Settings:
    analyzer: str = "rules"
    max_events_per_digest: int = 50
    source_lag_seconds: int = 120
    limits: Limits = field(default_factory=Limits)
    llm: LLMLimits = field(default_factory=LLMLimits)


def _section(cls, data, name):
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config.yaml: '{name}' must be a mapping")
    known = {f.name: f for f in fields(cls) if f.name not in ("limits", "llm")}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"config.yaml: unknown keys in '{name}': {', '.join(sorted(unknown))}")
    for key, value in data.items():
        if known[key].type in (int, "int") and (type(value) is not int or value < 0):
            raise ConfigError(f"config.yaml: '{name}.{key}' must be a non-negative integer")
        if known[key].type is str and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"config.yaml: '{name}.{key}' must be a non-empty string")
    return data


def load_settings(path=ROOT / "config.yaml", env=os.environ):
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read {Path(path).name}: {type(exc).__name__}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a mapping")
    pipeline = _section(Settings, raw.get("pipeline"), "pipeline")
    limits = Limits(**_section(Limits, raw.get("courtlistener"), "courtlistener"))
    if limits.per_hour <= limits.headroom_per_hour or limits.per_day <= limits.headroom_per_day:
        raise ConfigError("config.yaml: headroom must be smaller than the matching limit")
    llm = LLMLimits(**_section(LLMLimits, raw.get("llm"), "llm"))
    if not all((llm.max_input_chars, llm.max_output_tokens, llm.max_document_chars, llm.max_document_output_tokens)):
        raise ConfigError("config.yaml: llm character and output token limits must be positive")
    settings = Settings(**pipeline, limits=limits, llm=llm)
    analyzer = env.get("ANALYZER", "").strip()
    return replace(settings, analyzer=analyzer) if analyzer else settings


def secure_database_url(url):
    """Require a PostgreSQL URI with TLS and add conservative server-side timeouts."""
    url = (url or "").strip()
    if not url:
        raise ConfigError("DATABASE_URL is missing or empty")
    if not url.startswith(("postgresql://", "postgres://")):
        raise ConfigError("DATABASE_URL must be a PostgreSQL URI")
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    mode = query.get("sslmode", "require")
    if mode not in ("require", "verify-ca", "verify-full"):
        raise ConfigError("DATABASE_URL must use TLS (sslmode=require, verify-ca or verify-full)")
    query.update(sslmode=mode, options="-c statement_timeout=30000 -c lock_timeout=10000 -c search_path=public")
    return urlunsplit(parts._replace(query=urlencode(query)))
