"""Tools that read the candidate, and the one that checks proposed text against them.

The asymmetry here is the important part. An agent can **read** the Fact Bank in any
detail it likes, and it can **ask whether text is supported** by it. There is no tool that
writes to it.

That is not caution for its own sake. Every anti-fabrication check in this system works by
comparing a claim against the Fact Bank, so an agent that could add a fact could add
"7 years of Kubernetes" and every validator downstream would then agree the resume is
truthful. The evidence has to be something the agent cannot reach, or it stops being
evidence. Facts change in exactly one way: a person uploads a resume, or edits their
profile.

`check_claims_supported` is the tool that makes the hybrid pattern work. An agent proposes
text, calls this, and gets told which specific claims have no backing — so it can fix its
own output before a pure validator vetoes it outright. The validator still runs afterwards
and still has the final say; this just moves most of the correction earlier, where it is
cheap.
"""

from __future__ import annotations

from typing import Any

from ..logging import get_logger
from .base import ToolError, ToolFailure, tool

log = get_logger("tools.candidate")


@tool(
    "read_candidate_facts",
    """Read the candidate's Fact Bank — the only permitted source of claims about them.

    Every fact carries the id you must cite when you use it, and a locator pointing at the
    line of their resume it came from. If something is not in here, the candidate does not
    get to claim it, no matter how likely it seems.

    Filter by kind to keep the list short: skill, experience, project, education,
    certification, achievement.""",
    parameters={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "Restrict to one kind of fact.",
                "enum": [
                    "skill",
                    "experience",
                    "project",
                    "education",
                    "certification",
                    "achievement",
                    "publication",
                    "language",
                ],
            },
            "matching": {
                "type": "string",
                "description": "Only facts whose text contains this, case-insensitive.",
            },
            "limit": {"type": "integer", "description": "Maximum facts to return. Default 40."},
        },
    },
    safety_note="Read-only. There is deliberately no tool that writes a fact — the Fact "
    "Bank is the evidence every validator checks against, so an agent must not be able "
    "to edit its own evidence.",
)
async def read_candidate_facts(
    kind: str | None = None, matching: str | None = None, limit: int = 40
) -> dict[str, Any]:
    from ..storage import repository as repo
    from ..storage import session_scope

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
    if candidate is None:
        raise ToolFailure(ToolError.UNAVAILABLE, "no candidate profile has been loaded")

    needle = (matching or "").strip().lower()
    out: list[dict[str, Any]] = []
    for fact in candidate.facts:
        if kind and fact.kind.value != kind:
            continue
        blob = f"{fact.label} {fact.body or ''} {' '.join(fact.aliases)}".lower()
        if needle and needle not in blob:
            continue
        out.append(
            {
                # Cite this id. A claim with no id behind it is a fabricated one.
                "id": str(fact.id),
                "kind": fact.kind.value,
                "label": fact.label,
                "body": (fact.body or "")[:400],
                # Derived from dates, never from a claim. None means the duration is
                # unknown, and unknown means say nothing about it.
                "years_used": fact.years_used,
                "evidence": fact.evidence_locator,
                "verified": fact.verified,
                "strength": fact.strength,
            }
        )
        if len(out) >= max(1, min(limit, 200)):
            break

    return {
        "count": len(out),
        "facts": out,
        # The single number most often got wrong, so it is stated rather than left to be
        # inferred by adding up date ranges.
        "total_years_experience": candidate.total_years_experience(),
    }


