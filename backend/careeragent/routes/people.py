"""The People endpoints: who is worth contacting, across every company.

Read-only. Discovery lives on the company routes, because finding people is something you
do *to* a company; this is the cross-company view of what was found.

Every contact detail returned carries its source and verification state beside it. A page
that shows an address without saying where it came from invites somebody to trust a guess,
and this project has no way to produce a guess — a contact detail cannot be constructed
without provenance — so the labelling exists to make that visible rather than to hedge.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..logging import get_logger
from ..people import store as people_store
from ..storage import repository as repo
from ..storage import session_scope
from ..storage.models import Company

log = get_logger("routes.people")

router = APIRouter(prefix="/api/v1/people", tags=["people"])


@router.get("")
async def list_people(
    company_id: str = Query("", description="Restrict to one company."),
    function: str = Query("", description="ai_ml | data | engineering | recruiting | …"),
    min_relevance: int = Query(0, ge=0, le=100),
    min_seniority: int | None = Query(None, ge=0, le=5),
    has_email: bool | None = Query(None),
    has_linkedin: bool | None = Query(None),
    hiring_authority: bool | None = Query(None),
    contactable_only: bool = Query(False, description="Only published, verified addresses."),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Everyone known, best first, filtered.

    `contactable_only` means an address this project would actually send to without asking
    again. An unverified address is still returned by default and still shown — it simply
    never counts as contactable, which is the entire reason the two states are separate.
    """
    from sqlalchemy import select

    async with session_scope() as session:
        rows = await people_store.search(
            session,
            company_id=company_id,
            function=function,
            min_relevance=min_relevance,
            min_seniority=min_seniority,
            has_email=has_email,
            has_linkedin=has_linkedin,
            hiring_authority=hiring_authority,
            contactable_only=contactable_only,
            limit=limit,
        )
        names = {
            row[0]: row[1]
            for row in (await session.execute(select(Company.id, Company.name))).all()
        }
        people = [people_store.serialize(r, names.get(r.company_id, "")) for r in rows]

    verified = sum(1 for p in people if p["contactable_now"])
    return {
        "people": people,
        "count": len(people),
        "contactable": verified,
        "note": (
            f"{len(people)} person/people. {verified} with an address they published and "
            "we verified — the rest are shown with their source so you can decide, and "
            "are never written to automatically."
        )
        if people
        else (
            "Nobody found yet. Open a company and run 'Find people' — it reads their own "
            "team and leadership pages, and guesses nothing."
        ),
    }


@router.get("/functions")
async def functions() -> dict[str, Any]:
    """The function vocabulary, for building a filter that cannot mistype a value."""
    from ..people.relevance import CAREER_PRIORITIES, FUNCTION_MARKERS

    return {
        "functions": sorted(FUNCTION_MARKERS),
        "priorities_by_career": {k: list(v) for k, v in CAREER_PRIORITIES.items()},
        "note": (
            "Which functions matter depends on the career being targeted: an ML candidate "
            "wants the AI lead and the technical recruiter, a civil engineering candidate "
            "wants the project manager."
        ),
    }


@router.get("/{person_id}")
async def detail(person_id: str) -> dict[str, Any]:
    from ..storage.models import PersonRecord

    async with session_scope() as session:
        row = await session.get(PersonRecord, person_id)
        if row is None:
            raise HTTPException(404, "person not found")
        company = await repo.get_company(session, row.company_id)
        return people_store.serialize(row, company.name if company else "")
