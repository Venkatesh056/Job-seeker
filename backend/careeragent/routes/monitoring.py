"""Career monitoring over HTTP — addendum §13.

    GET  /monitor          what it is watching and when each board is next due
    POST /monitor/sweep    read whatever is due now
    GET  /monitor/new      the openings the last sweeps found, best first

The sweep endpoint is a POST because it reaches out to other people's servers, and that is
not something a GET should do — a link preview or a browser prefetch should never be able to
trigger a crawl.

Nothing here applies to anything. The monitor finds and scores; applying stays behind your
click, in the application endpoints.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..logging import get_logger
from ..monitoring import status as monitor_status
from ..monitoring import sweep
from ..storage import repository as repo
from ..storage import session_scope

log = get_logger("routes.monitoring")

router = APIRouter(prefix="/api/v1/monitor", tags=["monitoring"])


@router.get("")
async def state() -> dict[str, Any]:
    """What the monitor is watching, and when each board is next due."""
    async with session_scope() as session:
        return await monitor_status(session)


@router.post("/sweep")
async def run_sweep(
    limit: int = Query(0, ge=0, le=200, description="Boards to read. 0 uses the configured size."),
    dry_run: bool = Query(False, description="Report what would be found without storing it."),
) -> dict[str, Any]:
    """Read every board that is due now.

    Runs inline: a sweep of the default size takes a few seconds and the caller wants the
    result. Progress appears on the normal event feed while this is in flight.
    """
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(409, "Upload your resume first")
        row = await repo.get_or_create_candidate(session)
        report = await sweep(session, candidate, row.id, limit=limit or None, dry_run=dry_run)

    return {"summary": report.summary(), **report.to_dict()}


@router.get("/new")
async def recent_finds(
    since_hours: int = Query(48, ge=1, le=720),
    limit: int = Query(25, ge=1, le=100),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
) -> dict[str, Any]:
    """Openings first seen recently, best match first.

    Reads the job table by creation time rather than keeping a separate list of "things the
    monitor found" — the monitor is not a second source of truth, it is the thing that keeps
    the first one current.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from ..storage.models import Job, JobScore

    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        rows = (
            await session.execute(
                select(Job, JobScore.final_score, JobScore.priority)
                .join(JobScore, (JobScore.job_id == Job.id) & (JobScore.candidate_id == row.id))
                .where(Job.created_at >= cutoff, JobScore.final_score >= min_score)
                .order_by(JobScore.final_score.desc())
                .limit(limit)
            )
        ).all()

        found = [
            {
                "job_id": job.id,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "ats": job.ats,
                "posted": str(job.posted_date) if job.posted_date else None,
                "first_seen": job.created_at.isoformat(),
                "score": round(float(score or 0.0), 4),
                "priority": priority,
            }
            for job, score, priority in rows
        ]

    return {"since_hours": since_hours, "count": len(found), "jobs": found}


# ─── Filling in what discovery left out ─────────────────────────────────────


@router.post("/describe")
async def describe_missing(
    limit: int = Query(40, ge=1, le=200),
    source: str = Query("", description="Only jobs from this source, e.g. 'apollo'."),
) -> dict[str, Any]:
    """Read the job pages of postings discovered without a description, and rescore them.

    Some sources give a title, a location and a date but no body text — Apollo's feed is
    the main one, and it is the source that reaches companies whose careers page cannot be
    read at all. Skill matching needs the text, so without this those postings score as
    though they match none of the candidate's skills, which is not the same thing as
    nobody having read them.

    A POST because it reaches out to other people's servers. Bounded, and it only fetches
    postings that could plausibly survive scoring — fresh, right country, not senior, not
    already applied to.
    """
    from ..pipeline.describe import describe_missing as run

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(422, "no candidate profile loaded — import a resume first")
        row = await repo.get_or_create_candidate(session)
        result = await run(session, candidate, row.id, limit=limit, source=source or "")
        return result.to_dict()


@router.post("/apollo/postings")
async def apollo_postings(limit: int = Query(40, ge=1, le=200)) -> dict[str, Any]:
    """Ask Apollo for openings at the companies already held.

    Apollo is keyed on a domain rather than a board slug, so this reaches companies whose
    ATS was never resolved and whose careers page is a JavaScript shell — the two cases the
    board layer cannot serve. The postings arrive without descriptions; `/describe` fills
    those in for the ones worth reading.
    """
    from ..sources import apollo

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(422, "no candidate profile loaded — import a resume first")
        row = await repo.get_or_create_candidate(session)
        result = await apollo.harvest_postings(session, candidate, row.id, limit=limit)
        return result.to_dict()
