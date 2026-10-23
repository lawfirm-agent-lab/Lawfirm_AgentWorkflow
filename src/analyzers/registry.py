"""Choose the analyzer by name (config.yaml pipeline.analyzer or the ANALYZER variable)."""
import os

from src.analyzers.rules import RulesAnalyzer
from src.core.config import ConfigError
from src.llm.config import LLMSettings

ANALYZERS = {"rules": lambda env: RulesAnalyzer()}


def create_analyzer(name, env=os.environ):
    if name == "llm":
        LLMSettings.from_env(env)  # Validate the provider settings now so they are ready.
        raise ConfigError("The LLM analyzer is scheduled for Milestone 2; use ANALYZER=rules for now")
    if name not in ANALYZERS:
        raise ConfigError(f"Unknown analyzer {name!r}; available: {', '.join(sorted(ANALYZERS))}, llm")
    return ANALYZERS[name](env)
