"""CV profiles: a cut-off answer is kept and retried, and a saved CV's profile is reused by later runs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.llm.client import LLMError, LLMReply, create_client
from src.llm.config import LLMSettings
from src.matching.run import Model, prompt_version
from src.web.jobs import Jobs, write_json

ENV = {"LLM_PROVIDER": "openai_compatible", "LLM_BASE_URL": "http://localhost:1", "LLM_MODEL": "test-model"}
SETTINGS = LLMSettings.from_env(ENV)
CV = "<cv>\n[Page 1]\nJane Doe, security engineer\n</cv>"
PROFILE = '{"name": "Jane Doe"}'


class FakeClient:
    """Answers in turn: an LLMError is raised, text is returned."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def complete(self, messages, *, json_mode=False, max_tokens=None):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, LLMError):
            raise answer
        return LLMReply(answer, 100, 20)


def model(directory, *answers):
    result = Model(SETTINGS, Path(directory) / "cache")
    result.client = FakeClient(*answers)
    return result


def earlier_run(root, run_id, cv, trace, version=None, model_name="test-model"):
    folder = root / run_id
    folder.mkdir(parents=True)
    write_json(folder / "status.json", {"state": "done", "cv": cv, "model": model_name, "steps": [], "error": None,
                                        "prompt_version": version or prompt_version(), "started": "2026-11-04T00:00:00"})
    write_json(folder / "trace.json", trace)


class CutOffTests(unittest.TestCase):
    def test_client_keeps_the_cut_off_text_out_of_the_message(self):
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=200, json=lambda: {
            "choices": [{"message": {"content": '{"name": "Ja'}, "finish_reason": "length"}]})
        with self.assertRaises(LLMError) as caught:
            create_client(SETTINGS, session).complete([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual((str(caught.exception), caught.exception.cut_off), ("LLM output reached max_tokens", '{"name": "Ja'))

    def test_cut_off_answer_is_kept_and_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            m = model(directory, LLMError("LLM output reached max_tokens", cut_off='{"name": "Jane'), PROFILE)
            self.assertEqual(m.ask("expert_cv_v1", CV, json.loads, 12000, label="CV"), {"name": "Jane Doe"})
            self.assertEqual(m.client.calls, 2)
            cut = m.trace[0]
            self.assertEqual((cut["output"], cut["error"]), ('{"name": "Jane', "Cut off at the 12000-token output limit"))
            self.assertNotIn("error", m.trace[1])

    def test_cut_off_twice_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            cut = lambda: LLMError("LLM output reached max_tokens", cut_off="{")
            m = model(directory, cut(), cut())
            with self.assertRaisesRegex(LLMError, "max_tokens"):
                m.ask("expert_cv_v1", CV, json.loads, 12000)
            self.assertEqual([t.get("error") for t in m.trace], ["Cut off at the 12000-token output limit"] * 2)


class ReuseTests(unittest.TestCase):
    def test_saved_cv_profile_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            item = {"step": "expert_cv_v1", "label": "CV", "input": CV, "output": PROFILE, "cached": False}
            earlier_run(root, "20261104-000000-aaaaaa", "jane.pdf", [{"step": "extract_cv", "label": "jane.pdf"}, item])
            jobs = Jobs(root, SETTINGS, [], Path(directory), 1000, Path(directory), {})
            m = model(directory)  # No answers: any model call would fail.
            self.assertEqual(jobs.reuse_profiles(m, "jane.pdf"), 1)
            self.assertEqual(m.ask("expert_cv_v1", CV, json.loads, 12000), {"name": "Jane Doe"})
            self.assertEqual((m.client.calls, m.stats["cached"]), (0, 1))

    def test_not_reused_across_prompts_models_cvs_or_failed_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            item = {"step": "expert_cv_v1", "label": "CV", "input": CV, "output": PROFILE, "cached": False}
            earlier_run(root, "20261101-000000-aaaaaa", "jane.pdf", [item], version="00000000")
            earlier_run(root, "20261102-000000-bbbbbb", "jane.pdf", [item], model_name="other-model")
            earlier_run(root, "20261103-000000-cccccc", "other.pdf", [item])
            earlier_run(root, "20261104-000000-dddddd", "jane.pdf", [{**item, "error": "Cut off at the 12000-token output limit"}])
            jobs = Jobs(root, SETTINGS, [], Path(directory), 1000, Path(directory), {})
            m = model(directory, PROFILE)
            self.assertEqual(jobs.reuse_profiles(m, "jane.pdf"), 0)
            m.ask("expert_cv_v1", CV, json.loads, 12000)
            self.assertEqual(m.client.calls, 1)

    def test_replaced_cv_is_profiled_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            item = {"step": "expert_cv_v1", "label": "CV", "input": CV, "output": PROFILE, "cached": False}
            earlier_run(root, "20261104-000000-aaaaaa", "jane.pdf", [item])
            jobs = Jobs(root, SETTINGS, [], Path(directory), 1000, Path(directory), {})
            m = model(directory, '{"name": "Jane Q. Doe"}')
            jobs.reuse_profiles(m, "jane.pdf")
            new_cv = CV.replace("security engineer", "security engineer and expert witness")
            self.assertEqual(m.ask("expert_cv_v1", new_cv, json.loads, 12000), {"name": "Jane Q. Doe"})
            self.assertEqual(m.client.calls, 1)


if __name__ == "__main__":
    unittest.main()
