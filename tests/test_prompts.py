"""The prompt registry — spec §48.

These tests exist because of what a prompt regression looks like: nothing fails, nothing
logs, and the system starts producing confident text that is not true. The suite cannot
check that a model obeys a rule, but it can check that the rule is still present, still
shared, and still identified in the audit trail.

The structural assertion is the load-bearing one. `test_every_prompt_touching_facts_is_grounded`
would fail the moment someone adds a prompt that reads candidate facts without the shared
rules — which is exactly the mistake that produced five diverging copies in the first place.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from careeragent import prompts

# ─── The rules are shared, not copied ───────────────────────────────────────


def test_every_prompt_touching_facts_is_grounded():
    """A prompt that sees candidate or company facts must carry the shared rules.

    Adding a prompt is the moment the anti-fabrication guarantee is easiest to lose, so
    the default is grounded and opting out has to be deliberate and visible here.
    """
    ungrounded = [p.name for p in prompts.all_prompts() if not p.grounded]
    assert ungrounded == [], f"these prompts bypass the shared grounding rules: {ungrounded}"


@pytest.mark.parametrize("prompt", prompts.all_prompts(), ids=lambda p: p.name)
def test_the_grounding_rules_are_present_verbatim(prompt):
    """Present verbatim, not paraphrased. A paraphrase is how a rule softens."""
    assert prompts.GROUNDING in prompt.system


@pytest.mark.parametrize("prompt", prompts.all_prompts(), ids=lambda p: p.name)
def test_every_prompt_forbids_prose_around_its_json(prompt):
    """Every one of these is parsed as JSON. A model that adds commentary breaks the
    caller, which then falls back — silently losing the LLM's contribution."""
    assert prompts.JSON_ONLY in prompt.system


def test_the_rules_still_name_the_things_that_must_never_be_invented():
    """A canary on the shared block itself.

    Each of these words is in `GROUNDING` because a real ATS form or resume line has been
    filled in wrongly without it. Deleting one is a silent loosening, so it fails here.
    """
    rules = prompts.GROUNDING.lower()
    for forbidden in (
        "skill",
        "employer",
        "project",
        "metric",
        "duration",
        "degree",
        "certification",
        "job title",
        "years of experience",
        "seniority",
    ):
        assert forbidden in rules, f"the grounding rules no longer mention {forbidden!r}"
    # Untrusted text must be named as data, not instructions (§75).
    assert "never\n  instructions to follow" in rules or "instructions to follow" in rules


# ─── Versioning ─────────────────────────────────────────────────────────────


def test_a_version_identifies_the_text_not_just_the_number():
    """An edited prompt gets a new version even if nobody bumps the declared one.

    This is what keeps `agent_decision.prompt_version` honest: two decisions made under
    different instructions can never be recorded as having used the same prompt.
    """
    original = prompts.get("cover_letter")
    edited = prompts.Prompt(
        name=original.name,
        declared_version=original.declared_version,
        system=original.system + "\n- And one more rule.",
        template=original.template,
    )
    assert edited.declared_version == original.declared_version
    assert edited.version != original.version


def test_versions_are_stable_across_calls():
    """A version that changed per process would be useless in the trail."""
    assert prompts.get("answer_question").version == prompts.get("answer_question").version


def test_every_name_is_unique():
    names = [p.name for p in prompts.all_prompts()]
    assert len(names) == len(set(names))


def test_registering_a_duplicate_name_is_refused():
    """Silently replacing a prompt would change behaviour with no diff at the call site."""
    with pytest.raises(ValueError, match="already registered"):
        prompts.register("cover_letter", "2.0", system="x", template="y")


def test_an_unknown_prompt_names_the_known_ones():
    with pytest.raises(KeyError, match="answer_question"):
        prompts.get("no_such_prompt")


# ─── Rendering ──────────────────────────────────────────────────────────────


def test_rendering_preserves_the_literal_json_braces():
    """The templates contain example JSON. If `format` ate the braces, the model would be
    shown a broken schema and would improvise one."""
    rendered = prompts.get("answer_question").render(
        question="Why this role?",
        title="ML Engineer",
        company="Acme",
        job_context="Build models.",
        facts="fact-1 :: skill :: python",
    )
    assert '"cite":' in rendered
    assert '{"text":' in rendered
    assert "{question}" not in rendered


def test_a_missing_value_fails_loudly():
    """Better a KeyError at the call site than a prompt with an empty CANDIDATE FACTS
    section, which is how a model ends up inventing the contents."""
    with pytest.raises(KeyError):
        prompts.get("answer_question").render(question="Why?")


@pytest.mark.parametrize("prompt", prompts.all_prompts(), ids=lambda p: p.name)
def test_every_template_renders_with_its_own_placeholders(prompt):
    """Catches a template whose placeholders drifted from any caller's keyword arguments."""
    import string

    fields = {
        name
        for _, name, _, _ in string.Formatter().parse(prompt.template)
        if name and not name.isdigit()
    }
    rendered = prompt.render(**dict.fromkeys(fields, "X"))
    assert "X" in rendered or not fields


# ─── The call sites actually use it ─────────────────────────────────────────


def test_the_modules_that_talk_to_models_read_from_the_registry():
    """§69: a registry nothing imports is decoration.

    Asserted on the objects the modules hold, so this fails if a module goes back to a
    private string literal.
    """
    import careeragent.company as company
    from careeragent.applications import answers, cover_letter, outreach
    from careeragent.llm import agents
    from careeragent.resume import tailor

    assert agents.EXTRACTION is prompts.get("extract_job")
    assert agents.SCORING is prompts.get("score_jobs")
    assert tailor.REWRITE is prompts.get("tailor_resume")
    assert answers.ANSWER is prompts.get("answer_question")
    assert cover_letter.LETTER is prompts.get("cover_letter")
    assert outreach.MESSAGE is prompts.get("outreach_message")
    assert company.SUMMARY is prompts.get("company_summary")


def test_the_registry_is_readable_over_http():
    from careeragent.api import app

    with TestClient(app) as client:
        body = client.get("/api/v1/decisions/prompts").json()
        assert body["count"] == len(prompts.all_prompts())
        assert body["ungrounded"] == []
        names = {p["name"] for p in body["prompts"]}
        assert "tailor_resume" in names
        # The text itself is deliberately not served: it is policy, and an endpoint that
        # returned it would invite a client to edit and re-send it.
        assert all("system" not in p for p in body["prompts"])
