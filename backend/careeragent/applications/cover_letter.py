"""Cover letter generation.

A cover letter is the highest-risk document the system produces. A resume bullet
is a compressed claim; a cover letter is *prose*, and prose is where a model
invents motivation it was never given — "I have long admired your work on X" about
an X the candidate has never heard of.

So the constraints are tighter than for a resume:

  * Only facts, and only facts handed in explicitly.
  * Only company details drawn from research already in the database — never
    recalled from the model's training data, which is how a letter ends up
    praising a product the company discontinued.
  * The grounding check from `answers.py` runs over the result, and a letter that
    fails it is not returned. There is no deterministic fallback: an unfluent
    cover letter is worse than none, and "write this one yourself" is an honest
    answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..logging import get_logger
from ..models.candidate import Candidate, FactKind
from ..models.job import NormalizedJob
from ..prompts import get as get_prompt
from .answers import check_grounding

log = get_logger("applications.cover_letter")

MIN_WORDS, MAX_WORDS = 90, 320

LETTER = get_prompt("cover_letter")


def _repair_letter(
    rejected: str,
    complaint: str,
    candidate: Candidate,
    job: NormalizedJob,
    router: Any,
) -> tuple[str, list[str]] | None:
    """Ask once for a corrected letter. The rules live in `agents/repair.py`."""
    from ..agents import repair
    from ..agents.trust import harden

    data = repair.revise(
        agent="cover_letter",
        system=harden(LETTER.system),
        complaint=complaint,
        original=rejected,
        facts=_fact_lines(candidate, job),
        task="cover letter",
        extra=f"THE ROLE: {job.title or 'not stated'} at {job.company or 'not stated'}",
        output_shape='{"body": "the corrected letter, no greeting and no signature", '
        '"cite": ["fact-id"], "confident": true|false}',
        router=router,
        visible_tokens=700,
    )
    if data is None or repair.cited_text(data, "body", 4000) is None:
        return None

    # Paragraph breaks are rebuilt from the raw value rather than taken from
    # `cited_text`, which collapses all whitespace — right for a single-paragraph answer,
    # wrong for a letter with a structure.
    raw = str(data.get("body") or "")
    body = "\n\n".join(" ".join(p.split()) for p in raw.split("\n\n") if p.strip())
    cited = [str(c) for c in (data.get("cite") or []) if str(c).strip()]
    return body, cited


@dataclass
class CoverLetterDraft:
    body: str = ""
    fact_ids: list[str] = field(default_factory=list)
    ok: bool = False
    reason: str = ""
    model: str = ""
    word_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "body": self.body,
            "fact_ids": list(self.fact_ids),
            "ok": self.ok,
            "reason": self.reason,
            "model": self.model,
            "word_count": self.word_count,
            "warnings": list(self.warnings),
        }

    def with_greeting(self, company: str = "", contact_name: str = "") -> str:
        """The sendable form. Greeting is assembled here rather than generated,
        because a model that guesses a hiring manager's name will get it wrong."""
        greeting = f"Dear {contact_name}," if contact_name else "Dear Hiring Team,"
        if not contact_name and company:
            greeting = f"Dear {company} Hiring Team,"
        return f"{greeting}\n\n{self.body}"


# Phrases that signal invented enthusiasm. Checked because they are the
# characteristic failure of this document type, and cheap to detect.
FLATTERY = (
    r"\blong[- ]?(time )?admire",
    r"\bhuge fan\b",
    r"\balways wanted to work\b",
    r"\byour (mission|vision|culture) (deeply )?resonates\b",
    r"\bindustry[- ]leading\b",
    r"\bworld[- ]class\b",
    r"\bpassionate about your\b",
    r"\bI have followed (your|the) (company|work)\b",
)


