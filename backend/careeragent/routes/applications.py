"""Application endpoints: the queue, the board, and the approval flow.

The endpoint split *is* the safety model, so it is worth reading as one:

    POST /applications              shortlist — creates a record, touches nothing external
    POST /{id}/prepare              tailors, opens the form, fills, stops
    GET  /{id}/preview              what was filled, what was left, why
    POST /{id}/approve              the only endpoint that can submit
    POST /{id}/takeover             opens a visible browser and gets out of the way
    POST /{id}/status               manual outcome: interview, offer, rejection

There is no endpoint that discovers, tailors and submits in one call, and that
absence is deliberate. Every irreversible action needs its own request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..applications import service, tracker
from ..applications.service import ApplicationError
from ..applications.sessions import get_registry
from ..config import get_settings
from ..logging import get_logger
from ..storage import repository as repo
from ..storage import session_scope

log = get_logger("routes.applications")

router = APIRouter(prefix="/api/v1/applications", tags=["applications"])


class CreateRequest(BaseModel):
    job_id: str
    strategy: str = "ats_direct"


class PrepareRequest(BaseModel):
    use_llm: bool = True
    write_cover_letter: bool = False
    headless: bool = True


class ApproveRequest(BaseModel):
    """Corrections the user made while reviewing.

    Keyed by canonical field id or by the form's own label. A NEVER_FILL field is
    unreachable through here — `fill_form` checks sensitivity before it looks at
    overrides, so a demographic question cannot be answered by this route even if a
    value is supplied for it.
    """

    field_overrides: dict[str, str] = Field(default_factory=dict)
    confirm: bool = Field(
        default=False, description="Must be true. Present so an accidental POST does not submit."
    )


class StatusRequest(BaseModel):
    status: str
    note: str = ""


async def _candidate():
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        row = await repo.get_or_create_candidate(session)
        return candidate, row.id


# ─── Reading ────────────────────────────────────────────────────────────────


@router.get("")
async def board() -> dict[str, Any]:
    """The Kanban board: Discovered → Shortlisted → Ready → Applied → Interview → Offer."""
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        return await tracker.board(session, row.id)


@router.get("/queue")
async def queue() -> list[dict[str, Any]]:
    """Everything waiting on you, best score first."""
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        return await tracker.queue(session, row.id)


@router.get("/summary")
async def summary() -> dict[str, Any]:
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        return await tracker.summary(session, row.id)


@router.get("/follow-ups")
async def follow_ups() -> list[dict[str, Any]]:
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        return await tracker.follow_ups(session, row.id)


@router.get("/sessions")
async def live_sessions() -> dict[str, Any]:
    """Browser sessions currently held open for approval.

    Surfaced because they are real resources: a held session is a running Chromium,
    and the user should be able to see and end them.
    """
    return {
        "sessions": get_registry().describe(),
        "note": (
            "A held session is the form you reviewed. If it expires, approving "
            "re-opens the form and says so in the event log."
        ),
    }


@router.get("/{application_id}")
async def detail(application_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        result = await tracker.timeline(session, application_id)
        if not result:
            raise HTTPException(404, "application not found")
        return result


@router.get("/{application_id}/preview")
async def preview(application_id: str) -> dict[str, Any]:
    """What the agent filled, what it refused to fill, and what is holding it.

    The approval screen. `never_filled` is as important as `filled`: it is the list
    of things the agent decided were not its to answer.
    """
    async with session_scope() as session:
        application = await repo.get_application(session, application_id)
        if application is None:
            raise HTTPException(404, "application not found")
        answers = await repo.application_answers(session, application_id)
        live = await get_registry().get(application_id)
        return {
            "application_id": application_id,
            "status": application.status,
            "ats": application.ats,
            "url": application.application_url,
            "resume_render_id": application.resume_render_id,
            "cover_letter_id": application.cover_letter_id,
            "session_live": live is not None,
            **(application.preview or {}),
            "answers": [
                {
                    "field": a.canonical_field,
                    "question": a.question_text,
                    "answer": a.answer,
                    "confidence": a.confidence,
                    "source": a.source,
                }
                for a in answers
            ],
        }


@router.get("/{application_id}/screenshot/{index}")
async def screenshot(application_id: str, index: int = 0) -> FileResponse:
    """One captured frame of the form. The live browser view's still image.

    Screenshots are the evidence the user reviews, and the only way to debug an
    adapter that broke on a page nobody can see any more.
    """
    async with session_scope() as session:
        application = await repo.get_application(session, application_id)
        if application is None:
            raise HTTPException(404, "application not found")
        shots = (application.preview or {}).get("screenshots") or []
        if not shots or index >= len(shots):
            raise HTTPException(404, "no screenshot at that index")
        path = Path(shots[index])
        if not path.exists():
            raise HTTPException(410, "the screenshot is no longer on disk")
        return FileResponse(path, media_type="image/png")


# ─── Acting ─────────────────────────────────────────────────────────────────


@router.post("", status_code=201)
async def create(body: CreateRequest) -> dict[str, Any]:
    """Shortlist a job. Idempotent, and guaranteed not to create a duplicate."""
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        try:
            application = await service.shortlist(
                session, body.job_id, row.id, strategy=body.strategy
            )
        except ApplicationError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "application_id": application.id,
            "status": application.status,
            "strategy": application.strategy,
            "url": application.application_url,
        }


@router.post("/{application_id}/prepare")
async def prepare(application_id: str, body: PrepareRequest) -> dict[str, Any]:
    """Tailor the resume, open the form, fill what can be proved, and stop.

    Runs inline rather than in the background: it takes 20-60 seconds and the caller
    needs the result to render an approval screen. A fire-and-forget prepare would
    mean polling for a form the user is waiting to look at.
    """
    candidate, _ = await _candidate()
    if candidate is None:
        raise HTTPException(409, "Upload your resume first")

    async with session_scope() as session:
        try:
            result = await service.prepare(
                session,
                application_id,
                candidate,
                use_llm=body.use_llm,
                headless=body.headless,
                write_cover_letter=body.write_cover_letter,
            )
        except ApplicationError as exc:
            raise HTTPException(409, str(exc)) from exc
        return result.to_dict()


@router.post("/{application_id}/approve")
async def approve(application_id: str, body: ApproveRequest) -> dict[str, Any]:
    """Submit an application you have reviewed.

    `confirm` must be true. The pre-submit verdict still runs and can still stop the
    submission — an approval authorises submitting a form that passes, not one that
    does not.
    """
    if not body.confirm:
        raise HTTPException(400, "set confirm=true to submit this application")

    candidate, _ = await _candidate()
    if candidate is None:
        raise HTTPException(409, "Upload your resume first")

    async with session_scope() as session:
        try:
            result = await service.approve(
                session, application_id, candidate, field_overrides=body.field_overrides
            )
        except ApplicationError as exc:
            raise HTTPException(409, str(exc)) from exc
        return result.to_dict()


@router.post("/{application_id}/reject")
async def reject(application_id: str, reason: str = Query("")) -> dict[str, Any]:
    async with session_scope() as session:
        try:
            application = await service.reject(session, application_id, reason)
        except ApplicationError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"application_id": application_id, "status": application.status}


@router.post("/{application_id}/takeover")
async def takeover(application_id: str) -> dict[str, Any]:
    """Open a visible browser on the form and hand it over.

    For CAPTCHA, MFA, a login wall, or a question only you can answer. Requires a
    desktop session — the browser window opens on the machine running the API, which
    is the honest constraint of handing someone a real browser.
    """
    candidate, _ = await _candidate()
    if candidate is None:
        raise HTTPException(409, "Upload your resume first")

    async with session_scope() as session:
        try:
            result = await service.takeover(session, application_id, candidate)
        except ApplicationError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            **result.to_dict(),
            "note": (
                "A browser window is open on the form. Finish it yourself, then POST "
                "/status with 'applied' so the tracker knows."
            ),
        }


@router.post("/{application_id}/status")
async def set_status(application_id: str, body: StatusRequest) -> dict[str, Any]:
    """Record an outcome, or confirm a submission the agent could not verify.

    The state machine validates the move, so an illegal transition is a 409 with the
    legal options rather than a silently coerced status.
    """
    async with session_scope() as session:
        try:
            application = await service.set_status(
                session, application_id, body.status, note=body.note
            )
        except ApplicationError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"application_id": application_id, "status": application.status}


@router.post("/{application_id}/end-session")
async def end_session(application_id: str) -> dict[str, Any]:
    """Close the held browser session without approving or rejecting."""
    await get_registry().drop(application_id)
    return {"application_id": application_id, "session_live": False}


class BatchRequest(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=25)
    use_llm: bool = True
    limit: int = Field(default=5, ge=1, le=15)


@router.post("/batch/prepare")
async def batch_prepare(body: BatchRequest) -> dict[str, Any]:
    """Shortlist and prepare several jobs, sequentially, respecting the daily cap.

    Prepares only. Nothing in this response has been submitted, and each one still
    needs its own approval — which is what stops "apply to the top 10" from meaning
    ten unreviewed applications.
    """
    candidate, candidate_id = await _candidate()
    if candidate is None:
        raise HTTPException(409, "Upload your resume first")

    settings = get_settings()
    async with session_scope() as session:
        results = await service.prepare_batch(
            session,
            body.job_ids,
            candidate,
            candidate_id,
            use_llm=body.use_llm,
            limit=body.limit,
        )
        return {
            "prepared": [r.to_dict() for r in results],
            "awaiting_approval": sum(1 for r in results if r.status == "awaiting_approval"),
            "needs_you": sum(1 for r in results if r.status == "human_required"),
            "daily_cap": settings.daily_application_cap,
            "note": "Nothing here has been submitted. Each one needs its own approval.",
        }
