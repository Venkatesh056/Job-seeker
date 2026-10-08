"""Application question answering and cover letters.

The property under test throughout: an ungrounded answer is never returned as usable.
`REQUIRES_USER` is a correct outcome, and most of these tests assert that the system
reaches it rather than producing something plausible.
"""

from __future__ import annotations

from datetime import date

import pytest

from careeragent.applications import answers as qa
from careeragent.applications import cover_letter as cl
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
from careeragent.models.job import NormalizedJob


def fact(kind: FactKind, label: str, body: str = "", **kwargs) -> Fact:
    return Fact(
        kind=kind,
        label=label,
        body=body,
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#page=1,line=1",
        **kwargs,
    )


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(
        name="Asha Raman",
        facts=[
            fact(FactKind.SKILL, "python"),
            fact(FactKind.SKILL, "pytorch"),
            fact(
                FactKind.EXPERIENCE,
                "ML Intern, Nexora",
                body="Fine-tuned Whisper for speech recognition.",
                first_used_at=date(2023, 1, 1),
                last_used_at=date(2025, 1, 1),
            ),
            fact(FactKind.EDUCATION, "B.E. Computer Science"),
        ],
    )


@pytest.fixture
def job() -> NormalizedJob:
    return NormalizedJob(
        job_id="j1", company="Acme", title="ML Engineer", description="Build models."
    )


class _Router:
    def __init__(self, payload, available=True):
        self.payload = payload
        self._available = available

    def available(self):
        return self._available

    def complete_json(self, *args, **kwargs):
        return self.payload

    def complete(self, *args, **kwargs):
        class Result:
            model = "test/model"

            def json(_self):
                return self.payload

        return Result() if self.payload is not None else None


# ─── Never-answer ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field_id",
    [
        "authorization.sponsorship",
        "authorization.work_authorized",
        "demographic.gender",
        "demographic.race",
        "demographic.veteran",
        "demographic.disability",
        "legal.background_check",
    ],
)
def test_sensitive_fields_are_never_answered(field_id, candidate, job):
    """No router, no autonomy level and no override can change this outcome."""
    result = qa.answer("anything at all", field_id, candidate, job, use_llm=True)
    assert result.outcome is qa.Outcome.NEVER_ANSWER
    assert not result.usable


def test_sensitive_field_is_not_answered_even_with_a_willing_model(candidate, job):
    router = _Router({"text": "No sponsorship needed.", "cite": ["x"], "confident": True})
    result = qa.answer(
        "Do you require sponsorship?", "authorization.sponsorship", candidate, job, router=router
    )
    assert result.outcome is qa.Outcome.NEVER_ANSWER


# ─── Commitments ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "Are you willing to relocate?",
        "What are your salary expectations?",
        "What is your notice period?",
        "Do you consent to a background check?",
    ],
)
def test_commitments_go_to_the_user(question, candidate, job):
    """A stale "yes, I'll relocate" is a promise made on someone's behalf."""
    result = qa.answer(question, "question.freeform:abc", candidate, job, use_llm=False)
    assert result.outcome is qa.Outcome.REQUIRES_USER


def test_a_commitment_ignores_question_memory(candidate, job):
    result = qa.answer(
        "Are you willing to relocate?",
        "question.freeform:abc",
        candidate,
        job,
        remembered="Yes, happily",
        use_llm=False,
    )
    assert result.outcome is qa.Outcome.REQUIRES_USER


# ─── Deterministic answers ──────────────────────────────────────────────────


def test_years_of_experience_comes_from_dated_facts(candidate, job):
    result = qa.answer(
        "Years of experience", "experience.years_total", candidate, job, use_llm=False
    )
    assert result.outcome is qa.Outcome.ANSWERED
    assert result.text == "2"
    assert result.fact_ids


def test_years_of_experience_without_dates_escalates(job):
    """A number here with nothing behind it is exactly the claim never to guess."""
    bare = Candidate(name="X", facts=[fact(FactKind.SKILL, "python")])
    result = qa.answer("Years of experience", "experience.years_total", bare, job, use_llm=False)
    assert result.outcome is qa.Outcome.REQUIRES_USER
    assert "invented" in result.reason


# ─── Grounding ──────────────────────────────────────────────────────────────


def test_grounding_rejects_an_invented_technology(candidate):
    reason = qa.check_grounding("I have deployed models on Kubernetes.", ["skills"], candidate)
    assert "kubernetes" in reason


def test_grounding_rejects_an_overstated_duration(candidate):
    reason = qa.check_grounding("I have 8 years of Python experience.", ["skills"], candidate)
    assert "8 years" in reason


