"""LLM router and agents.

Every test here runs offline. A suite that calls a paid provider is slow,
flaky, and quietly expensive — and it would fail when someone else's API has a
bad minute rather than when our code breaks.

The property under test throughout: the LLM layer is *optional*. No key, no
budget, a dead provider or a garbled reply must all degrade to None so the
deterministic pipeline carries on.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from careeragent.llm.agents import _clean_extraction, extract_job, score_jobs
from careeragent.llm.router import Budget, LLMRouter, Tier, extract_json
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
from careeragent.models.job import RawJob
from careeragent.pipeline.normalize import normalize

# ─── JSON recovery ──────────────────────────────────────────────────────────


def test_extract_json_plain() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_from_fenced_block() -> None:
    """Models emit code fences even when told not to."""
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("```\n[1, 2]\n```") == [1, 2]


def test_extract_json_ignores_preamble() -> None:
    assert extract_json('Sure! Here is the result:\n{"a": 1}') == {"a": 1}


def test_extract_json_gives_up_cleanly() -> None:
    assert extract_json("I cannot help with that.") is None
    assert extract_json("") is None


# ─── Budget ─────────────────────────────────────────────────────────────────


def test_budget_blocks_once_the_ceiling_is_reached(tmp_path: Path) -> None:
    budget = Budget(0.10, tmp_path / "b.json")
    assert budget.allows()
    budget.record(0.09)
    assert budget.allows()
    budget.record(0.02)
    assert not budget.allows()
    assert budget.remaining == 0.0


def test_budget_survives_a_restart(tmp_path: Path) -> None:
    """A spend ledger that resets on restart is not a ceiling."""
    path = tmp_path / "b.json"
    Budget(1.0, path).record(0.42)
    assert Budget(1.0, path).spent == pytest.approx(0.42)


def test_budget_tolerates_a_corrupt_ledger(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    path.write_text("not json", encoding="utf-8")
    assert Budget(1.0, path).spent == 0.0


# ─── Router ─────────────────────────────────────────────────────────────────


@pytest.fixture
def router(tmp_path, monkeypatch) -> LLMRouter:
    monkeypatch.setenv("CAREERAGENT_CACHE_DIR", str(tmp_path / "cache"))
    from careeragent import config as config_module

    config_module.get_settings.cache_clear()
    r = LLMRouter(budget=Budget(1.0, tmp_path / "budget.json"))
    r.cache_dir = tmp_path / "llmcache"
    r.cache_dir.mkdir(parents=True, exist_ok=True)
    yield r
    config_module.get_settings.cache_clear()


def fake_response(text: str, cost: float = 0.001, tokens: int = 100):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=tokens, completion_tokens=tokens),
        _hidden_params={"response_cost": cost},
    )


def test_router_reports_unavailable_without_keys(router, monkeypatch) -> None:
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: None)
    assert router.chain(Tier.CHEAP) == []
    assert not router.available()
    assert router.complete("hi", agent="test") is None


def test_router_returns_none_rather_than_raising_when_all_models_fail(router, monkeypatch) -> None:
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")

    import litellm

    monkeypatch.setattr(
        litellm, "completion", lambda **_k: (_ for _ in ()).throw(RuntimeError("provider down"))
    )
    assert router.complete("hi", agent="test") is None


def test_router_falls_over_to_the_next_model(router, monkeypatch) -> None:
    """Providers retire models without warning, which is the entire reason a
    chain exists."""
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")
    calls: list[str] = []

    import litellm

    def completion(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise RuntimeError("model_not_found")
        return fake_response('{"ok": true}')

    monkeypatch.setattr(litellm, "completion", completion)
    result = router.complete("hi", agent="test")
    assert result is not None
    assert len(calls) == 2
    assert result.model == calls[1]


def test_router_caches_identical_calls(router, monkeypatch) -> None:
    """The same job description is re-analysed on every run; caching is what
    makes repeat runs nearly free."""
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")
    hits: list[int] = []

    import litellm

    def completion(**_kwargs):
        hits.append(1)
        return fake_response('{"ok": true}')

    monkeypatch.setattr(litellm, "completion", completion)
    first = router.complete("same prompt", agent="test")
    second = router.complete("same prompt", agent="test")

    assert len(hits) == 1, "second call hit the provider"
    assert second.cache_hit and not first.cache_hit
    assert second.cost_usd == 0.0


def test_router_stops_at_the_budget_ceiling(router, monkeypatch) -> None:
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **_k: fake_response("x", cost=2.0))
    assert router.complete("first", agent="test") is not None
    assert router.complete("second", agent="test") is None, "spent past the ceiling"


def test_router_rejects_an_empty_reply(router, monkeypatch) -> None:
    """gpt-oss models burn the token budget on hidden reasoning and return ''.
    Treating that as an answer would propagate an empty extraction."""
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **_k: fake_response(""))
    assert router.complete("hi", agent="test") is None


def test_router_enforces_a_token_floor(router, monkeypatch) -> None:
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")
    seen: dict = {}
    import litellm

    def completion(**kwargs):
        seen.update(kwargs)
        return fake_response('{"ok": 1}')

    monkeypatch.setattr(litellm, "completion", completion)
    router.complete("hi", max_tokens=10, agent="test")
    assert seen["max_tokens"] >= router.settings.llm_min_max_tokens


def test_complete_json_returns_none_on_garbage(router, monkeypatch) -> None:
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **_k: fake_response("no json here"))
    assert router.complete_json("hi", agent="test") is None


# ─── Extraction cleaning ────────────────────────────────────────────────────


def test_clean_extraction_coerces_model_sloppiness() -> None:
    """Models return strings where lists belong, '3-5' for a number, and 'N/A'
    where null belongs."""
    cleaned = _clean_extraction(
        {
            "required_skills": "Python",
            "preferred_skills": ["SQL", "N/A", "  "],
            "min_years_experience": "3-5 years",
            "seniority": "Senior",
            "employment_type": "Full Time",
            "work_mode": "REMOTE",
            "sponsorship_required": "yes",
            "domain": "fintech",
        }
    )
    assert cleaned["required_skills"] == ["Python"]
    assert cleaned["preferred_skills"] == ["SQL"]
    assert cleaned["min_years_experience"] == 3.0
    assert cleaned["seniority"] == "senior"
    assert cleaned["employment_type"] == "full_time"
    assert cleaned["work_mode"] == "remote"
    # "yes" is not a boolean; a non-bool must not become one.
    assert cleaned["sponsorship_required"] is None


def test_clean_extraction_rejects_invented_enum_values() -> None:
    cleaned = _clean_extraction({"seniority": "godlike", "work_mode": "hybrid-ish"})
    assert cleaned["seniority"] is None
    assert cleaned["work_mode"] is None


# ─── Agents ─────────────────────────────────────────────────────────────────


def make_job(**kwargs):
    base = {
        "source": "greenhouse",
        "source_job_id": "1",
        "title": "ML Engineer",
        "url": "https://x/1",
        "company_name": "Acme",
        "description": "We need Python and PyTorch. 3 years experience.",
    }
    return normalize(RawJob(**{**base, **kwargs}))


def make_candidate() -> Candidate:
    return Candidate(
        name="Test",
        facts=[
            Fact(
                kind=FactKind.SKILL,
                label="python",
                evidence_source=EvidenceSource.MASTER_RESUME,
                evidence_locator="cv.pdf#page=1,line=1",
            )
        ],
    )


def test_extract_job_returns_none_without_a_description(router) -> None:
    assert extract_job(make_job(description=""), router) is None


def test_extract_job_parses_a_good_reply(router, monkeypatch) -> None:
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")
    payload = json.dumps(
        {"required_skills": ["Python", "PyTorch"], "seniority": "mid", "keywords": ["ml"]}
    )
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **_k: fake_response(payload))
    data = extract_job(make_job(), router)
    assert data is not None
    assert data["required_skills"] == ["Python", "PyTorch"]
    assert data["seniority"] == "mid"


def test_score_jobs_maps_results_back_to_the_right_jobs(router, monkeypatch) -> None:
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")
    jobs = [
        make_job(source_job_id="1", title="ML Engineer"),
        make_job(source_job_id="2", title="Data Analyst", url="https://x/2"),
    ]
    payload = json.dumps(
        {
            "scores": [
                {"i": 0, "score": 0.9, "strengths": ["python"], "missing": ["aws"]},
                {"i": 1, "score": 0.2, "concerns": ["seniority gap"]},
            ]
        }
    )
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **_k: fake_response(payload))
    scores = score_jobs(make_candidate(), jobs, router)

    assert scores[jobs[0].job_id].score == 0.9
    assert scores[jobs[0].job_id].missing_skills == ["aws"]
    assert scores[jobs[1].job_id].score == 0.2


def test_score_jobs_drops_out_of_range_indices(router, monkeypatch) -> None:
    """A hallucinated index must not attach a score to the wrong job — that
    would silently mis-rank someone's applications."""
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")
    jobs = [make_job()]
    payload = json.dumps({"scores": [{"i": 7, "score": 0.9}, {"i": 0, "score": 0.5}]})
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **_k: fake_response(payload))
    scores = score_jobs(make_candidate(), jobs, router)
    assert len(scores) == 1
    assert scores[jobs[0].job_id].score == 0.5


