"""What you are looking for — spec §6.

    GET  /preferences          the current answers, and what they will do
    PUT  /preferences          set them
    GET  /preferences/options  the vocabulary a form should offer

The options endpoint exists so a UI offers exactly what the scorer acts on. A dropdown
containing "graduate" when the scorer recognises "fresher" produces a setting that silently
does nothing, which is worse than not offering it.

Changing a preference does not rescore what is already stored — `POST /graph/rescore` does
that, and keeping them apart means answering a question does not quietly rewrite a thousand
rows underneath you. The response says so.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import preferences
from ..logging import get_logger
from ..models.candidate import SearchConfig
from ..storage import repository as repo
from ..storage import session_scope

log = get_logger("routes.preferences")

router = APIRouter(prefix="/api/v1/preferences", tags=["preferences"])


class PreferencesIn(BaseModel):
    """Everything the scorer acts on. Every field optional — a partial answer is fine."""

    roles: list[str] = Field(
        default_factory=list,
        description="Titles you are targeting, e.g. Data Analyst, ML Engineer.",
        examples=[["Data Analyst", "ML Engineer"]],
    )
    locations: list[str] = Field(
        default_factory=list,
        description="Cities or regions. Leave empty to accept anywhere.",
        examples=[["Bangalore", "Chennai", "Remote"]],
    )
    experience_band: str | None = Field(
        default=None,
        description="internship | fresher | entry | 0-1 | 1-2 | mid | senior. "
        "A junior band rejects senior and lead roles outright.",
    )
    work_modes: list[str] = Field(
        default_factory=list, description="remote | hybrid | onsite. Empty accepts all."
    )
    employment_types: list[str] = Field(
        default_factory=list,
        description="internship | full_time | part_time | contract. Anything else is rejected.",
    )
    max_job_age_days: int | None = Field(
        default=30, ge=1, le=365, description="Reject postings older than this."
    )
    keywords_include: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(
        default_factory=list, description="A posting containing one of these is rejected."
    )
    companies_preferred: list[str] = Field(default_factory=list)
    companies_blocked: list[str] = Field(
        default_factory=list, description="Rejected outright, however well they score."
    )
    salary_min: float | None = None


async def _candidate_id() -> str:
    async with session_scope() as session:
        if await repo.load_candidate(session) is None:
            raise HTTPException(409, "Upload your resume first")
        row = await repo.get_or_create_candidate(session)
        return row.id


@router.get("")
async def read() -> dict[str, Any]:
    """Your current answers, and what each one actually does to the ranking."""
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        config = await preferences.load(session, candidate_id)
    return preferences.describe(config)


@router.put("")
async def write(body: PreferencesIn) -> dict[str, Any]:
    """Set what you are looking for.

    Values the scorer does not recognise are dropped rather than stored, so what comes back
    is what will actually be acted on.
    """
    candidate_id = await _candidate_id()
    config = SearchConfig(name=preferences.DEFAULT_NAME, **body.model_dump())

    async with session_scope() as session:
        saved = await preferences.save(session, candidate_id, config)

    described = preferences.describe(saved)
    return {
        **described,
        # Said explicitly, because the alternative — silently rewriting every stored score
        # the moment a dropdown changes — would be a surprising amount of work to trigger
        # from a form.
        "note": (
            "New searches and monitoring use this immediately. Jobs already scored keep "
            "their old scores until you rescore them."
        ),
        "rescore_with": "POST /api/v1/graph/rescore?apply=true",
    }


@router.get("/options")
async def options() -> dict[str, Any]:
    """The vocabulary a form should offer — exactly what the scorer acts on."""
    return {
        "experience_bands": preferences.EXPERIENCE_BANDS,
        "work_modes": preferences.WORK_MODES,
        "employment_types": preferences.EMPLOYMENT_TYPES,
        "junior_bands": sorted(preferences.JUNIOR_BANDS),
        "note": (
            "Choosing a junior band removes senior and lead roles from your results rather "
            "than ranking them lower."
        ),
    }
