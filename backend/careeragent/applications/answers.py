"""Application question answering.

Three outcomes, and the third is a feature:

    ANSWERED        grounded in named facts
    REQUIRES_USER   we do not know, or the answer is a commitment only you can make
    NEVER_ANSWER    a legal declaration or demographic question

`REQUIRES_USER` is the default, not the exception. Most open application questions
("why do you want to work here?") are answerable from evidence; some ("are you
willing to relocate?") are commitments; and a system that guesses at either will
eventually put something on a real application that the candidate has to defend
in an interview.

Generated answers pass the same grounding check the resume validator uses: an
answer that introduces a technology the Fact Bank cannot support is downgraded to
REQUIRES_USER rather than returned. The model does not get the last word.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any

from ..browser.catalog import BY_ID, Sensitivity, is_freeform
from ..logging import get_logger
from ..models.candidate import Candidate, FactKind
from ..models.job import NormalizedJob
from ..prompts import get as get_prompt
from ..resume.validate import _supported_terms, _technology_tokens

log = get_logger("applications.answers")

MAX_ANSWER_CHARS = 1400


class Outcome(enum.StrEnum):
    ANSWERED = "answered"
    REQUIRES_USER = "requires_user"
    NEVER_ANSWER = "never_answer"


@dataclass
class Answer:
    question: str
    canonical_field: str
    outcome: Outcome
    text: str = ""
    fact_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    source: str = "generated"  # generated | memory | deterministic | user

    @property
    def usable(self) -> bool:
        return self.outcome is Outcome.ANSWERED and bool(self.text.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "canonical_field": self.canonical_field,
            "outcome": self.outcome.value,
            "text": self.text,
            "fact_ids": list(self.fact_ids),
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "source": self.source,
        }


def normalize_question(text: str) -> str:
    """A stable key for question memory.

    Company and role names are stripped so "Why do you want to work at Acme?" and
    "Why do you want to work at Globex?" share a memory entry — the *shape* is
    reusable even though the answer is not.
    """
    lowered = " ".join((text or "").lower().split())
    lowered = re.sub(r"[^\w\s?]", "", lowered)
    lowered = re.sub(
        r"\b(at|for|with)\s+[a-z0-9][\w.&-]*(\s+(inc|ltd|llc|gmbh|pvt|plc))?\b",
        "at <company>",
        lowered,
    )
    return lowered.strip()[:500]


# ─── Question classification ────────────────────────────────────────────────

# Questions that are commitments rather than facts. The candidate answers these,
# every time, even when a previous answer exists — circumstances change and a
# stale "yes, I'll relocate" is a promise made on their behalf.
COMMITMENT_PATTERNS = (
    r"willing to relocat",
    r"willing to travel",
    r"able to (start|commence|join)",
    r"salary expectation|expected (ctc|compensation|salary)",
    r"notice period",
    r"consent",
    r"agree to",
    r"do you accept",
)

# Questions whose answer is a plain fact we hold.
FACTUAL_HANDLERS: dict[str, str] = {
    "experience.years_total": "years",
    "availability.start_date": "availability",
    "employment.current_company": "current_company",
}


def is_commitment(question: str) -> bool:
    lowered = " ".join((question or "").lower().split())
    return any(re.search(p, lowered) for p in COMMITMENT_PATTERNS)


# ─── Deterministic answers ──────────────────────────────────────────────────


def answer_deterministic(
    question: str, canonical_field: str, candidate: Candidate
) -> Answer | None:
    """Answer from the profile where a rule exists. None means "not my problem"."""
    canonical = BY_ID.get(canonical_field)
    if canonical is not None and canonical.sensitivity is Sensitivity.NEVER_FILL:
        return Answer(
            question,
            canonical_field,
            Outcome.NEVER_ANSWER,
            reason="legal declaration or demographic question — yours to answer, never the agent's",
            source="deterministic",
        )

    if is_commitment(question):
        return Answer(
            question,
            canonical_field,
            Outcome.REQUIRES_USER,
            reason="this is a commitment, not a fact; confirm it per application",
            source="deterministic",
        )

    handler = FACTUAL_HANDLERS.get(canonical_field)
    if handler == "years":
        years = candidate.total_years_experience()
        if years is None:
            return Answer(
                question,
                canonical_field,
                Outcome.REQUIRES_USER,
                reason="no dated experience in the Fact Bank; a number here would be invented",
                source="deterministic",
            )
        fact_ids = [
            str(f.id) for f in candidate.facts if f.kind is FactKind.EXPERIENCE and f.first_used_at
        ]
        return Answer(
            question,
            canonical_field,
            Outcome.ANSWERED,
            text=f"{years:g}",
            fact_ids=fact_ids,
            confidence=1.0,
            source="deterministic",
        )

    if handler == "availability":
        if candidate.availability:
            return Answer(
                question,
                canonical_field,
                Outcome.ANSWERED,
                text=candidate.availability,
                confidence=0.9,
                source="deterministic",
            )
        return Answer(
            question,
            canonical_field,
            Outcome.REQUIRES_USER,
            reason="availability is not set on your profile",
            source="deterministic",
        )

    if handler == "current_company":
        current = [
            f
            for f in candidate.facts
            if f.kind is FactKind.EXPERIENCE and f.first_used_at and f.last_used_at is None
        ]
        if current:
            return Answer(
                question,
                canonical_field,
                Outcome.ANSWERED,
                text=current[0].label,
                fact_ids=[str(current[0].id)],
                confidence=0.9,
                source="deterministic",
            )
        return Answer(
            question,
            canonical_field,
            Outcome.REQUIRES_USER,
            reason="no current role in the Fact Bank",
            source="deterministic",
        )

    return None


# ─── LLM answers ────────────────────────────────────────────────────────────

ANSWER = get_prompt("answer_question")


def _fact_lines(candidate: Candidate, job: NormalizedJob, limit: int = 28) -> list[str]:
    """Facts most relevant to this job, as `id :: kind :: text` lines.

    Ids are included so the model can cite them, which is what makes the answer's
    provenance checkable rather than asserted.
    """
    wanted = {k.lower() for k in [*job.requirements, *job.preferred_skills]}

    def relevance(fact: Any) -> int:
        text = f"{fact.label} {fact.body}".lower()
        return sum(1 for term in wanted if term and term in text)

    interesting = [
        f
        for f in candidate.facts
        if f.kind
        in {
            FactKind.EXPERIENCE,
            FactKind.PROJECT,
            FactKind.EDUCATION,
            FactKind.CERTIFICATION,
            FactKind.ACHIEVEMENT,
        }
    ]
    interesting.sort(key=relevance, reverse=True)

    lines = [
        f"{f.id} :: {f.kind.value} :: {' '.join(f.label.split())}"
        + (f" — {' '.join(f.body.split())[:220]}" if f.body else "")
        for f in interesting[:limit]
    ]
    skills = sorted({f.label for f in candidate.facts if f.kind is FactKind.SKILL})
    if skills:
        lines.append(f"skills :: skill :: {', '.join(skills)}")
    years = candidate.total_years_experience()
    lines.append(
        f"duration :: fact :: total professional experience is {years:g} years"
        if years is not None
        else "duration :: fact :: total experience NOT STATED — do not mention any duration"
    )
    return lines


def answer_with_llm(
    question: str,
    canonical_field: str,
    candidate: Candidate,
    job: NormalizedJob,
    router: Any,
) -> Answer:
    """Generate a grounded answer, then verify the grounding before returning it."""
    from ..agents.trust import harden, safe_job_text
    from ..llm.router import Tier, tokens_for

    job_context, _ = safe_job_text(" ".join((job.description or "")[:1200].split()))
    data = router.complete_json(
        ANSWER.render(
            question=question,
            title=job.title or "not stated",
            company=job.company or "not stated",
            job_context=job_context or "no description available",
            facts="\n".join(_fact_lines(candidate, job)),
        ),
        tier=Tier.STRONG,
        system=harden(ANSWER.system),
        temperature=0.3,
        # 60-120 words of answer, plus the citation list.
        max_tokens=tokens_for(400),
        agent="application_question",
    )
    if not isinstance(data, dict):
        return Answer(
            question,
            canonical_field,
            Outcome.REQUIRES_USER,
            reason="the model did not return a usable answer",
        )

    text = " ".join(str(data.get("text") or "").split())[:MAX_ANSWER_CHARS]
    cited = [str(c) for c in (data.get("cite") or []) if str(c).strip()]
    confident = bool(data.get("confident", True))

    if not text:
        return Answer(
            question, canonical_field, Outcome.REQUIRES_USER, reason="the model returned nothing"
        )
    if not confident or not cited:
        return Answer(
            question,
            canonical_field,
            Outcome.REQUIRES_USER,
            text=text,
            reason="the model could not ground this in your facts",
        )

    verdict = check_grounding(text, cited, candidate)
    if verdict:
        # One chance to fix it. An answer rejected for a single unevidenced phrase is
        # usually one clause away from correct, and sending the whole thing to the user to
        # edit wastes work that was nearly right. The check still decides.
        log.info(
            "answer_rejected", field=canonical_field, reason=verdict, action="asking for a fix"
        )
        repaired = _repair_answer(question, text, verdict, candidate, job, router)
        if repaired is not None:
            fixed_text, fixed_cites = repaired
            if not check_grounding(fixed_text, fixed_cites, candidate):
                log.info("answer_repaired", field=canonical_field)
                return Answer(
                    question,
                    canonical_field,
                    Outcome.ANSWERED,
                    text=fixed_text,
                    fact_ids=fixed_cites,
                    # Lower than a first-time pass. The agent needed telling, and that is
                    # worth carrying into anything that weighs how much to trust this.
                    confidence=0.65,
                    source="generated",
                )
            text, cited = fixed_text, fixed_cites

        log.warning("answer_not_repairable", field=canonical_field, reason=verdict)
        return Answer(
            question,
            canonical_field,
            Outcome.REQUIRES_USER,
            text=text,
            fact_ids=cited,
            reason=verdict,
        )

    return Answer(
        question,
        canonical_field,
        Outcome.ANSWERED,
        text=text,
        fact_ids=cited,
        confidence=0.75,
        source="generated",
    )


def _repair_answer(
    question: str,
    rejected: str,
    verdict: str,
    candidate: Candidate,
    job: NormalizedJob,
    router: Any,
) -> tuple[str, list[str]] | None:
    """Ask once for a corrected answer.

    The rules it is asked to follow live in `agents/repair.py` rather than here: "remove it,
    do not argue" is safety-critical, three agents need it, and three copies would be three
    chances for one of them to soften.
    """
    from ..agents import repair
    from ..agents.trust import harden

    data = repair.revise(
        agent="application_question",
        system=harden(ANSWER.system),
        complaint=verdict,
        original=rejected,
        facts="\n".join(_fact_lines(candidate, job)),
        task="answer",
        extra=f"THE QUESTION WAS: {question}",
        output_shape='{"text": "the corrected answer", "cite": ["fact-id"], '
        '"confident": true|false}',
        router=router,
        visible_tokens=400,
    )
    if data is None:
        return None
    return repair.cited_text(data, "text", MAX_ANSWER_CHARS)


def check_grounding(text: str, cited: list[str], candidate: Candidate) -> str:
    """Is this answer supported? Returns a reason it is not, or "".

    The same test the resume validator applies, for the same reason: an answer is
    a claim, and a claim needs a fact. Checks that cited ids exist and that no
    unevidenced technology appears in the prose.
    """
    known = {str(f.id) for f in candidate.facts} | {"skills", "duration"}
    unknown = [c for c in cited if c not in known]
    if unknown:
        return f"cites facts that do not exist: {', '.join(unknown[:3])}"

    introduced = _technology_tokens(text) - _supported_terms(candidate)
    if introduced:
        return f"mentions {', '.join(sorted(introduced)[:4])} with no evidence"

    # Same decimal handling as the resume validator: "0.5 years" is not "5 years".
    if match := re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\+?\s*years?\b", text, re.IGNORECASE):
        claimed = float(match.group(1))
        actual = candidate.total_years_experience()
        if actual is None:
            return f"claims {claimed:g} years; no dated experience in the Fact Bank"
        if claimed > actual + 0.5:
            return f"claims {claimed:g} years; facts support {actual:g}"

    return ""


# ─── Entry point ────────────────────────────────────────────────────────────


def answer(
    question: str,
    canonical_field: str,
    candidate: Candidate,
    job: NormalizedJob,
    *,
    router: Any = None,
    remembered: str | None = None,
    use_llm: bool = True,
) -> Answer:
    """Answer one application question.

    Order: never-answer and commitments first (no model call can override them),
    then deterministic rules, then memory, then the LLM. Anything unresolved
    returns REQUIRES_USER, which the approval queue surfaces.
    """
    # A deterministic verdict wins outright, except when it is only REQUIRES_USER on a
    # freeform question — that is the case the model is allowed to attempt.
    deterministic = answer_deterministic(question, canonical_field, candidate)
    if deterministic and (
        deterministic.outcome is not Outcome.REQUIRES_USER or not is_freeform(canonical_field)
    ):
        return deterministic

    # Reused, but re-checked: a remembered answer whose grounding no longer holds
    # (the Fact Bank changed) must not be silently reused.
    if (
        remembered
        and not is_commitment(question)
        and not check_grounding(remembered, [], candidate)
    ):
        return Answer(
            question,
            canonical_field,
            Outcome.ANSWERED,
            text=remembered,
            confidence=0.8,
            source="memory",
        )

    if not use_llm:
        return Answer(
            question,
            canonical_field,
            Outcome.REQUIRES_USER,
            reason="open question; needs an evidence-grounded answer",
        )

    if router is None:
        from ..llm.router import get_router

        router = get_router()
    if not router.available():
        return Answer(
            question,
            canonical_field,
            Outcome.REQUIRES_USER,
            reason="no LLM configured; answer this one yourself",
        )

    return answer_with_llm(question, canonical_field, candidate, job, router)


def answer_all(
    questions: list[tuple[str, str]],
    candidate: Candidate,
    job: NormalizedJob,
    *,
    router: Any = None,
    memory: dict[str, str] | None = None,
    use_llm: bool = True,
) -> list[Answer]:
    """Answer a form's open questions. `questions` is [(label, canonical_field)]."""
    memory = memory or {}
    out: list[Answer] = []
    for label, canonical_field in questions:
        out.append(
            answer(
                label,
                canonical_field,
                candidate,
                job,
                router=router,
                remembered=memory.get(normalize_question(label)),
                use_llm=use_llm,
            )
        )
    return out
