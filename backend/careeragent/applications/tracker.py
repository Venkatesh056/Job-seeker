"""Application tracking views.

Read-only projections over `application`: the Kanban board, the follow-up list,
and the timeline. No state changes here — those all go through `service.set_status`
so that every transition is validated by the state machine and recorded as an event.

The one judgement call in this module: `SUBMISSION_UNVERIFIED` is surfaced as its
own attention item rather than shown as applied. It is the single most useful
honest signal the tracker produces — "we clicked submit and saw nothing, go and
check" — and collapsing it into APPLIED would hide both a possible failure and a
possible duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..storage import repository as repo
from ..storage.models import Application, Job
from ..storage.models import ApplicationStatus as S
from .states import BOARD_COLUMNS, NEEDS_HUMAN, RESPONDED_STATES, SUBMITTED_STATES, board_column


@dataclass
class Card:
    application_id: str
    job_id: str
    title: str
    company: str
    location: str
    status: str
    column: str
    ats: str | None = None
    final_score: float | None = None
    submitted_at: str | None = None
    follow_up_date: str | None = None
    updated_at: str | None = None
    url: str | None = None
    needs_attention: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


ATTENTION_REASON = {
    S.AWAITING_APPROVAL: "waiting for your approval",
    S.HUMAN_REQUIRED: "needs you to finish it",
    S.SUBMISSION_UNVERIFIED: "submitted but unconfirmed — please verify",
    S.FOLLOW_UP_DUE: "follow-up due",
    S.FAILED: "failed; can be retried",
}


def _card(application: Application, job: Job) -> Card:
    status = S(application.status)
    return Card(
        application_id=application.id,
        job_id=application.job_id,
        title=job.title,
        company=job.company,
        location=job.location or "",
        status=status.value,
        column=board_column(status),
        ats=application.ats,
        final_score=application.final_score,
        submitted_at=application.submitted_at.isoformat() if application.submitted_at else None,
        follow_up_date=(
            application.follow_up_date.isoformat() if application.follow_up_date else None
        ),
        updated_at=application.updated_at.isoformat() if application.updated_at else None,
        url=application.application_url,
        needs_attention=ATTENTION_REASON.get(status, ""),
    )


async def board(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    """The Kanban view: ordered columns, each with its cards."""
    rows = await repo.list_applications(session, candidate_id, limit=500)
    cards = [_card(app, job) for app, job in rows]

    columns = []
    for name, _members in BOARD_COLUMNS:
        in_column = [c.to_dict() for c in cards if c.column == name]
        columns.append({"name": name, "count": len(in_column), "cards": in_column})

    return {
        "columns": columns,
        "total": len(cards),
        "needs_attention": [c.to_dict() for c in cards if c.needs_attention],
    }


async def queue(session: AsyncSession, candidate_id: str) -> list[dict[str, Any]]:
    """Everything blocked on a human, highest score first.

    This is the screen the brief's §27 approval queue describes, and the ordering
    matters: a user with 14 applications waiting should see the best one first.
    """
    rows = await repo.list_applications(
        session, candidate_id, statuses=[s.value for s in NEEDS_HUMAN], limit=200
    )
    cards = [_card(app, job) for app, job in rows]
    cards.sort(key=lambda c: c.final_score or 0.0, reverse=True)
    return [c.to_dict() for c in cards]


async def follow_ups(session: AsyncSession, candidate_id: str) -> list[dict[str, Any]]:
    """Applications whose follow-up date has arrived."""
    rows = await repo.list_applications(session, candidate_id, limit=500)
    today = date.today()
    out = []
    for application, job in rows:
        if (
            application.follow_up_date
            and application.follow_up_date <= today
            and S(application.status) in SUBMITTED_STATES
            and S(application.status) not in RESPONDED_STATES
        ):
            card = _card(application, job)
            card.needs_attention = (
                f"applied {(today - application.follow_up_date).days + 10} days ago; no response"
            )
            out.append(card.to_dict())
    return out


async def timeline(session: AsyncSession, application_id: str) -> dict[str, Any]:
    """Everything that happened to one application, in order.

    The audit trail a user reads when they want to know what the agent actually
    did — which is a different question from what state the application is in now.
    """
    application = await repo.get_application(session, application_id)
    if application is None:
        return {}
    job = await session.get(Job, application.job_id)
    events = await repo.application_events(session, application_id)
    answers = await repo.application_answers(session, application_id)

    return {
        "application_id": application_id,
        "job": {
            "id": application.job_id,
            "title": job.title if job else "",
            "company": job.company if job else "",
            "location": job.location if job else "",
        },
        "status": application.status,
        "strategy": application.strategy,
        "ats": application.ats,
        "url": application.application_url,
        "scores": {
            "match": application.match_score,
            "company": application.company_score,
            "final": application.final_score,
        },
        "resume_render_id": application.resume_render_id,
        "cover_letter_id": application.cover_letter_id,
        "submitted_at": application.submitted_at.isoformat() if application.submitted_at else None,
        "evidence": application.evidence or {},
        "preview": application.preview or {},
        "attempts": application.attempts,
        "last_error": application.last_error,
        "notes": application.notes,
        "events": [{"type": e.type, "at": e.at.isoformat(), "payload": e.payload} for e in events],
        "answers": [
            {
                "field": a.canonical_field,
                "question": a.question_text,
                "answer": a.answer,
                "confidence": a.confidence,
                "source": a.source,
                "fact_ids": a.fact_ids,
            }
            for a in answers
        ],
    }


async def mark_follow_ups_due(session: AsyncSession, candidate_id: str) -> int:
    """Move applications past their follow-up date into FOLLOW_UP_DUE.

    A scheduled housekeeping step, separated from the read views so that looking at
    the board never mutates it.
    """
    from .service import set_status

    rows = await repo.list_applications(
        session, candidate_id, statuses=[S.APPLIED.value], limit=500
    )
    today = date.today()
    moved = 0
    for application, _job in rows:
        if application.follow_up_date and application.follow_up_date <= today:
            await set_status(
                session,
                application.id,
                S.FOLLOW_UP_DUE.value,
                note=f"no response by {today.isoformat()}",
            )
            moved += 1
    return moved


async def summary(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    """The overview cards from the brief's §27."""
    rows = await repo.list_applications(session, candidate_id, limit=1000)
    now = datetime.now(UTC)

    by_status: dict[str, int] = {}
    submitted = responded = interviews = offers = unverified = 0
    today = week = 0

    for application, _job in rows:
        status = S(application.status)
        by_status[status.value] = by_status.get(status.value, 0) + 1
        if status in SUBMITTED_STATES:
            submitted += 1
        if status in RESPONDED_STATES:
            responded += 1
        if status is S.INTERVIEW:
            interviews += 1
        if status is S.OFFER:
            offers += 1
        if status is S.SUBMISSION_UNVERIFIED:
            unverified += 1
        if application.submitted_at:
            age = (now - application.submitted_at).total_seconds()
            if age <= 86_400:
                today += 1
            if age <= 7 * 86_400:
                week += 1

    return {
        "total": len(rows),
        "by_status": by_status,
        "submitted": submitted,
        "submitted_today": today,
        "submitted_this_week": week,
        "responses": responded,
        "interviews": interviews,
        "offers": offers,
        "submission_unverified": unverified,
        "awaiting_approval": by_status.get(S.AWAITING_APPROVAL.value, 0),
        "needs_human": sum(by_status.get(s.value, 0) for s in NEEDS_HUMAN),
    }
