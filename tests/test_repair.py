"""The shared revision helper, and the two agents that gained a round.

The reason this is one module rather than three similar functions is a single instruction:
*remove the unsupported claim, do not argue that it was supported*. A model asked to fix an
evidence violation will otherwise reach for wording that sounds supported, and three copies
of that rule would be three chances for one of them to soften.

So the first tests here assert the rule is present and says what it must say. The rest check
the two agents that now use it — cover letters and outreach — where the rejection is usually
one clause of an otherwise usable draft.
"""

from __future__ import annotations

import pytest

from careeragent.agents import repair
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
from careeragent.models.job import RawJob
from careeragent.pipeline.normalize import normalize_all


def _fact(kind, label, **kw):
    return Fact(
        kind=kind,
        label=label,
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#page=1,line=1",
        **kw,
    )


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(
        name="Asha Raman",
        email="asha@example.com",
        facts=[
            _fact(FactKind.SKILL, "python"),
            _fact(FactKind.SKILL, "sql"),
            _fact(
                FactKind.PROJECT,
                "Sales Report",
                body="Built reporting pipelines in Python and SQL.",
            ),
        ],
    )


@pytest.fixture
def job():
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


class ScriptedRouter:
    """Returns each scripted reply in turn, then None. Records the agent of each call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    available = staticmethod(lambda: True)

    def complete_json(self, prompt, **kwargs):
        self.calls.append(kwargs.get("agent", "?"))
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None


# ─── The rule that must not drift ───────────────────────────────────────────


def test_the_revision_rules_say_remove_not_justify():
    """The whole point of sharing this. A model told to "fix" an evidence violation will
    look for supported-sounding wording unless it is told plainly to delete the claim."""
    rules = repair.REVISION_RULES.lower()
    assert "removing" in rules or "remove" in rules
    assert "do not argue" in rules
    assert "do not substitute a different unsupported claim" in rules
    # And an honest empty answer has to stay available, or the model will invent something
    # to fill the space it just cleared.
    assert "nothing survives" in rules


def test_the_complaint_reaches_the_model_verbatim():
    """ "mentions kubernetes with no evidence" is specific enough to act on. A summarised
    complaint produces a vaguer rewrite."""
    prompt = repair.build_prompt(
        complaint="mentions kubernetes with no evidence",
        original="I ran a Kubernetes platform.",
        facts="f1 :: skill :: python",
        task="answer",
        output_shape='{"text": "..."}',
    )
    assert "mentions kubernetes with no evidence" in prompt
    assert repair.REVISION_RULES in prompt
    assert "I ran a Kubernetes platform." in prompt
    assert "f1 :: skill :: python" in prompt


def test_only_one_round_is_ever_allowed():
    assert repair.MAX_ROUNDS == 1


def test_a_revision_is_asked_for_at_temperature_zero(candidate):
    """A second sample at the temperature that produced the violation is not a correction."""
    router = ScriptedRouter({"text": "ok", "cite": ["f1"], "confident": True})
    captured: dict = {}

    def complete_json(prompt, **kwargs):
        captured.update(kwargs)
        return {"text": "ok", "cite": ["f1"], "confident": True}

    router.complete_json = complete_json  # type: ignore[method-assign]
    repair.revise(
        agent="x",
        system="s",
        complaint="c",
        original="o",
        facts="f",
        task="answer",
        output_shape="{}",
        router=router,
        visible_tokens=400,
    )
    assert captured["temperature"] == 0.0
    assert captured["agent"] == "x_repair"


def test_an_unusable_reply_is_none_not_an_exception():
    router = ScriptedRouter("not a dict")
    assert (
        repair.revise(
            agent="x",
            system="s",
            complaint="c",
            original="o",
            facts="f",
            task="answer",
            output_shape="{}",
            router=router,
            visible_tokens=400,
        )
        is None
    )


# ─── Uncited revisions are refused ──────────────────────────────────────────


def test_a_revision_with_nothing_cited_is_refused():
    """The agent was asked to keep what the facts back. Citing nothing means it did not,
    and accepting that would let a confident empty claim through."""
    assert repair.cited_text({"text": "Something.", "cite": []}, "text", 500) is None


def test_a_revision_the_agent_is_unsure_of_is_refused():
    assert (
        repair.cited_text({"text": "Maybe.", "cite": ["f1"], "confident": False}, "text", 500)
        is None
    )


def test_a_usable_revision_comes_back_with_its_citations():
    got = repair.cited_text({"text": "  Built  pipelines. ", "cite": ["f1", "f2"]}, "text", 500)
    assert got == ("Built pipelines.", ["f1", "f2"])


# ─── Cover letters ──────────────────────────────────────────────────────────


def test_a_letter_rejected_for_flattery_is_revised(candidate, job):
    """Invented enthusiasm is a claim about the candidate's history with this company that
    nothing supports — and it is almost always one sentence of an otherwise fine letter."""
    from careeragent.applications import cover_letter

    ids = [str(f.id) for f in candidate.facts]
    # One reply: `_repair_letter` is the revision call itself, so the script holds only what
    # the model says when told what was wrong.
    router = ScriptedRouter(
        {
            "body": "I build reporting pipelines in Python and SQL.",
            "cite": ids[:2],
            "confident": True,
        },
    )
    draft = cover_letter._repair_letter(
        "I have always admired Acme. I build reporting pipelines in Python and SQL.",
        "contains unsupported flattery: always admired",
        candidate,
        job,
        router,
    )

    assert draft is not None
    body, cited = draft
    assert "admired" not in body.lower()
    assert cited == ids[:2]
    assert router.calls == ["cover_letter_repair"]


def test_a_letter_revision_keeps_its_paragraphs(candidate, job):
    """`cited_text` collapses whitespace, which is right for a one-paragraph answer and
    wrong for a letter. The letter path rebuilds the breaks from the raw value."""
    from careeragent.applications import cover_letter

    ids = [str(f.id) for f in candidate.facts]
    router = ScriptedRouter(
        {
            "body": "I build reporting pipelines in Python.\n\nI would like to discuss the role.",
            "cite": ids[:1],
            "confident": True,
        }
    )
    result = cover_letter._repair_letter("x", "y", candidate, job, router)

    assert result is not None
    assert "\n\n" in result[0], "the letter came back as one run-on paragraph"


def test_an_unusable_letter_revision_is_none(candidate, job):
    from careeragent.applications import cover_letter

    router = ScriptedRouter({"body": "", "cite": [], "confident": True})
    assert cover_letter._repair_letter("x", "y", candidate, job, router) is None


# ─── Outreach ───────────────────────────────────────────────────────────────


def test_an_outreach_message_is_revised(candidate):
    from careeragent.applications import outreach

    ids = [str(f.id) for f in candidate.facts]
    router = ScriptedRouter(
        {
            "body": "I build reporting pipelines in Python and SQL.",
            "cite": ids[:1],
            "confident": True,
        }
    )
    result = outreach._repair_message(
        "I ran a Kubernetes platform for six years.",
        "mentions kubernetes with no evidence",
        candidate,
        ["f1 :: skill :: python"],
        router,
    )

    assert result is not None
    assert "kubernetes" not in result[0].lower()
    assert router.calls == ["outreach_repair"]


def test_a_revised_outreach_message_is_still_only_a_draft():
    """A revision that satisfies the checks has earned the right to be shown to the user,
    not the right to leave. There is no send transport and no tool that sends one."""
    from careeragent import tools
    from careeragent.applications import outreach

    assert tools.REGISTRY.get("send_outreach") is None
    assert tools.REGISTRY.get("send_message") is None
    # The draft type carries `ok`, which means "worth showing you", never "sent".
    assert "sent" not in outreach.Draft.__dataclass_fields__