def test_score_jobs_clamps_out_of_range_scores(router, monkeypatch) -> None:
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: "key")
    jobs = [make_job()]
    import litellm

    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **_k: fake_response(json.dumps({"scores": [{"i": 0, "score": 7.5}]})),
    )
    assert score_jobs(make_candidate(), jobs, router)[jobs[0].job_id].score == 1.0


def test_score_jobs_returns_empty_when_the_llm_is_unavailable(router, monkeypatch) -> None:
    """The pipeline must carry on with stage-1/2 results."""
    monkeypatch.setattr("careeragent.llm.router.get_api_key", lambda _p: None)
    assert score_jobs(make_candidate(), [make_job()], router) == {}


def test_apply_extraction_never_overwrites_parsed_evidence() -> None:
    """The deterministic parse wins on anything it already resolved. The model
    fills blanks; it does not overrule the document."""
    from careeragent.llm import apply_extraction
    from careeragent.models.job import Seniority

    job = make_job(title="Senior ML Engineer")
    assert job.seniority is Seniority.SENIOR

    apply_extraction(job, {"seniority": "intern", "required_skills": ["Go"]})
    assert job.seniority is Seniority.SENIOR, "LLM overwrote a parsed field"
    assert job.requirements == ["Go"], "LLM should fill an empty field"
