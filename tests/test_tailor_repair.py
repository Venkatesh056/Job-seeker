"""Truncation, the repair round, and the rewrite budget.

This file exists because of a bug that produced no error and no failing test: resume
tailoring was falling back to the deterministic document on essentially every job, after
paying for the model call, and the log blamed the model.

    llm_unparseable_json  preview='{"summary": "Candidate with 0.2 years of profess...'

The JSON was fine. It had been cut off at `max_tokens`, and a preview of the first 120
characters of a truncated reply looks exactly like a preview of a valid one. Worse, the
fragment was cached, so subsequent runs failed instantly on a stored fragment rather than on
a live call.

Measured on four real jobs: 0 of 4 used the model's document before, 4 of 4 after.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
from careeragent.models.job import RawJob
from careeragent.pipeline.normalize import normalize_all
from careeragent.resume import tailor as tailor_mod


def fact(kind: FactKind, label: str, **kwargs) -> Fact:
    return Fact(
        kind=kind,
        label=label,
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#page=1,line=1",
        **kwargs,
    )


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(
        name="Asha Raman",
        email="asha@example.com",
        location="Bangalore, India",
        facts=[
            fact(FactKind.SKILL, "python"),
            fact(FactKind.SKILL, "sql"),
            fact(
                FactKind.PROJECT,
                "Resume Ranker",
                body="Built a ranking pipeline in Python over a SQL store.",
            ),
        ],
    )


@pytest.fixture
def job():
    return normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id="1",
                title="Data Analyst",
                url="https://boards.greenhouse.io/acme/jobs/1",
                company_name="Acme",
                location="Bangalore, India",
                description="Work with Python and SQL on reporting pipelines.",
                ats="greenhouse",
            )
        ]
    )[0]


# ─── The cap scales with the work ───────────────────────────────────────────


def test_the_rewrite_budget_grows_with_the_number_of_lines():
    """A flat cap truncated fifteen bullets mid-array on every real document."""
    assert tailor_mod._rewrite_budget(1) < tailor_mod._rewrite_budget(15)
    assert tailor_mod._rewrite_budget(15) >= 4000, (
        "fifteen bullets plus a summary plus the reasoning these models do before "
        "emitting needs real room; 3400 was measured running out at 3396"
    )


def test_a_budget_is_allowed_for_zero_lines():
    """Guards a division-by-zero-shaped mistake: no lines still needs a summary."""
    assert tailor_mod._rewrite_budget(0) > 0


# ─── Truncation is visible ──────────────────────────────────────────────────


class FakeChoice:
    def __init__(self, content: str, finish_reason: str) -> None:
        self.message = SimpleNamespace(content=content)
        self.finish_reason = finish_reason


def _fake_response(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[FakeChoice(content, finish_reason)],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=2396),
        _hidden_params={"response_cost": 0.01},
    )


@pytest.fixture
def router(tmp_path, monkeypatch):
    """A real router with a temp cache, and litellm replaced."""
    from careeragent import config as config_module
    from careeragent.llm.router import LLMRouter

    monkeypatch.setenv("CAREERAGENT_CACHE_DIR", str(tmp_path))
    # A list field, so the environment form is JSON.
    monkeypatch.setenv("CAREERAGENT_LLM_CHAIN_STRONG", '["groq/fake-model"]')
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    config_module.get_settings.cache_clear()
    yield LLMRouter()
    config_module.get_settings.cache_clear()


def test_a_truncated_reply_is_reported_as_truncated(router, monkeypatch, caplog):
    """Not as "unparseable". The two look identical in a preview and call for opposite
    responses — raise the cap, or stop trusting the model."""
    import litellm

    monkeypatch.setattr(
        litellm, "completion", lambda **_k: _fake_response('{"summary": "cut off he', "length")
    )
    result = router.complete("x", agent="probe")

    assert result is not None
    assert result.truncated is True


def test_a_complete_reply_is_not_flagged(router, monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **_k: _fake_response('{"ok": true}', "stop"))
    result = router.complete("x", agent="probe")

    assert result is not None
    assert result.truncated is False


def test_a_truncated_reply_is_never_cached(router, monkeypatch):
    """The compounding failure: a stored fragment is served forever, so later runs fail
    instantly on a cached fragment instead of on a live call. Three of four jobs in the
    original measurement failed this way."""
    import litellm

    calls = {"n": 0}

    def counting(**_kwargs):
        calls["n"] += 1
        return _fake_response('{"summary": "cut off he', "length")

    monkeypatch.setattr(litellm, "completion", counting)
    router.complete("same prompt", agent="probe")
    router.complete("same prompt", agent="probe")

    assert calls["n"] == 2, "a truncated reply was cached and replayed"


def test_a_complete_reply_is_cached(router, monkeypatch):
    """The cache still has to work, or every run pays twice."""
    import litellm

    calls = {"n": 0}

    def counting(**_kwargs):
        calls["n"] += 1
        return _fake_response('{"ok": true}', "stop")

    monkeypatch.setattr(litellm, "completion", counting)
    router.complete("same prompt", agent="probe")
    second = router.complete("same prompt", agent="probe")

    assert calls["n"] == 1
    assert second is not None and second.cache_hit


# ─── The prompt asks for less ───────────────────────────────────────────────


def test_the_rewrite_prompt_asks_only_for_changed_lines():
    """Asking for every line back cost a full resume of output per call, which is what ran
    into the cap. The parser has always applied updates by index, so a partial list works."""
    from careeragent import prompts

    template = prompts.get("tailor_resume").template
    assert "ONLY the lines you actually changed" in template
    assert prompts.get("tailor_resume").declared_version == "1.1", (
        "the contract changed; a decision recorded under 1.0 must not be attributed to it"
    )


# ─── The repair round ───────────────────────────────────────────────────────


def test_a_rejected_rewrite_gets_one_chance_to_be_corrected(candidate, job, monkeypatch):
    """The hybrid pattern: the validator vetoes, the agent answers, the validator decides.

    First reply smuggles in an unevidenced technology. Second removes it. The accepted
    document is the corrected one, flagged `repaired` so a document the model was talked
    into getting right is distinguishable from one it got right.
    """
    replies = [
        {
            "summary": "Analyst with Kubernetes expertise.",
            "lines": [{"i": 0, "text": "Built a ranking pipeline using Kubernetes."}],
        },
        {
            "summary": "Analyst who builds ranking pipelines.",
            "lines": [{"i": 0, "text": "Built a ranking pipeline in Python."}],
        },
    ]

    class Router:
        available = staticmethod(lambda: True)

        def complete_json(self, *_args, **_kwargs):
            return replies.pop(0) if replies else None

    result = tailor_mod.tailor(candidate, job, use_llm=True, router=Router())

    assert not result.fell_back, result.fallback_reason
    assert result.repaired is True
    assert result.report.ok
    assert "kubernetes" not in result.resume.summary.lower()


def test_a_rewrite_that_cannot_be_corrected_still_falls_back(candidate, job):
    """The floor. Two bad attempts and the user gets the honest deterministic document."""

    class Router:
        available = staticmethod(lambda: True)

        def complete_json(self, *_args, **_kwargs):
            return {
                "summary": "Expert with 9 years of Kubernetes and Terraform.",
                "lines": [{"i": 0, "text": "Led a Kubernetes platform team for 9 years."}],
            }

    result = tailor_mod.tailor(candidate, job, use_llm=True, router=Router())

    assert result.fell_back
    assert result.repaired is False
    assert result.resume.generated_by == "deterministic"
    assert result.fallback_reason


def test_the_repair_round_runs_at_most_once(candidate, job):
    """Two rounds cost twice as much to fix a document the model has already shown it
    cannot fix."""
    calls = {"n": 0}

    class Router:
        available = staticmethod(lambda: True)

        def complete_json(self, *_args, **_kwargs):
            calls["n"] += 1
            return {
                "summary": "Expert with 9 years of Kubernetes.",
                "lines": [{"i": 0, "text": "Led a Kubernetes team for 9 years."}],
            }

    tailor_mod.tailor(candidate, job, use_llm=True, router=Router())
    assert calls["n"] == 1 + tailor_mod.MAX_REPAIR_ROUNDS


def test_a_first_time_pass_is_not_marked_repaired(candidate, job):
    class Router:
        available = staticmethod(lambda: True)

        def complete_json(self, *_args, **_kwargs):
            return {
                "summary": "Analyst who builds ranking pipelines in Python.",
                "lines": [{"i": 0, "text": "Built a ranking pipeline in Python."}],
            }

    result = tailor_mod.tailor(candidate, job, use_llm=True, router=Router())
    assert result.report.ok
    assert result.repaired is False
    assert result.to_dict()["repaired"] is False


def test_a_model_that_returns_nothing_falls_back_without_a_repair_attempt(candidate, job):
    """There is nothing to repair, and a round asking the model to fix an absence is a
    round spent on nothing."""
    calls = {"n": 0}

    class Router:
        available = staticmethod(lambda: True)

        def complete_json(self, *_args, **_kwargs):
            calls["n"] += 1
            return None

    result = tailor_mod.tailor(candidate, job, use_llm=True, router=Router())

    assert result.fell_back
    assert calls["n"] == 1
    assert "nothing usable" in result.fallback_reason
