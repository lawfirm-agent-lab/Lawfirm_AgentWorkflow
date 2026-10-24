"""Choose the analyzer by name (config.yaml pipeline.analyzer or the ANALYZER variable).

rules         description rules only
llm           rules plus a verified LLM reading (falls back to rules on any failure)
rules+shadow  rules decide; the LLM runs alongside and is stored for comparison only
"""
import os

from src.analyzers.document import DocumentAnalyzer
from src.analyzers.llm import LLMAnalyzer
from src.analyzers.rules import RulesAnalyzer
from src.core.config import ConfigError, Settings
from src.core.models import SHADOW_ANALYZER
from src.llm.client import create_client
from src.llm.config import LLMSettings

NAMES = ("rules", "llm", "rules+shadow")


def check_analyzer(name, env=os.environ):
    """Validate the choice and its provider settings before any database work."""
    if name not in NAMES:
        raise ConfigError(f"Unknown analyzer {name!r}; available: {', '.join(NAMES)}")
    if name != "rules":
        LLMSettings.from_env(env)


def create_analyzer(name, env=os.environ, *, store=None, settings=None, session=None):
    check_analyzer(name, env)
    if name == "rules":
        return RulesAnalyzer()
    llm_settings = LLMSettings.from_env(env)
    limits = (settings or Settings()).llm
    llm = LLMAnalyzer(store, create_client(llm_settings, session), llm_settings, limits,
                      name="llm" if name == "llm" else SHADOW_ANALYZER)
    if name == "llm":
        return llm
    rules = RulesAnalyzer()
    rules.shadow = llm
    return rules


def create_document_analyzer(env=os.environ, *, store, settings=None, session=None):
    """Full-document briefing with the configured LLM provider (see src.analyzers.document)."""
    llm_settings = LLMSettings.from_env(env)
    return DocumentAnalyzer(store, create_client(llm_settings, session), llm_settings, (settings or Settings()).llm)
