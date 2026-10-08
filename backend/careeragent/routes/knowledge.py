"""The knowledge graph over HTTP — addendum §14–19.

    GET /graph              the candidate's own neighbourhood; the default view
    GET /graph/around       walk outward from any node
    GET /graph/stats        how big it would be, without building it
    GET /graph/questions/*  the seven traversals worth asking by name

Reads only. The graph is a projection over the relational tables, so there is nothing here
that could write to it even in principle — a `POST /graph` would have to write to `job` or
`candidate_fact`, which is what the rest of the API is for.

Every response carries `truncated`. A client that renders a capped graph and lets the user
conclude "that is all of it" would be drawing the wrong conclusion from a true picture.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..knowledge import around, candidate_graph, questions, stats
from ..logging import get_logger
from ..storage import repository as repo
from ..storage import session_scope

log = get_logger("routes.knowledge")

router = APIRouter(prefix="/api/v1/graph", tags=["graph"])

# Past this a force-directed layout is a grey disc, and the browser drops frames laying it
# out. The cap is offered rather than imposed so a client that wants a table can raise it.
MAX_NODES_CEILING = 800


async def _candidate_id() -> str:
    async with session_scope() as session:
        if await repo.load_candidate(session) is None:
            raise HTTPException(409, "Upload your resume first")
        row = await repo.get_or_create_candidate(session)
        return row.id


@router.get("")
async def graph(
    max_nodes: int = Query(400, ge=10, le=MAX_NODES_CEILING),
    jobs: int = Query(40, ge=0, le=200),
) -> dict[str, Any]:
    """You, your skills, the facts behind them, and what they reach."""
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        projected = await candidate_graph(
            session, candidate_id, max_nodes=max_nodes, job_limit=jobs
        )
    return projected.to_dict()


@router.get("/stats")
async def graph_stats() -> dict[str, Any]:
    """Counts only. Lets a client decide whether to ask for a view rather than requesting
    one and discovering it was capped."""
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        return await stats(session, candidate_id)


@router.get("/around")
async def neighbourhood(
    node: str = Query(description="A node id, e.g. `company:swiggy` or `skill:python`."),
    depth: int = Query(1, ge=1, le=2),
    max_nodes: int = Query(200, ge=10, le=MAX_NODES_CEILING),
) -> dict[str, Any]:
    """Walk outward from one node — the Obsidian-style local view.

    Depth is capped at two. Three hops from a skill reaches most of the database, which is a
    picture of everything and therefore of nothing.
    """
    if ":" not in node:
        raise HTTPException(422, "node must look like `kind:key`, e.g. `company:swiggy`")
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        projected = await around(session, node, candidate_id, depth=depth, max_nodes=max_nodes)
    return {"root": node, "depth": depth, **projected.to_dict()}


# ─── The questions ──────────────────────────────────────────────────────────


@router.get("/questions/untouched-companies")
async def untouched(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """Companies with open roles you score well on that you have never contacted."""
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        rows = await questions.untouched_companies(session, candidate_id, limit=limit)
    return {"count": len(rows), "companies": rows}


@router.get("/questions/bridging-skills")
async def bridging(
    limit: int = Query(10, ge=1, le=40),
    min_score: float = Query(0.4, ge=0.0, le=1.0),
) -> dict[str, Any]:
    """Skills that would connect you to jobs you already partly match.

    Names that are also ordinary English — "go", "c", "excel" — are left out. Even with
    word-boundary matching, "we want you to go beyond" reads as a Go requirement, and a
    recommendation built on that is worse than no recommendation.
    """
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        rows = await questions.bridging_skills(
            session, candidate_id, limit=limit, min_score=min_score
        )
    return {
        "count": len(rows),
        "min_score": min_score,
        "skills": rows,
        "excludes_ambiguous_names": True,
    }


@router.get("/questions/skill-reach")
async def reach(limit: int = Query(25, ge=1, le=100)) -> dict[str, Any]:
    """How many known jobs each of your skills connects you to.

    Useful both ways: high reach is what to lead a resume with, and zero reach usually means
    the Fact Bank spells a skill differently from the market rather than that nobody wants
    it.
    """
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        rows = await questions.skill_reach(session, candidate_id, limit=limit)
    return {"count": len(rows), "skills": rows}


@router.get("/questions/skill-companies")
async def skill_companies(
    top_skills: int = Query(8, ge=1, le=20),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Companies whose open roles ask for what you are strongest at, with those roles.

    Ranked by how many of your strongest skills a company wants, not by how many roles it
    has open — one role wanting four of your skills is a better lead than nine wanting one.
    """
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        rows = await questions.companies_for_my_strongest_skills(
            session, candidate_id, top_skills=top_skills, limit=limit
        )
    return {"count": len(rows), "companies": rows, "excludes_ambiguous_names": True}


@router.get("/questions/uncontacted-people")
async def uncontacted_people(
    min_job_score: float = Query(0.45, ge=0.0, le=1.0),
    min_relevance: int = Query(45, ge=0, le=100),
    per_company: int = Query(3, ge=1, le=20, description="At most this many per company."),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """People worth writing to at companies you match, who have not heard from you.

    Suppressed people are excluded outright rather than returned with a flag, so a client
    cannot render them and let someone click send. `reachable` says a channel exists; it
    does not say the system may use it.
    """
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        return await questions.uncontacted_at_matched_companies(
            session,
            candidate_id,
            min_job_score=min_job_score,
            min_relevance=min_relevance,
            per_company_cap=per_company,
            limit=limit,
        )


@router.get("/questions/what-worked")
async def what_worked(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """Applications that drew a response, and whether outreach preceded them.

    A rejection counts as a response — it is evidence the application was read, which is
    exactly what silence is not.
    """
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        return await questions.applications_that_went_somewhere(session, candidate_id, limit=limit)


@router.get("/questions/resume-outcomes")
async def resume_outcomes(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """Which tailored resume versions drew responses.

    `rate` is `null` below five submissions rather than rounded. One response from one
    application is not a 100% response rate, and showing it as one would make the weakest
    evidence in the system look like the strongest.
    """
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        return await questions.resume_versions_by_outcome(session, candidate_id, limit=limit)


@router.get("/questions/history")
async def history(company: str = Query(min_length=1)) -> dict[str, Any]:
    """Everything you have already done involving one company.

    The thing to check before writing to someone: what was applied for, which resume went,
    and whether anyone has been contacted.
    """
    candidate_id = await _candidate_id()
    async with session_scope() as session:
        return await questions.history_with(session, candidate_id, company)


@router.post("/rescore")
async def rescore_stored(
    apply: bool = Query(False, description="Write the new scores. Without it, nothing changes."),
    limit: int = Query(0, ge=0, le=5000, description="Only the first N, for a quick look."),
) -> dict[str, Any]:
    """Recompute stored job scores against the current scorer.

    Reads what is already in the database — no network, no LLM spend, nothing re-fetched.
    Embedding and reasoning scores are preserved: neither depends on skill matching, and the
    second one was paid for.

    Lives under `/graph` because it exists for the same reason the graph does — the skill
    matcher they share was wrong, and every score computed before the fix is still wrong
    until something recomputes it.

    Defaults to a dry run. `apply=true` writes.
    """
    from ..pipeline.rescore import rescore

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(409, "Upload your resume first")
        row = await repo.get_or_create_candidate(session)
        report = await rescore(session, candidate, row.id, dry_run=not apply, limit=limit or None)

    return {"summary": report.summary(), **report.to_dict()}