@tool(
    "check_claims_supported",
    """Check whether proposed text is supported by the candidate's facts, before it is used.

    Give it the text you intend to put on a resume, in an answer or in a letter. It returns
    each unsupported claim it found and why.

    Use this on your own output before you return it. An empty `violations` list means the
    text will survive validation; anything in it will be rejected later, and then the
    deterministic version gets used instead of yours.""",
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The proposed text — a summary, a bullet, an answer, a letter.",
            },
        },
        "required": ["text"],
    },
    safety_note="Pure check against the Fact Bank. Cannot modify anything. Advisory only "
    "— the real validator runs again on the finished document and can still veto it.",
)
async def check_claims_supported(text: str) -> dict[str, Any]:
    from ..resume.documents import Bullet, Entry, TailoredResume
    from ..resume.validate import check_evidence
    from ..storage import repository as repo
    from ..storage import session_scope

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
    if candidate is None:
        raise ToolFailure(ToolError.UNAVAILABLE, "no candidate profile has been loaded")

    # The validator works on a document, so the text is wrapped in the smallest one that
    # will carry it. Reusing the real validator matters more than the wrapping being
    # elegant: a second, simpler implementation would drift from the one that decides.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    probe = TailoredResume(
        candidate_name=candidate.name,
        # Carried as experience bullets rather than as the summary, because the summary is
        # checked by a different rule: `check_evidence` treats a summary as prose about the
        # candidate and bullets as claims about work, and a proposed bullet is what callers
        # actually hand in here.
        experience=[Entry(title="proposed", bullets=[Bullet(text=line) for line in lines])],
    )
    _integrity, violations = check_evidence(probe, candidate)

    # Two of the validator's checks are about *bookkeeping* rather than truth: a bullet
    # must carry the ids of the facts it came from, and an entry must cite at least one.
    # Loose text has no ids yet — they are attached when the real document is assembled —
    # so reporting those here would flag every input, including perfectly honest text. An
    # advisory tool that fails everything is one an agent stops reading.
    BOOKKEEPING = {"unattributed_claim", "unattributed_entry", "dangling_evidence"}
    substantive = [v for v in violations if v.kind not in BOOKKEEPING]

    return {
        "supported": not substantive,
        "violations": [
            {"kind": v.kind, "detail": v.detail, "where": v.location} for v in substantive[:12]
        ],
        "note": (
            "No unsupported claim found. Cite the fact ids you used when you return this "
            "text — attribution is checked separately on the finished document."
            if not substantive
            else "Fix these before returning the text; the validator will reject it "
            "otherwise and the deterministic version will be used instead."
        ),
    }


@tool(
    "recall_answer_to_question",
    """Look up how this candidate answered a similar application question before.

    Questions repeat constantly across applications — notice period, sponsorship, expected
    salary, why this role. Reusing a previously approved answer is better than generating a
    new one: it is consistent with what employers have already been told.

    Returns nothing when there is no match, or when the stored answer has been flagged as
    needing a fresh look because circumstances may have changed.""",
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question as the form asks it."},
        },
        "required": ["question"],
    },
    safety_note="Read-only. Answers flagged `requires_recheck` are withheld rather than "
    "reused, because a stale notice period or salary is a commitment made in the "
    "candidate's name.",
)
async def recall_answer_to_question(question: str) -> dict[str, Any]:
    from ..applications.answers import normalize_question
    from ..storage import repository as repo
    from ..storage import session_scope

    normalized = normalize_question(question)
    async with session_scope() as session:
        candidate_row = await repo.get_or_create_candidate(session)
        remembered = await repo.question_memory(session, candidate_row.id)

    if answer := remembered.get(normalized):
        return {"found": True, "question": question, "answer": answer}
    return {
        "found": False,
        "question": question,
        "note": "no previously approved answer; either it was never asked or it needs "
        "rechecking before reuse",
    }


@tool(
    "past_applications_to_company",
    """What has already happened with this company.

    Returns previous applications, their current state, and their outcome. Check this
    before applying: a second application to a role that was rejected last month reads
    badly, and an application already in flight must not be duplicated.""",
    parameters={
        "type": "object",
        "properties": {
            "company": {"type": "string", "description": "Company name as written."},
        },
        "required": ["company"],
    },
    safety_note="Read-only over our own application history.",
)
async def past_applications_to_company(company: str) -> dict[str, Any]:
    from sqlalchemy import select

    from ..storage import repository as repo
    from ..storage import session_scope
    from ..storage.models import Application, Job

    async with session_scope() as session:
        candidate_row = await repo.get_or_create_candidate(session)
        rows = (
            await session.execute(
                select(Application, Job)
                .join(Job, Job.id == Application.job_id)
                .where(
                    Application.candidate_id == candidate_row.id,
                    Job.company.ilike(f"%{company.strip()}%"),
                )
                .order_by(Application.created_at.desc())
                .limit(25)
            )
        ).all()

        applications = [
            {
                "application_id": app.id,
                "title": job.title,
                "status": str(app.status),
                "applied_at": app.submitted_at.isoformat() if app.submitted_at else None,
                "notes": (app.notes or "")[:200] or None,
                "last_error": (app.last_error or "")[:200] or None,
            }
            for app, job in rows
        ]

    active = {"applying", "awaiting_approval", "ready", "applied", "interviewing"}
    return {
        "company": company,
        "count": len(applications),
        "has_application_in_flight": any(a["status"] in active for a in applications),
        "applications": applications,
    }
