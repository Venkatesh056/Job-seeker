"""The supervisor endpoint: one goal, in a sentence — spec §5.

Everything else in this API asks for a specific operation. This asks for an outcome, and
lets the supervisor work out the operations. `POST /supervisor/run` with *"find data
science internships in Bangalore and get three applications ready"* is the whole request.

Two things the endpoint is careful about, both because of what this costs:

  * **`max_steps` is capped here, not only in the graph.** A request asking for fifty steps
    is a request to spend the day's LLM budget, and the ceiling belongs at the edge where
    the request arrives.
  * **`dry_run` is offered prominently.** It skips the two steps that cost something real —
    discovery spends API quota, preparation opens a browser — so the routing can be watched
    for the price of the model calls alone. It is the right default for "what would you do?".

Nothing here submits an application. The supervisor's furthest reach is a filled form
parked at `awaiting_approval`, and the response says so explicitly rather than leaving the
caller to infer it.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..logging import get_logger
from ..storage import repository as repo
from ..storage import session_scope

log = get_logger("routes.supervisor")

router = APIRouter(prefix="/api/v1/supervisor", tags=["supervisor"])

# Each step is at least one model call plus whatever the step itself costs. Twelve is
# already an expensive request; fifty would be a bill.
MAX_STEPS_CEILING = 12


class GoalRequest(BaseModel):
    goal: str = Field(
        min_length=8,
        max_length=500,
        description="What you want, in a sentence.",
        examples=[
            "Find data science internships in Bangalore and get three applications ready",
            "Look at what is already queued and prepare the two strongest",
        ],
    )
    max_steps: int = Field(default=6, ge=1, le=MAX_STEPS_CEILING)
    dry_run: bool = Field(
        default=False,
        description="Skip discovery and form filling. Shows the plan without spending API "
        "quota or opening a browser.",
    )
    autonomy_level: int | None = Field(
        default=None,
        ge=0,
        le=5,
        description="Overrides the configured level for this run. Below 5, preparation "
        "always stops at the approval.",
    )


@router.post("/run")
async def run(body: GoalRequest) -> dict[str, Any]:
    """Pursue one goal, deciding each step from what the last one found.

    Runs inline. A supervisor run is a handful of model calls and the caller wants the
    result; progress shows up in the normal event feed while this is in flight.
    """
    async with session_scope() as session:
        if await repo.load_candidate(session) is None:
            raise HTTPException(409, "Upload your resume first")

    from ..graph.supervisor import run_supervisor

    run_id = str(uuid.uuid4())
    try:
        state = await run_supervisor(
            body.goal,
            max_steps=body.max_steps,
            autonomy_level=body.autonomy_level,
            dry_run=body.dry_run,
            run_id=run_id,
        )
    except Exception as exc:
        log.exception("supervisor_failed", run_id=run_id)
        raise HTTPException(500, f"supervisor failed: {type(exc).__name__}") from exc

    parked = state.get("awaiting_approval") or []
    return {
        "run_id": run_id,
        "goal": body.goal,
        "dry_run": body.dry_run,
        "summary": state.get("summary", ""),
        "stopped_because": state.get("stopped_because", ""),
        "steps_taken": state.get("steps_taken", 0),
        # The path it chose, which is the interesting part — it was not written down
        # anywhere in advance.
        "plan": state.get("plan") or [],
        "history": state.get("history") or [],
        "shortlisted": state.get("shortlisted") or [],
        "prepared": state.get("prepared") or [],
        "awaiting_approval": parked,
        # Stated rather than implied. A caller reading `prepared` should not have to work
        # out whether anything was sent.
        "submitted": 0,
        "note": (
            f"{len(parked)} application(s) are filled and waiting for you to review. "
            "Nothing was submitted — approve them to send."
            if parked
            else "Nothing was submitted. The supervisor cannot submit an application."
        ),
        "error": state.get("error") or None,
    }


@router.get("/steps")
async def steps() -> dict[str, Any]:
    """What the supervisor is able to choose between, and what it cannot do.

    Worth exposing: the absence of a submit step is a property of the system, and a caller
    should be able to check it rather than take it on trust.
    """
    from ..graph.supervisor import EXPLAIN, MAX_STEPS, STEPS, STUCK_AFTER
    from ..tools import toolset

    return {
        "steps": list(STEPS),
        "default_max_steps": MAX_STEPS,
        "max_steps_ceiling": MAX_STEPS_CEILING,
        "stuck_after": STUCK_AFTER,
        "stop_reasons": dict(EXPLAIN),
        "tools": [t.name for t in toolset("supervisor")],
        "cannot": [
            "submit an application",
            "send a message",
            "write to the Fact Bank",
            "bypass a CAPTCHA, a login or an MFA prompt",
        ],
    }
