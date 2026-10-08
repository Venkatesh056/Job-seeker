"""Company endpoints: research, fit scores, and ATS discovery.

Research reads only what a company publishes for reading — home page, about page,
careers page, robots.txt honoured. There is no enrichment vendor and no profile
scraping behind any of these endpoints.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import company as company_mod
from ..logging import get_logger
from ..storage import repository as repo
from ..storage import session_scope
from ..storage.models import ATSBoard, Company

log = get_logger("routes.companies")

router = APIRouter(prefix="/api/v1/companies", tags=["companies"])


class ResearchRequest(BaseModel):
    domain: str | None = None
    refresh: bool = False


def _serialize(company: Company, boards: list[ATSBoard] | None = None) -> dict[str, Any]:
    return {
        "id": company.id,
        "name": company.name,
        "domain": company.domain,
        "website": company.website,
        "industry": company.industry,
        "size_band": company.size_band,
        "hq_location": company.hq_location,
        "description": company.description,
        "tech_focus": list(company.tech_focus or []),
        "ai_relevance": company.ai_relevance,
        "fit_score": company.fit_score,
        "signals": company.signals or {},
        "researched_at": company.researched_at.isoformat() if company.researched_at else None,
        "boards": [{"provider": b.provider, "slug": b.slug} for b in (boards or [])],
    }


@router.get("/{company_id}")
async def detail(company_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    async with session_scope() as session:
        company = await repo.get_company(session, company_id)
        if company is None:
            raise HTTPException(404, "company not found")
        boards = list(
            (await session.execute(select(ATSBoard).where(ATSBoard.company_id == company_id)))
            .scalars()
            .all()
        )
        candidate = await repo.load_candidate(session)

        payload = _serialize(company, boards)
        if candidate is not None and company.researched_at:
            # Recomputed rather than stored: the fit depends on the candidate, and the
            # Fact Bank changes more often than a company's home page does.
            profile = company_mod.CompanyProfile(
                name=company.name,
                domain=company.domain,
                website=company.website,
                description=company.description or "",
                industry=company.industry,
                size_band=company.size_band,
                hq_location=company.hq_location,
                tech_focus=list(company.tech_focus or []),
                signals=company.signals or {},
                ai_relevance=company.ai_relevance,
                researched_at=company.researched_at,
            )
            score, reasons = company_mod.score_fit(profile, candidate)
            payload["fit_score"] = score
            payload["fit_reasons"] = reasons
        return payload


@router.post("/{company_id}/research")
async def research(company_id: str, body: ResearchRequest) -> dict[str, Any]:
    """Read a company's public pages and score the fit.

    Cached: an already-researched company returns its stored profile unless
    `refresh` is set. A home page does not change between discovery runs, and
    re-fetching it would make repeat runs slower for no new information.
    """
    async with session_scope() as session:
        company = await repo.get_company(session, company_id)
        if company is None:
            raise HTTPException(404, "company not found")
        candidate = await repo.load_candidate(session)

        if company.researched_at and not body.refresh:
            return {**_serialize(company), "cached": True}

        domain = body.domain or company.domain
        if not domain:
            raise HTTPException(
                422,
                "No domain known for this company. Pass one — research reads the "
                "company's own website, so it needs an address.",
            )

        profile = await company_mod.research(
            company.name, domain, candidate=candidate, session=session
        )
        if profile.error and not profile.researched:
            raise HTTPException(502, f"could not read {domain}: {profile.error}")

        await repo.save_company_profile(session, company, profile)
        return {**_serialize(company), "cached": False, "fit_reasons": profile.fit_reasons}


@router.post("/research/pending")
async def research_pending(limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    """Research companies that have never been researched.

    A batch housekeeping endpoint: discovery only researches companies whose jobs got
    past the relevance gate, so this fills in the rest when the user wants them.
    """
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        pending = await repo.companies_needing_research(session, limit)
        if not pending:
            return {"researched": 0, "companies": [], "note": "nothing pending"}

        done = []
        for company in pending:
            profile = await company_mod.research(company.name, company.domain, candidate=candidate)
            if profile.researched:
                await repo.save_company_profile(session, company, profile)
                done.append(
                    {
                        "id": company.id,
                        "name": company.name,
                        "fit_score": profile.fit_score,
                        "industry": profile.industry,
                        "ai_relevance": profile.ai_relevance,
                    }
                )
        return {"researched": len(done), "companies": done}


@router.post("/{company_id}/discover-ats")
async def discover_ats(company_id: str) -> dict[str, Any]:
    """Find which ATS a company uses, from its careers page.

    Resolving this is the real cost in job discovery (docs/00-research.md §3), so the
    result is stored on `ats_board` and reused by every later run.
    """
    import httpx

    from ..config import get_settings
    from ..sources.ats.discover import discover_from_website

    async with session_scope() as session:
        company = await repo.get_company(session, company_id)
        if company is None:
            raise HTTPException(404, "company not found")
        if not company.domain:
            raise HTTPException(422, "no domain known for this company")

        settings = get_settings()
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
            headers={"user-agent": settings.user_agent},
        ) as client:
            found = await discover_from_website(company.domain, client, settings)

        for provider, slug in found:
            await repo.upsert_ats_board(
                session, provider, slug, company.id, "careers_page", 0, True
            )
        return {
            "company_id": company_id,
            "found": [{"provider": p, "slug": s} for p, s in found],
            "note": (
                "no ATS link found in the careers page — it may be JavaScript-rendered"
                if not found
                else ""
            ),
        }


# ─── Applying to a company rather than to a job ─────────────────────────────


class CompanyApplyRequest(BaseModel):
    # Which openings to prepare. Omit to take the plan's own choice, which is the
    # applyable openings in rank order, as many as today's cap allows.
    job_ids: list[str] | None = None
    use_llm: bool = True
    limit: int | None = None


@router.get("/{company_id}/openings")
async def openings(
    company_id: str,
    refresh: bool = Query(True, description="Re-read the company's ATS boards first."),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
) -> dict[str, Any]:
    """Every opening at this company, and whether we would apply to each.

    Read-only. Nothing is prepared and nothing is submitted — this is the plan the user
    reviews before deciding, and the reason each skipped opening was skipped.
    """
    from ..applications import company_apply

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(422, "no candidate profile loaded — import a resume first")
        candidate_row = await repo.get_or_create_candidate(session)
        try:
            result = await company_apply.plan(
                session,
                company_id,
                candidate,
                candidate_row.id,
                refresh=refresh,
                min_score=min_score,
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return result.to_dict()


@router.post("/{company_id}/apply")
async def apply_to_company(company_id: str, body: CompanyApplyRequest) -> dict[str, Any]:
    """Prepare one application per opening, each with its own tailored resume.

    Submits nothing. Every application lands in the same approval queue a single
    application does, and the daily cap binds here exactly as it does everywhere else.
    """
    from ..applications import company_apply

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(422, "no candidate profile loaded — import a resume first")
        candidate_row = await repo.get_or_create_candidate(session)
        try:
            return await company_apply.apply(
                session,
                company_id,
                candidate,
                candidate_row.id,
                job_ids=body.job_ids,
                use_llm=body.use_llm,
                limit=body.limit,
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc


# ─── People at a company ────────────────────────────────────────────────────
#
# Read from what the company publishes about itself — its leadership and team pages. Every
# automated contact provider is unavailable on the free plans this project runs on, and the
# company's own page is the most defensible source in any case: a page saying "Priya Sharma,
# VP Engineering" was published deliberately, for exactly this purpose.
#
# Nothing here guesses an address. A contact detail cannot be constructed without its
# source, so there is nowhere to put one.


@router.post("/{company_id}/people")
async def discover_people(
    company_id: str,
    target_role: str = Query("", description="Defaults to the candidate's own target."),
    job_id: str = Query("", description="Rank against this specific opening."),
) -> dict[str, Any]:
    """Read the company's people pages, rank who matters, and remember them.

    Ranking is per candidate and per opening — the same engineering manager is a 96 for an
    ML candidate and a 12 for someone applying to finance — so the job it was scored
    against is stored with the number.
    """
    from ..people import discover, rank
    from ..people import store as people_store
    from ..storage.models import Job

    async with session_scope() as session:
        company = await repo.get_company(session, company_id)
        if company is None:
            raise HTTPException(404, "company not found")
        if not company.domain:
            raise HTTPException(
                422,
                "no domain known for this company, and its people pages are found from "
                "its website. Run ATS discovery or add a domain first.",
            )

        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(422, "no candidate profile loaded — import a resume first")
        row = await repo.get_or_create_candidate(session)

        job = await session.get(Job, job_id) if job_id else None
        config = await _preferences_for(session, row.id)
        role = target_role or (job.title if job else "") or _target_role_of(candidate, config)

        found = await discover(company.name, company.domain)
        if found.people:
            await people_store.save(session, company.id, company.name, found.people)

        employees = (company.signals or {}).get("apollo", {}).get("employees")
        ranked = rank(
            found.people,
            target_role=role,
            job_title=job.title if job else "",
            company_size=employees if isinstance(employees, int) else None,
            role_is_junior=_is_junior(config),
        )
        # `row.id` is the candidate this ranking was computed for. Passed explicitly
        # rather than left to the ambient binding: this route already knows the owner,
        # and a score filed against the wrong one is the bug the table exists to stop.
        await people_store.record_relevance(
            session, company.id, ranked, job_id=job_id, candidate_id=row.id
        )

        return {
            "company": company.name,
            "domain": company.domain,
            "target_role": role,
            "job": job.title if job else None,
            "people": [r.to_dict() for r in ranked],
            "found": len(found.people),
            "pages_read": found.pages_read,
            "note": found.note(),
        }


@router.get("/{company_id}/people")
async def list_people(company_id: str) -> dict[str, Any]:
    """People already known at this company. Reads nothing from the network."""
    from ..people import store as people_store

    async with session_scope() as session:
        company = await repo.get_company(session, company_id)
        if company is None:
            raise HTTPException(404, "company not found")
        rows = await people_store.for_company(session, company.id)
        return {
            "company": company.name,
            "people": [people_store.serialize(r, company.name) for r in rows],
            "count": len(rows),
        }


def _target_role_of(candidate: Any, config: Any = None) -> str:
    """The role the candidate is aiming at.

    The career detector already answers this from the Fact Bank — for this candidate it
    returns "Machine Learning" with the queries "Machine Learning Engineer", "ML Engineer".
    Asking it is far better than the fallback it replaced, which read a `job_title` fact
    the parser never produces and so always returned the useless "Engineer".
    """
    roles = (getattr(config, "roles", None) or []) if config is not None else []
    if roles:
        return str(roles[0])

    try:
        from ..careers.detect import detect

        result = detect(candidate)
        if result and result.matches:
            # A match carries the job family; the family carries the role names.
            family = result.matches[0].family
            queries = getattr(family, "queries", None) or []
            if queries:
                return str(queries[0])
            if label := getattr(family, "label", ""):
                return str(label)
    except Exception as exc:  # detection is a convenience, never a hard dependency
        log.debug("career_detect_unavailable", error=f"{type(exc).__name__}")

    titles = [f.label for f in candidate.facts if getattr(f.kind, "value", "") == "job_title"]
    return titles[0] if titles else "Engineer"


async def _preferences_for(session: Any, candidate_id: str) -> Any:
    from .. import preferences

    return await preferences.load(session, candidate_id)


def _is_junior(config: Any) -> bool:
    from .. import preferences

    return (getattr(config, "experience_band", "") or "").lower() in preferences.JUNIOR_BANDS