def test_grounding_rejects_a_dangling_citation(candidate):
    reason = qa.check_grounding("I worked with Python.", ["not-a-fact"], candidate)
    assert "do not exist" in reason


def test_grounding_accepts_a_supported_answer(candidate):
    assert qa.check_grounding("I worked with Python and PyTorch.", ["skills"], candidate) == ""


def test_ungrounded_model_answer_is_downgraded(candidate, job):
    """The model does not get the last word."""
    router = _Router(
        {"text": "I have deep Kubernetes expertise.", "cite": ["skills"], "confident": True}
    )
    result = qa.answer_with_llm(
        "Tell us about your infrastructure work", "question.freeform:a", candidate, job, router
    )
    assert result.outcome is qa.Outcome.REQUIRES_USER
    assert "kubernetes" in result.reason


def test_grounded_model_answer_is_kept(candidate, job):
    router = _Router(
        {
            "text": "I fine-tuned Whisper for speech recognition during my internship.",
            "cite": ["skills"],
            "confident": True,
        }
    )
    result = qa.answer_with_llm("Describe a project", "question.freeform:a", candidate, job, router)
    assert result.outcome is qa.Outcome.ANSWERED


def test_a_model_that_says_it_is_unsure_is_believed(candidate, job):
    router = _Router({"text": "Probably something.", "cite": [], "confident": False})
    result = qa.answer_with_llm("Why us?", "question.freeform:a", candidate, job, router)
    assert result.outcome is qa.Outcome.REQUIRES_USER


def test_no_llm_escalates_rather_than_guessing(candidate, job):
    result = qa.answer(
        "Why do you want to work here?",
        "question.freeform:a",
        candidate,
        job,
        router=_Router(None, available=False),
    )
    assert result.outcome is qa.Outcome.REQUIRES_USER


# ─── Question memory ────────────────────────────────────────────────────────


def test_normalize_question_collapses_the_company(candidate):
    a = qa.normalize_question("Why do you want to work at Acme?")
    b = qa.normalize_question("Why do you want to work at Globex?")
    assert a == b


def test_remembered_answer_is_rechecked_before_reuse(candidate, job):
    """A memory whose grounding no longer holds must not be silently reused."""
    stale = qa.answer(
        "Describe your infrastructure experience",
        "question.freeform:a",
        candidate,
        job,
        remembered="I run large Kubernetes clusters.",
        use_llm=False,
    )
    assert stale.outcome is qa.Outcome.REQUIRES_USER


def test_valid_remembered_answer_is_reused(candidate, job):
    result = qa.answer(
        "Describe a project you are proud of",
        "question.freeform:a",
        candidate,
        job,
        remembered="I fine-tuned Whisper for speech recognition.",
        use_llm=False,
    )
    assert result.outcome is qa.Outcome.ANSWERED
    assert result.source == "memory"


# ─── Cover letters ──────────────────────────────────────────────────────────


def test_cover_letter_needs_an_llm(candidate, job):
    """No deterministic fallback by design: an unfluent letter is worse than none."""
    draft = cl.generate(candidate, job, router=_Router(None, available=False))
    assert not draft.ok
    assert "no LLM" in draft.reason


def test_cover_letter_rejects_invented_flattery(candidate, job):
    router = _Router(
        {
            "body": "I have long admired your work in machine learning. I fine-tuned Whisper.",
            "cite": ["skills"],
            "confident": True,
        }
    )
    draft = cl.generate(candidate, job, router=router)
    assert not draft.ok
    assert "flattery" in draft.reason


def test_cover_letter_rejects_an_invented_technology(candidate, job):
    router = _Router(
        {"body": "I build production Kubernetes platforms.", "cite": ["skills"], "confident": True}
    )
    draft = cl.generate(candidate, job, router=router)
    assert not draft.ok
    assert "kubernetes" in draft.reason


def test_cover_letter_accepts_a_grounded_draft(candidate, job):
    router = _Router(
        {
            "body": "I fine-tuned Whisper for speech recognition and work in Python and PyTorch.",
            "cite": ["skills"],
            "confident": True,
        }
    )
    draft = cl.generate(candidate, job, router=router)
    assert draft.ok
    assert draft.word_count > 0


def test_greeting_is_assembled_not_generated():
    """A model that guesses a hiring manager's name will get it wrong."""
    draft = cl.CoverLetterDraft(body="Body text.", ok=True)
    assert draft.with_greeting("Acme").startswith("Dear Acme Hiring Team,")
    assert draft.with_greeting("Acme", "Priya").startswith("Dear Priya,")


@pytest.mark.parametrize(
    "text",
    [
        "I have long admired your mission.",
        "I am a huge fan of your product.",
        "Your industry-leading platform is why I applied.",
    ],
)
def test_flattery_detector(text):
    assert cl.check_flattery(text)


