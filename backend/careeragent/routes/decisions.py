"""Reading the decision trail — spec §50, §77.

The rest of the API answers "what is the state of my applications?". This answers the
question that comes after a surprise:

    Why did it do that?

Three shapes, because there are three ways that question gets asked:

  * ``/decisions`` — the last N decisions, newest first. "What has it been doing?"
  * ``/decisions/{subject_type}/{subject_id}`` — every decision about one job or
    application, oldest first, so it reads as a narrative.
  * ``/decisions/summary`` — per-agent volume and average confidence. An agent making
    hundreds of decisions at 40% confidence is doing a lot of guessing, and that is
    worth being able to see without reading rows.

All three are reads. Nothing here can change a decision or re-run one: the trail is
append-only, and an endpoint that could edit history would make it worthless as an
explanation.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..agents import audit
from ..storage import session_scope

router = APIRouter(prefix="/api/v1/decisions", tags=["decisions"])

# The route order matters: `/summary` is declared before `/{subject_type}/{subject_id}`
# is even reachable, but `/agents` would otherwise be a plausible `subject_type`, so the
# fixed paths come first and the parameterised one is last in the file.


@router.get("")
async def recent(
    limit: int = Query(100, ge=1, le=500),
    agent: str | None = Query(None, description="Filter to one agent, e.g. 'recovery'"),
) -> dict[str, Any]:
    """The most recent decisions, newest first."""
    async with session_scope() as session:
        rows = await audit.recent(session, limit=limit, agent=agent)
    return {"count": len(rows), "agent": agent, "decisions": rows}


@router.get("/summary")
async def summary() -> dict[str, Any]:
    """How many decisions each agent has made, and how sure it tends to be."""
    async with session_scope() as session:
        return await audit.summarize(session)


@router.get("/prompts")
async def prompt_registry() -> dict[str, Any]:
    """Every prompt and its current version.

    Lives here rather than under settings because its only real use is reading a
    decision's `prompt_version` and finding out what those instructions actually said.
    `grounded` is the interesting column: a prompt that touches candidate or company
    facts and is not grounded would be a hole in the anti-fabrication guarantee.
    """
    from ..prompts import all_prompts

    entries = [p.to_dict() for p in all_prompts()]
    return {
        "count": len(entries),
        "ungrounded": [p["name"] for p in entries if not p["grounded"]],
        "prompts": entries,
    }


@router.get("/tools")
async def tool_layer() -> dict[str, Any]:
    """What the agents are actually able to do.

    The most useful field is `by_effect.external`, and the useful thing about it is that it
    is empty: no tool in this system reaches the outside world on its own. Submitting an
    application and sending a message are not tools — they sit behind a human click — and
    `absent_by_design` names the capabilities that were deliberately never built.
    """
    from ..tools import describe

    return describe()


@router.get("/{subject_type}/{subject_id}")
async def for_subject(subject_type: str, subject_id: str) -> dict[str, Any]:
    """Every decision made about one subject, oldest first.

    `subject_type` is `job` or `application`. An unknown subject is an empty trail, not
    a 404: "nothing was decided about this" is a true and useful answer, and the subject
    may legitimately exist with no decisions recorded yet.
    """
    async with session_scope() as session:
        rows = await audit.for_subject(session, subject_type, subject_id)
    return {
        "subject_type": subject_type,
        "subject_id": subject_id,
        "count": len(rows),
        "decisions": rows,
    }
