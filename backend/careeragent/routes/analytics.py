"""Analytics endpoints.

Every rate in every response carries its sample size and a `reportable` flag. A
percentage computed on three applications is returned as `indicative_rate` with
`rate: null`, so a client cannot render it as a finding by accident.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import analytics
from ..storage import repository as repo
from ..storage import session_scope

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


async def _candidate_id() -> str:
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        return row.id


@router.get("/funnel")
async def funnel() -> dict[str, Any]:
    """Discovered → scored → past gate → strong → shortlisted → prepared → submitted
    → responses → interviews → offers, with the conversion at each step."""
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        return (await analytics.funnel(session, candidate_id)).to_dict()


@router.get("/rates")
async def rates() -> dict[str, Any]:
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        return await analytics.rates(session, candidate_id)


@router.get("/by-dimension")
async def by_dimension(dimension: str = Query("role")) -> dict[str, Any]:
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        try:
            return await analytics.by_dimension(session, candidate_id, dimension)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


@router.get("/dimensions")
async def dimensions() -> list[str]:
    return list(analytics.DIMENSIONS)


@router.get("/resume-performance")
async def resume_performance() -> dict[str, Any]:
    """Which archetype validates well, and which one actually gets replies.

    Both on one screen on purpose: an archetype scoring 98% on ATS heuristics with no
    responses is the interesting case, and either number alone hides it.
    """
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        return await analytics.resume_performance(session, candidate_id)


@router.get("/costs")
async def costs(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """LLM spend, by agent, and the cost per submitted application."""
    async with session_scope() as session:
        return await analytics.costs(session, days)


@router.get("/overview")
async def overview() -> dict[str, Any]:
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        return await analytics.overview(session, candidate_id)