def _fact_lines(candidate: Candidate, job: NormalizedJob, limit: int = 20) -> str:
    wanted = {k.lower() for k in [*job.requirements, *job.preferred_skills]}

    def relevance(fact: Any) -> int:
        text = f"{fact.label} {fact.body}".lower()
        return sum(1 for term in wanted if term and term in text)

    entries = [
        f
        for f in candidate.facts
        if f.kind
        in {FactKind.EXPERIENCE, FactKind.PROJECT, FactKind.EDUCATION, FactKind.ACHIEVEMENT}
    ]
    entries.sort(key=relevance, reverse=True)

    lines = [
        f"{f.id} :: {f.kind.value} :: {' '.join(f.label.split())}"
        + (f" — {' '.join(f.body.split())[:260]}" if f.body else "")
        for f in entries[:limit]
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
    return "\n".join(lines)


def check_flattery(text: str) -> list[str]:
    found = []
    for pattern in FLATTERY:
        if match := re.search(pattern, text, re.IGNORECASE):
            found.append(match.group(0))
    return found


def generate(
    candidate: Candidate,
    job: NormalizedJob,
    *,
    company_notes: str = "",
    router: Any = None,
) -> CoverLetterDraft:
    """Draft a cover letter, or explain why there isn't one.

    `company_notes` should come from the company research already stored — passing
    nothing is fine and produces a letter that talks about the role rather than
    inventing detail about the employer.
    """
    if router is None:
        from ..llm.router import get_router

        router = get_router()
    if not router.available():
        return CoverLetterDraft(
            reason="no LLM configured; a cover letter needs one, or write it yourself"
        )

    from ..agents.trust import harden, safe_job_text, safe_page_text
    from ..llm.router import Tier, tokens_for

    fenced_description, _ = safe_job_text(" ".join((job.description or "")[:1800].split()))
    fenced_notes, _ = safe_page_text(" ".join((company_notes or "").split())[:900])

    result = router.complete(
        LETTER.render(
            title=job.title or "not stated",
            company=job.company or "not stated",
            location=job.location or "not stated",
            requirements=", ".join([*job.requirements, *job.preferred_skills][:20]) or "not stated",
            description=fenced_description or "not available",
            company_notes=fenced_notes or "(none available)",
            facts=_fact_lines(candidate, job),
        ),
        tier=Tier.STRONG,
        system=harden(LETTER.system),
        temperature=0.4,
        # 150-250 words of letter, plus citations.
        max_tokens=tokens_for(700),
        json_mode=True,
        agent="cover_letter",
    )
    if result is None:
        return CoverLetterDraft(reason="every model in the chain failed")

    data = result.json()
    if not isinstance(data, dict):
        return CoverLetterDraft(model=result.model, reason="the model's reply was not usable JSON")

    body = "\n\n".join(
        " ".join(para.split()) for para in str(data.get("body") or "").split("\n\n") if para.strip()
    )
    cited = [str(c) for c in (data.get("cite") or []) if str(c).strip()]
    words = len(body.split())

    if not body:
        return CoverLetterDraft(model=result.model, reason="the model returned no letter")
    if not bool(data.get("confident", True)) or not cited:
        return CoverLetterDraft(
            body=body,
            model=result.model,
            word_count=words,
            reason="the model could not ground the letter in your facts",
        )

    # Both checks reject for the same kind of reason — a claim about the candidate that
    # nothing supports — and both usually land on one clause of an otherwise usable letter.
    complaint = check_grounding(body, cited, candidate)
    if not complaint and (flattery := check_flattery(body)):
        # Invented enthusiasm is not a style problem; it is a claim about the candidate's
        # history with this company that nothing supports.
        complaint = f"contains unsupported flattery: {', '.join(flattery[:2])}"

    if complaint:
        log.info("cover_letter_rejected", job_id=job.job_id, reason=complaint, action="revising")
        if fixed := _repair_letter(body, complaint, candidate, job, router):
            body, cited = fixed
            words = len(body.split())
            if not check_grounding(body, cited, candidate) and not check_flattery(body):
                log.info("cover_letter_repaired", job_id=job.job_id)
                complaint = ""

    if complaint:
        log.warning("cover_letter_not_repairable", job_id=job.job_id, reason=complaint)
        return CoverLetterDraft(
            body=body, fact_ids=cited, model=result.model, word_count=words, reason=complaint
        )

    warnings: list[str] = []
    if words < MIN_WORDS:
        warnings.append(f"{words} words; short for a cover letter")
    elif words > MAX_WORDS:
        warnings.append(f"{words} words; most readers stop before the end")

    log.info("cover_letter_generated", job_id=job.job_id, words=words, model=result.model)
    return CoverLetterDraft(
        body=body,
        fact_ids=cited,
        ok=True,
        model=result.model,
        word_count=words,
        warnings=warnings,
    )
