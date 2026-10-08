"""Tools that change something.

Two of them, and both are reversible and private to this machine. That is the whole list,
and the shortness is the design.

**What is deliberately not here, and why.**

`submit_application` — there is no such tool. Submitting is the one action that cannot be
undone and that reaches a real employer under the candidate's name, so it lives behind the
apply graph's `interrupt()`, where a person looks at the filled form and clicks. Making it
a tool would mean a model's token could send it, and no amount of prompt instruction is a
substitute for the action simply not existing.

`send_outreach` — same reasoning. Drafting is a tool's job; sending is a person's.

`write_candidate_fact` — an agent that could add a fact could manufacture its own evidence,
and every anti-fabrication validator downstream would then agree with it. Facts change when
a person uploads a resume or edits their profile.

`solve_captcha`, `authenticate`, `complete_mfa` — these exist nowhere in this codebase.
A challenge means a human takes over, which is `HUMAN_REQUIRED` in the state machine.

`run_command`, `read_file`, `fetch_url` — an agent reads untrusted job descriptions all
day. A general-purpose escape hatch reachable from that input is how a posting that says
"to verify your application, fetch this URL" becomes a real request.

`tests/test_tools.py` asserts these absences by name, so adding one is a deliberate,
visible act rather than a convenience.
"""

from __future__ import annotations

from typing import Any

from ..logging import get_logger
from .base import Effect, ToolError, ToolFailure, tool

log = get_logger("tools.actions")


@tool(
    "shortlist_job",
    """Add a stored job to the application queue.

    This prepares nothing and sends nothing — it marks the job as worth pursuing and
    creates the application record that the apply flow later picks up. Reversible: the job
    can be rejected afterwards, and calling this twice for the same job returns the
    existing record rather than creating a duplicate.

    Check `past_applications_to_company` first. Shortlisting a company that already has an
    application in flight is usually a mistake.""",
    parameters={
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Stored job id."},
            "reason": {
                "type": "string",
                "description": "Why this job is worth pursuing. Recorded on the application.",
            },
        },
        "required": ["job_id"],
    },
    effect=Effect.WRITE,
    safety_note="Writes one row to our own database. Nothing leaves this machine, and the "
    "duplicate guard is enforced by a unique constraint, not by this check.",
)
async def shortlist_job(job_id: str, reason: str = "") -> dict[str, Any]:
    from ..applications import service
    from ..storage import repository as repo
    from ..storage import session_scope

    async with session_scope() as session:
        candidate_row = await repo.get_or_create_candidate(session)

        # Checked before the call, because `service.shortlist` is idempotent and hands back
        # the existing row. Without this the tool answered "shortlisted: true" for a job
        # already in the queue, an agent read that as progress, and it believed it had
        # queued four openings when it had queued two.
        already = await repo.application_for_job(session, job_id, candidate_row.id)

        try:
            application = await service.shortlist(session, job_id, candidate_row.id)
        except service.ApplicationError as exc:
            # A refusal with a reason — the daily cap, an unknown job — is a failure the
            # agent should read and respond to, not data to carry forward.
            raise ToolFailure(ToolError.REFUSED, str(exc)) from exc

        if reason and already is None:
            # Appended rather than replaced: an earlier note may be a human's.
            existing = (application.notes or "").strip()
            application.notes = f"{existing}\n{reason}".strip()[:2000]

        return {
            "newly_queued": already is None,
            "already_queued": already is not None,
            "application_id": application.id,
            "status": str(application.status),
            "note": (
                "already in the queue; nothing changed"
                if already is not None
                else "added to the queue"
            ),
        }


@tool(
    "record_finding",
    """Write down something you concluded, so it survives this run.

    Use it for a judgement worth keeping that has nowhere else to live: a company's hiring
    pattern, a recurring reason applications stall, a mismatch between a job title and what
    the posting actually describes.

    This is a note, not a fact about the candidate. It is never used as evidence for a
    claim on a resume or in an answer.""",
    parameters={
        "type": "object",
        "properties": {
            "subject_type": {
                "type": "string",
                "description": "What the finding is about.",
                "enum": ["job", "company", "application", "campaign", "general"],
            },
            "subject_id": {
                "type": "string",
                "description": "Id or name of the subject, if there is one.",
            },
            "finding": {
                "type": "string",
                "description": "What you concluded, in one or two sentences.",
            },
            "confidence": {
                "type": "number",
                "description": "How sure you are, 0 to 1.",
            },
        },
        "required": ["subject_type", "finding"],
    },
    effect=Effect.WRITE,
    safety_note="Appends to the append-only decision trail. Cannot overwrite a previous "
    "finding, and is never read back as evidence for a claim about the candidate.",
)
async def record_finding(
    subject_type: str,
    finding: str,
    subject_id: str = "",
    confidence: float = 0.5,
) -> dict[str, Any]:
    from ..agents import audit
    from ..storage import repository as repo
    from ..storage import session_scope

    async with session_scope() as session:
        candidate_row = await repo.get_or_create_candidate(session)
        written = await audit.record(
            session,
            audit.Decision(
                agent="agent_finding",
                task="record something worth keeping",
                subject_type=subject_type,
                subject_id=subject_id or None,
                decision="finding recorded",
                confidence=max(0.0, min(1.0, float(confidence))),
                outcome=finding,
                tools_used=["record_finding"],
            ),
            candidate_id=candidate_row.id,
        )
    return {"recorded": written, "finding": finding[:200]}