def test_plain_text_is_not_flagged_as_flattery():
    assert not cl.check_flattery("I built a speech model and would like to do more of that.")


# ─── The repair round ───────────────────────────────────────────────────────


def _job_for_repair():
    from careeragent.models.job import RawJob
    from careeragent.pipeline.normalize import normalize_all

    return normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id="r1",
                title="Data Analyst",
                url="https://boards.greenhouse.io/acme/jobs/r1",
                company_name="Acme",
                location="Bangalore, India",
                description="Reporting with Python and SQL.",
                ats="greenhouse",
            )
        ]
    )[0]


def _candidate_for_repair():
    from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind

    def f(kind, label, **kw):
        return Fact(
            kind=kind,
            label=label,
            evidence_source=EvidenceSource.MASTER_RESUME,
            evidence_locator="cv.pdf#page=1,line=1",
            **kw,
        )

    return Candidate(
        name="Asha Raman",
        facts=[
            f(FactKind.SKILL, "python"),
            f(FactKind.SKILL, "sql"),
            f(
                FactKind.PROJECT,
                "Sales Report",
                body="Built reporting pipelines in Python and SQL.",
            ),
        ],
    )


class _ScriptedRouter:
    """Returns each scripted reply in turn, then None."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[str] = []

    def complete_json(self, prompt, **kwargs):
        self.calls.append(kwargs.get("agent", "?"))
        return self.replies.pop(0) if self.replies else None


def test_an_answer_rejected_for_one_phrase_gets_corrected():
    """Measured on the real Fact Bank: an answer citing five facts was rejected for
    "mentions data visualization with no evidence", and the whole thing went to the user to
    edit. One clause away from correct is worth one more call."""
    from careeragent.applications.answers import Outcome, answer_with_llm

    candidate = _candidate_for_repair()
    ids = [str(f.id) for f in candidate.facts]
    router = _ScriptedRouter(
        {
            "text": "I build reporting pipelines in Python and SQL, and I use Kubernetes daily.",
            "cite": ids[:2],
            "confident": True,
        },
        {
            "text": "I build reporting pipelines in Python and SQL.",
            "cite": ids[:2],
            "confident": True,
        },
    )

    answer = answer_with_llm(
        "Describe your skills.", "experience.summary", candidate, _job_for_repair(), router
    )

    assert answer.outcome is Outcome.ANSWERED
    assert "kubernetes" not in answer.text.lower()
    # Lower than a first-time pass: the agent needed telling, and anything weighing how much
    # to trust this should see the difference.
    assert answer.confidence == 0.65
    assert router.calls == ["application_question", "application_question_repair"]


def test_an_answer_the_facts_do_not_support_still_reaches_the_user():
    """`requires_user` is a first-class outcome, not a failure to engineer away."""
    from careeragent.applications.answers import Outcome, answer_with_llm

    candidate = _candidate_for_repair()
    ids = [str(f.id) for f in candidate.facts]
    unsupported = {
        "text": "I have seven years of Kubernetes experience.",
        "cite": ids[:1],
        "confident": True,
    }
    router = _ScriptedRouter(unsupported, unsupported)

    answer = answer_with_llm(
        "Describe your skills.", "experience.summary", candidate, _job_for_repair(), router
    )

    assert answer.outcome is Outcome.REQUIRES_USER
    assert answer.reason
    # The text is preserved so the user can edit rather than start over.
    assert answer.text


def test_the_repair_round_runs_at_most_once():
    from careeragent.applications.answers import answer_with_llm

    candidate = _candidate_for_repair()
    ids = [str(f.id) for f in candidate.facts]
    bad = {"text": "I ran a Kubernetes platform.", "cite": ids[:1], "confident": True}
    router = _ScriptedRouter(bad, bad, bad, bad)

    answer_with_llm(
        "Describe your skills.", "experience.summary", candidate, _job_for_repair(), router
    )
    assert router.calls == ["application_question", "application_question_repair"]


def test_a_grounded_first_answer_costs_no_repair_call():
    from careeragent.applications.answers import Outcome, answer_with_llm

    candidate = _candidate_for_repair()
    router = _ScriptedRouter(
        {
            "text": "I build reporting pipelines in Python and SQL.",
            "cite": [str(candidate.facts[0].id)],
            "confident": True,
        }
    )

    answer = answer_with_llm(
        "Describe your skills.", "experience.summary", candidate, _job_for_repair(), router
    )

    assert answer.outcome is Outcome.ANSWERED
    assert answer.confidence == 0.75
    assert router.calls == ["application_question"]
