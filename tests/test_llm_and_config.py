"""Provider-neutral LLM settings, adapter registry, analyzer selection and runner config."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from src.analyzers.llm import PROMPT_VERSION
from src.analyzers.registry import create_analyzer
from src.analyzers.rules import RulesAnalyzer
from src.core.config import ConfigError, load_settings, secure_database_url
from src.llm.client import LLMError, create_client
from src.llm.config import LLMSettings
from src.pipeline import runner

DEEPSEEK = {"LLM_PROVIDER": "openai_compatible", "LLM_MODEL": "any-model-name",
            "LLM_BASE_URL": "https://api.deepseek.com/", "LLM_API_KEY": "sk-secret"}


class LLMSettingsTests(unittest.TestCase):
    def test_any_model_is_configuration(self):
        settings = LLMSettings.from_env(DEEPSEEK)
        self.assertEqual((settings.model, settings.base_url), ("any-model-name", "https://api.deepseek.com"))
        self.assertNotIn("sk-secret", repr(settings))
        local = LLMSettings.from_env({"LLM_PROVIDER": "openai_compatible", "LLM_MODEL": "llama",
                                      "LLM_BASE_URL": "http://localhost:11434/v1"})
        self.assertEqual(local.api_key, "")

    def test_rejects_unsafe_settings(self):
        for change in ({"LLM_MODEL": ""}, {"LLM_API_KEY": ""}, {"LLM_BASE_URL": "http://api.example.com"},
                       {"LLM_BASE_URL": "https://user:pw@api.example.com"}, {"LLM_TIMEOUT_SECONDS": "0"},
                       {"LLM_EXTRA_BODY": "not json"}, {"LLM_EXTRA_BODY": '{"model": "other"}'}):
            with self.subTest(change=change), self.assertRaises(ConfigError) as error:
                LLMSettings.from_env({**DEEPSEEK, **change})
            self.assertNotIn("sk-secret", str(error.exception))

    def test_openai_compatible_request(self):
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=200, json=lambda: {"choices": [{"message": {"content": "{}"}}]})
        client = create_client(LLMSettings.from_env(DEEPSEEK), session)
        session.post.return_value.json = lambda: {"choices": [{"message": {"content": "{}"}}],
                                                  "usage": {"prompt_tokens": 51, "completion_tokens": 21,
                                                            "prompt_cache_hit_tokens": 7}}
        client = create_client(LLMSettings.from_env({**DEEPSEEK, "LLM_EXTRA_BODY": '{"thinking": {"type": "disabled"}}'}), session)
        reply = client.complete([{"role": "user", "content": "hi"}], json_mode=True, max_tokens=300)
        self.assertEqual((reply.text, reply.input_tokens, reply.output_tokens, reply.cached_tokens), ("{}", 51, 21, 7))
        call = session.post.call_args
        self.assertEqual(call.kwargs["json"]["thinking"], {"type": "disabled"})
        self.assertEqual(call.kwargs["json"]["max_tokens"], 300)
        self.assertEqual(call.args[0], "https://api.deepseek.com/chat/completions")
        self.assertEqual(call.kwargs["json"]["model"], "any-model-name")
        self.assertEqual(call.kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer sk-secret")
        self.assertFalse(call.kwargs["allow_redirects"])

    def test_provider_errors_hide_details(self):
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=401, text="invalid key sk-secret")
        with self.assertRaises(LLMError) as error:
            create_client(LLMSettings.from_env(DEEPSEEK), session).complete([])
        self.assertNotIn("sk-secret", str(error.exception))
        with self.assertRaises(ConfigError):
            create_client(LLMSettings.from_env({**DEEPSEEK, "LLM_PROVIDER": "unknown"}))

    def test_transient_errors_and_cut_off_output(self):
        client = lambda session: create_client(LLMSettings.from_env(DEEPSEEK), session)
        cases = {"dropped connection": (requests.ConnectionError("reset"), True),
                 "timeout": (requests.ReadTimeout("slow"), True)}
        for name, (exc, transient) in cases.items():
            with self.subTest(name=name), self.assertRaises(LLMError) as error:
                session = MagicMock()
                session.post.side_effect = exc
                client(session).complete([])
            self.assertEqual(error.exception.transient, transient)
        for status, transient in ((503, True), (429, False), (400, False)):
            with self.subTest(status=status), self.assertRaises(LLMError) as error:
                client(MagicMock(**{"post.return_value": MagicMock(status_code=status)})).complete([])
            self.assertEqual(error.exception.transient, transient)
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=200)
        session.post.return_value.json = lambda: {"choices": [{"message": {"content": '{"a": '}, "finish_reason": "length"}]}
        with self.assertRaises(LLMError) as error:
            client(session).complete([])
        self.assertIn("max_tokens", str(error.exception))
        self.assertFalse(error.exception.transient)


class AnalyzerAndConfigTests(unittest.TestCase):
    def test_analyzer_selection(self):
        self.assertIsInstance(create_analyzer("rules", {}), RulesAnalyzer)
        self.assertEqual(create_analyzer("llm", DEEPSEEK).version, f"{PROMPT_VERSION}:any-model-name")
        with self.assertRaises(ConfigError):
            create_analyzer("llm", {})
        with self.assertRaises(ConfigError):
            create_analyzer("magic", {})

    def write(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_settings_validation_and_override(self):
        settings = load_settings(self.write("pipeline:\n  analyzer: rules\ncourtlistener:\n  per_day: 100\n"), {"ANALYZER": "llm"})
        self.assertEqual((settings.analyzer, settings.limits.per_day), ("llm", 100))
        self.assertEqual(load_settings(self.write("llm:\n  max_calls_per_run: 3\n"), {}).llm.max_calls_per_run, 3)
        for text in ("courtlistener:\n  per_day: -1\n", "pipeline:\n  unknown: 1\n", "llm:\n  max_input_chars: 0\n",
                     "courtlistener:\n  per_day: 20\n  headroom_per_day: 25\n", "pipeline:\n  analyzer: ''\n"):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_settings(self.write(text), {})
        self.assertEqual(load_settings().limits.per_day, 125)

    def test_database_url_requires_tls(self):
        url = secure_database_url("postgresql://u:p@db.example.com:5432/postgres")
        self.assertIn("sslmode=require", url)
        self.assertIn("statement_timeout", url)
        for bad in ("", "mysql://x", "postgresql://u:p@h/db?sslmode=disable"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as error:
                secure_database_url(bad)
            self.assertNotIn("u:p", str(error.exception))

    def test_runner_fails_safely_without_secrets(self):
        with patch.dict("os.environ", {"COURTLISTENER_TOKEN": "", "DATABASE_URL": ""}, clear=False), \
                patch("builtins.print") as printed:
            self.assertEqual(runner.main([]), 1)
            self.assertIn("COURTLISTENER_TOKEN", printed.call_args.args[0])
            self.assertEqual(runner.main(["--stages", "route", "--dry-run"]), 1)
            self.assertIn("DATABASE_URL", printed.call_args.args[0])

    def test_runner_local_sqlite_dry_run(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), patch("builtins.print"):
            self.assertEqual(runner.main(["--sqlite", str(Path(directory) / "p.db"), "--stages",
                                          "analyze,route,deliver", "--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
