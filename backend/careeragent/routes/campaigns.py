"""Campaign endpoints: resume → field → region → companies → apply.

This is the flow the product is actually for, and the ordering of these endpoints is
the flow:

    GET  /careers/detect        what field is this resume in, and which families?
    GET  /careers/families      the full picker, grouped
    GET  /campaigns/sources     which data sources are live, and where to get the rest
    POST /campaigns/run         region + families → companies, ranked, ATS-resolved
    GET  /campaigns/{id}        the company list, while it is still filling in
    POST /campaigns/apply       hand N jobs to the pool, three at a time
    GET  /campaigns/batch       what the pool is doing right now
    POST /campaigns/approve     answer one parked approval

Nothing here submits on its own. `/apply` starts the pool in whatever mode the request
asked for, and in the default `preview` mode every application parks at an approval
that `/approve` has to answer.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..careers import detect
from ..careers import families as fam
from ..graph import get_pool, run_campaign
from ..graph.apply import pending_approval, resume_application
from ..logging import get_logger
from ..sources import directory as dirsrc
from ..storage import repository as repo
from ..storage import session_scope

log = get_logger("routes.campaigns")

router = APIRouter(prefix="/api/v1", tags=["campaigns"])

# Finished campaigns, kept in memory for the dashboard to read back. The durable record
# is the jobs and companies written to the database by the ranking agent; this is the
# run-shaped view of it.
_CAMPAIGNS: dict[str, dict[str, Any]] = {}


class CampaignRequest(BaseModel):
    locations: list[str] = Field(default_factory=lambda: ["Bangalore"], max_length=6)
    family_ids: list[str] = Field(default_factory=list, max_length=12)
    include_related: bool = True
    max_age_days: int = Field(default=30, ge=1, le=365)
    max_companies: int = Field(default=300, ge=10, le=1000)
    enrich_limit: int = Field(default=60, ge=0, le=300)


class ApplyRequest(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=50)
    mode: str = Field(default="preview", description="preview | one_click | threshold")
    concurrency: int = Field(default=3, ge=1, le=5)
    use_llm: bool = True


class ApprovalRequest(BaseModel):
    approved: bool
    field_overrides: dict[str, str] = Field(default_factory=dict)


# ─── Careers ────────────────────────────────────────────────────────────────


@router.get("/careers/families")
async def families() -> dict[str, Any]:
    """Every job family, grouped for the picker."""
    return {
        "groups": {
            group: [
                {
                    "id": f.id,
                    "label": f.label,
                    "queries": list(f.queries[:3]),
                    "related": list(f.related),
                }
                for f in members
            ]
            for group, members in fam.by_group().items()
        },
        "total": len(fam.FAMILIES),
    }


@router.get("/careers/detect")
async def detect_field() -> dict[str, Any]:
    """What field this resume is in, and which families to search.

    Evidence-based and explainable: each match carries the skills and titles that
    produced it, so a wrong answer can be seen to be wrong rather than merely
    disagreed with.
    """
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
    if candidate is None:
        raise HTTPException(409, "Upload your resume first")

    detection = detect(candidate)
    suggested = detection.suggested_ids(6)
    return {
        **detection.to_dict(),
        "suggested": suggested,
        "expanded": [
            {"id": f.id, "label": f.label, "group": f.group} for f in fam.expand(suggested[:3])
        ],
    }


# ─── Sources ────────────────────────────────────────────────────────────────


@router.get("/campaigns/sources")
async def sources() -> dict[str, Any]:
    """Which company-discovery sources are live, and how to turn the others on.

    Surfaced prominently because it is the single biggest determinant of how many
    companies a region search returns, and an empty result with no explanation reads
    as "the tool is broken" rather than "three free keys are missing".
    """
    described = dirsrc.describe()
    configured = [s for s in described if s["configured"]]
    keyed = [s for s in described if s["needs_key"] and not s["configured"]]
    return {
        "sources": described,
        "configured": len(configured),
        "total": len(described),
        "missing_keys": keyed,
        "note": (
            "Keyless sources cover remote roles well and cities poorly. For regional "
            "coverage — Bangalore, Chennai, Pune — add Adzuna at minimum; it is free, "
            "instant, and covers India properly."
            if keyed
            else "All sources configured."
        ),
    }


# ─── Campaigns ──────────────────────────────────────────────────────────────


@router.post("/campaigns/run")
async def start_campaign(body: CampaignRequest) -> dict[str, Any]:
    """Run the five-agent discovery campaign.

    Runs inline rather than in the background: the caller wants the company list, and
    a campaign takes under a minute at typical settings. Long runs stream their progress
    through the normal event feed while this is in flight.
    """
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(409, "Upload your resume first")
        row = await repo.get_or_create_candidate(session)
        candidate_id = row.id

    if not dirsrc.available():
        raise HTTPException(
            503,
            "No company-discovery source is reachable. The keyless ones may be rate "
            "limited; add an Adzuna key for regional coverage.",
        )

    run_id = str(uuid.uuid4())
    try:
        state = await run_campaign(
            candidate_id=candidate_id,
            locations=[loc.strip() for loc in body.locations if loc.strip()],
            family_ids=body.family_ids,
            include_related=body.include_related,
            max_age_days=body.max_age_days,
            max_companies=body.max_companies,
            enrich_limit=body.enrich_limit,
            run_id=run_id,
        )
    except Exception as exc:
        log.exception("campaign_failed", run_id=run_id)
        raise HTTPException(500, f"campaign failed: {type(exc).__name__}") from exc

    if state.get("error"):
        raise HTTPException(409, state["error"])

    payload = {
        "campaign_id": run_id,
        "detected_field": state.get("detected_field"),
        "archetype": state.get("archetype"),
        "families": state.get("resolved_families") or [],
        "locations": body.locations,
        "companies": state.get("companies") or [],
        "jobs": state.get("ranked") or [],
        "stats": state.get("stats") or {},
        "source_errors": (state.get("source_errors") or [])[:8],
    }
    _CAMPAIGNS[run_id] = payload
    return payload


@router.get("/campaigns")
async def list_campaigns() -> list[dict[str, Any]]:
    return [
        {
            "campaign_id": cid,
            "locations": payload.get("locations"),
            "companies": len(payload.get("companies") or []),
            "jobs": len(payload.get("jobs") or []),
            "stats": payload.get("stats"),
        }
        for cid, payload in list(_CAMPAIGNS.items())[-10:]
    ]


# ─── The pool ───────────────────────────────────────────────────────────────


@router.post("/campaigns/apply")
async def apply_batch(body: ApplyRequest) -> dict[str, Any]:
    """Hand a set of jobs to the pool. Three run at once; the rest queue.

    Returns as soon as the batch is queued. Poll `/campaigns/batch` for progress — each
    item reports what was filled, what was refused, and whether it is parked at an
    approval.
    """
    from ..graph.apply import MODES

    if body.mode not in MODES:
        raise HTTPException(422, f"mode must be one of: {', '.join(MODES)}")

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(409, "Upload your resume first")
        candidate_id = (await repo.get_or_create_candidate(session)).id

    pool = get_pool()
    try:
        batch = await pool.start(
            body.job_ids,
            candidate_id,
            mode=body.mode,
            concurrency=body.concurrency,
            use_llm=body.use_llm,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    return {
        **batch.summary(),
        "note": (
            "Nothing has been submitted. Each application fills its form and parks for "
            "your approval."
            if body.mode == "preview"
            else "Applications will be submitted as they pass the pre-submit verdict."
        ),
    }


@router.get("/campaigns/batch/current")
async def current_batch() -> dict[str, Any]:
    pool = get_pool()
    batch = pool.current()
    if batch is None:
        return {"running": False, "recent": pool.recent()}
    return {"running": pool.running, **batch.summary()}


@router.post("/campaigns/batch/stop")
async def stop_batch() -> dict[str, Any]:
    """Stop after the in-flight applications finish.

    Not a hard cancel: killing a worker mid-submit is how an application goes out with
    no record that it did.
    """
    stopped = await get_pool().stop()
    return {
        "stopped": stopped,
        "note": "In-flight applications will finish; nothing further will start."
        if stopped
        else "no batch running",
    }


# ─── Approvals ──────────────────────────────────────────────────────────────


@router.get("/campaigns/approvals/{application_id}")
async def approval(application_id: str) -> dict[str, Any]:
    """What a parked application is waiting to be told."""
    parked = await pending_approval(application_id)
    if parked is None:
        raise HTTPException(404, "this application is not waiting on an approval")
    return parked


@router.post("/campaigns/approvals/{application_id}")
async def answer_approval(application_id: str, body: ApprovalRequest) -> dict[str, Any]:
    """Approve or decline one parked application.

    Approving resumes the graph at the submit agent with the tailored resume, generated
    answers and held browser page intact. The pre-submit verdict still runs before the
    click, so approving does not force a submit past a field that went blank while it
    waited.
    """
    parked = await pending_approval(application_id)
    if parked is None:
        raise HTTPException(404, "this application is not waiting on an approval")

    state = await resume_application(
        application_id,
        approved=body.approved,
        field_overrides=body.field_overrides,
    )
    return {
        "application_id": application_id,
        "approved": body.approved,
        "status": state.get("status"),
        "evidence": state.get("evidence") or {},
        "holds": state.get("holds") or [],
        "error": state.get("error", ""),
    }


@router.get("/campaigns/approvals")
async def pending_approvals() -> dict[str, Any]:
    """Every application in the current batch that is parked at an approval."""
    batch = get_pool().current()
    if batch is None:
        return {"pending": [], "total": 0}

    waiting = [item for item in batch.items if item.awaiting_approval]
    detail = await asyncio.gather(
        *(pending_approval(item.application_id) for item in waiting),
        return_exceptions=True,
    )
    return {
        "pending": [
            {
                **item.to_dict(),
                "preview": payload if isinstance(payload, dict) else None,
            }
            for item, payload in zip(waiting, detail, strict=False)
        ],
        "total": len(waiting),
    }


@router.get("/campaigns/modes")
async def modes() -> dict[str, Any]:
    return {
        "modes": [
            {
                "id": "preview",
                "label": "Fill, show me, then submit on a second click",
                "description": (
                    "Every application parks at an approval with what was filled and "
                    "what was refused. Default."
                ),
            },
            {
                "id": "one_click",
                "label": "Fill and submit — clicking Apply was the approval",
                "description": (
                    "Submits without a second confirmation. Still stops for CAPTCHA, "
                    "MFA, login walls, demographic and sponsorship questions."
                ),
            },
            {
                "id": "threshold",
                "label": "Submit above a match score, queue the rest",
                "description": "Auto-submits strong matches; parks marginal ones.",
            },
        ],
        "hard_stops": [
            "CAPTCHA",
            "MFA / one-time codes",
            "login or account creation",
            "identity or document verification",
            "demographic questions",
            "work authorisation and sponsorship declarations",
        ],
        "note": "No mode can reach the hard stops. They are refused a layer below.",
    }


# ─── Declared last on purpose ───────────────────────────────────────────────
# FastAPI matches in declaration order, so a path parameter this greedy has to come
# after every literal path that shares its prefix — otherwise `/campaigns/modes`
# resolves here and 404s as "unknown campaign".


@router.get("/campaigns/{campaign_id}")
async def campaign(campaign_id: str) -> dict[str, Any]:
    result = _CAMPAIGNS.get(campaign_id)
    if result is None:
        raise HTTPException(404, "unknown campaign")
    return result
