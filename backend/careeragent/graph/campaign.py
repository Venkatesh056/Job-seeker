"""The discovery campaign: five agents, two parallel fan-outs, one LangGraph.

    START
      │
      ▼
    ① profile ─────────── resume → career field → families → search queries
      │
      ├── Send() ────────► ② sourcing × (families × sources)      ‖ parallel
      │                       each: one aggregator call, one query, one region
      ▼
    ③ aggregate ───────── fold hits into unique employers, drop staffing agencies
      │
      ├── Send() ────────► ④ enrichment × companies                ‖ parallel
      │                       each: resolve the company's own ATS board
      ▼
    ⑤ rank ────────────── score employers against the profile, decide
      │
      ▼
     END

The two `Send()` fan-outs are why this is a graph rather than a function. Sourcing is a
map over (family × source) and enrichment is a map over companies; both write into one
shared state from dozens of concurrent branches, and the reducers in `state.py` make
that merge the framework's problem instead of ours.

What deliberately stays *outside* the graph: the browser. Applying holds a live page
across a human approval, and a page cannot be checkpointed — see `graph/apply.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..careers import archetype_for, detect
from ..careers import families as fam
from ..events import EventType, Level, get_bus
from ..logging import get_logger
from ..sources import directory as dirsrc
from ..storage import repository as repo
from ..storage import session_scope
from .state import CampaignState, EnrichTask, SourcingTask

log = get_logger("graph.campaign")

# Concurrency ceilings. Free tiers rate-limit on burst as readily as on volume, and a
# hundred simultaneous requests is how a daily quota becomes a 429 in four seconds.
SOURCING_CONCURRENCY = 6
ENRICH_CONCURRENCY = 8

# Per-source call budget for one campaign, set by what the endpoint actually is.
#
# The metered sources are obvious: JSearch's free tier is 200 calls a *month*, so one
# run must not spend a week's worth. The keyless ones are less obvious and bit us on
# the first live run — Arbeitnow returns the *entire* board in one response and we
# filter it locally, so asking twenty times returns the same payload twenty times and
# earns a 429. A source that answers everything in one call gets budgeted one call.
CALL_BUDGET = {
    # Metered, keyed.
    "jsearch": 3,
    "adzuna": 12,
    "jooble": 8,
    # Keyless. Arbeitnow is a whole-board dump; the other two do filter server-side.
    "arbeitnow": 1,
    "remotive": 5,
    "themuse": 5,
}
DEFAULT_BUDGET = 10


async def _emit(type_: EventType, message: str, run_id: str | None = None, **payload: Any) -> None:
    import uuid as _uuid

    await get_bus().emit_one(
        type_,
        message,
        run_id=_uuid.UUID(run_id) if run_id else None,
        **payload,
    )


# ─── ① Profile agent ────────────────────────────────────────────────────────


async def profile_agent(state: CampaignState) -> dict[str, Any]:
    """Resume → career field → job families → the phrases to search for.

    Runs first and alone because everything downstream is a function of its output: the
    queries the sourcing agents send, the archetype the tailor uses, and the skills the
    matcher scores against.
    """
    run_id = state.get("run_id")
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)

    if candidate is None:
        return {"error": "no candidate — upload a resume first"}

    detection = detect(candidate)
    chosen_ids = state.get("family_ids") or detection.suggested_ids(4)
    resolved = fam.expand(chosen_ids, include_related=state.get("include_related", True))
    queries = fam.search_queries(resolved)

    await _emit(
        EventType.RUN_STARTED,
        f"Detected {detection.primary.label if detection.primary else 'unknown field'} — "
        f"searching {len(resolved)} job famil{'y' if len(resolved) == 1 else 'ies'} "
        f"across {len(state.get('locations') or [])} location(s)",
        run_id,
        families=[f.id for f in resolved],
        queries=len(queries),
        confident=detection.confident,
    )

    return {
        "detected_field": detection.primary.id if detection.primary else "",
        "archetype": archetype_for([f.id for f in resolved]),
        "resolved_families": [{"id": f.id, "label": f.label, "group": f.group} for f in resolved],
        "queries": queries,
        "hits": [],
        "source_calls": {},
        "source_errors": [],
        "exhausted": [],
    }


def fan_out_sourcing(state: CampaignState) -> list[Send] | str:
    """Build one Send per (family × location × source), within the call budget.

    Budgeting happens here rather than inside the agents because it is a property of
    the *campaign*, not of any one call: the agents cannot see each other, and a
    per-agent limit would multiply by however many branches got scheduled.
    """
    if state.get("error"):
        return END

    families = state.get("resolved_families") or []
    locations = state.get("locations") or ["Remote"]
    sources = dirsrc.available()
    if not sources:
        return END

    spent: dict[str, int] = {}
    tasks: list[Send] = []

    # Families first, then locations, then sources: when the budget runs out mid-way
    # the user's highest-priority family has already been searched everywhere.
    for family_info in families:
        family = fam.get(family_info["id"])
        if family is None:
            continue
        for query in family.queries[:2]:
            for location in locations:
                for source in sources:
                    budget = CALL_BUDGET.get(source.id, DEFAULT_BUDGET)
                    if spent.get(source.id, 0) >= budget:
                        continue
                    spent[source.id] = spent.get(source.id, 0) + 1
                    tasks.append(
                        Send(
                            "sourcing_agent",
                            SourcingTask(
                                query=query,
                                location=location,
                                source_id=source.id,
                                family_id=family.id,
                                max_age_days=state.get("max_age_days", 30),
                            ),
                        )
                    )

    log.info("sourcing_fanout", tasks=len(tasks), budget=spent)
    return tasks or END


# ─── ② Sourcing agents (parallel) ───────────────────────────────────────────

_sourcing_gate = asyncio.Semaphore(SOURCING_CONCURRENCY)


async def sourcing_agent(task: SourcingTask) -> dict[str, Any]:
    """One aggregator call. Returns hits, or an explanation and nothing else."""
    source = dirsrc.SOURCES_BY_ID.get(task["source_id"])
    if source is None:
        return {"hits": [], "source_calls": {}, "source_errors": [], "exhausted": []}

    async with _sourcing_gate, dirsrc.http_client() as client:
        result = await source.search(
            task["query"],
            task["location"],
            client=client,
            limit=50,
            max_age_days=task["max_age_days"],
        )

    hits = [
        {
            "name": hit.name,
            "source": hit.source,
            "domain": hit.domain,
            "location": hit.location,
            "job_title": hit.job_title,
            "job_url": hit.job_url,
            "category": hit.category,
            "posted_date": hit.posted_date.isoformat() if hit.posted_date else None,
            "description": hit.description,
            "family_id": task["family_id"],
        }
        for hit in result.hits
    ]

    if hits:
        log.debug(
            "sourcing_hit",
            source=source.id,
            query=task["query"],
            location=task["location"],
            hits=len(hits),
        )

    return {
        "hits": hits,
        "source_calls": {source.id: result.calls},
        "source_errors": [f"{source.id}: {result.error}"] if result.error else [],
        "exhausted": [source.id] if result.quota_exhausted else [],
    }


# ─── ③ Aggregation ──────────────────────────────────────────────────────────


async def aggregate_agent(state: CampaignState) -> dict[str, Any]:
    """Fold every hit into unique employers, best-attested first."""
    from datetime import date as _date

    from ..sources.directory.base import CompanyHit

    grouped: list[tuple[str, Any]] = []
    by_family: dict[str, list[CompanyHit]] = {}
    for raw in state.get("hits") or []:
        posted = None
        if raw.get("posted_date"):
            try:
                posted = _date.fromisoformat(raw["posted_date"])
            except ValueError:
                posted = None
        by_family.setdefault(raw.get("family_id", ""), []).append(
            CompanyHit(
                name=raw["name"],
                source=raw["source"],
                domain=raw.get("domain"),
                location=raw.get("location", ""),
                job_title=raw.get("job_title", ""),
                job_url=raw.get("job_url", ""),
                category=raw.get("category", ""),
                posted_date=posted,
                description=raw.get("description", ""),
            )
        )

    for family_id, hits in by_family.items():
        result = dirsrc.SourceResult(source="merged", hits=hits)
        grouped.append((family_id, result))

    companies = dirsrc.aggregate(grouped)
    capped = companies[: state.get("max_companies", 300)]

    run_id = state.get("run_id")
    errors = state.get("source_errors") or []
    await _emit(
        EventType.JOB_DUPLICATE,
        f"{len(capped)} unique employers from {len(state.get('hits') or [])} postings",
        run_id,
        companies=len(capped),
        postings=len(state.get("hits") or []),
        calls=state.get("source_calls") or {},
        level=Level.WARN if errors and not capped else Level.INFO,
    )
    for error in errors[:4]:
        await _emit(EventType.ERROR, error, run_id, level=Level.WARN)

    return {"companies": [c.to_dict() for c in capped]}


def fan_out_enrichment(state: CampaignState) -> list[Send] | str:
    """One Send per company, to resolve its own ATS board.

    Capped separately from the company list: resolving an ATS costs several HTTP
    requests against the company's own site, so enriching 300 companies is a very
    different act from listing them. The top N by attestation get resolved; the rest
    stay listed and are resolved on demand when the user clicks into them.
    """
    companies = state.get("companies") or []
    if not companies:
        return "rank_agent"

    limit = state.get("enrich_limit", 60)
    return [
        Send(
            "enrichment_agent",
            EnrichTask(
                company=company,
                candidate_id=state.get("candidate_id", ""),
                fetch_jobs=True,
            ),
        )
        for company in companies[:limit]
    ] or "rank_agent"


# ─── ④ Enrichment agents (parallel) ─────────────────────────────────────────

_enrich_gate = asyncio.Semaphore(ENRICH_CONCURRENCY)


async def enrichment_agent(task: EnrichTask) -> dict[str, Any]:
    """Turn a lead into something applyable.

    Resolves the employer's own ATS board from their website, then reads their real
    openings from it. This is the step that converts "Adzuna says Acme is hiring" into
    "here is Acme's Greenhouse board and the exact application URL" — and it is why the
    aggregators only ever need to supply a *name*.
    """
    company = dict(task["company"])
    name = company.get("name", "")
    domain = company.get("domain")

    async with _enrich_gate:
        if not domain:
            domain = await _guess_domain(name)
            company["domain"] = domain
        if not domain:
            company["enrich_note"] = "no website found; cannot resolve an ATS"
            return {"enriched": [company]}

        try:
            from ..sources.ats import ATSFetcher

            async with ATSFetcher() as fetcher:
                boards = await fetcher.fetch_many([domain], task["fetch_jobs"])
        except Exception as exc:
            company["enrich_note"] = f"{type(exc).__name__}"
            return {"enriched": [company]}

    jobs: list[dict[str, Any]] = []
    for _target, found in boards.items():
        for board in found:
            if not board.jobs:
                continue
            company["ats_provider"] = board.provider
            company["ats_slug"] = board.slug
            company["applyable"] = True
            for job in board.jobs[:25]:
                jobs.append(
                    {
                        "title": job.title,
                        "location": job.location or "",
                        "url": job.url,
                        "source": board.provider,
                        "company": name,
                        "description": (job.description or "")[:4000],
                        "source_job_id": job.source_job_id,
                    }
                )
            break

    company["ats_jobs"] = jobs
    company["ats_job_count"] = len(jobs)
    if not jobs:
        company["enrich_note"] = "no readable ATS board; apply through the posting link"
    return {"enriched": [company]}


async def _guess_domain(name: str) -> str | None:
    """Find an employer's website from their name.

    Uses DuckDuckGo's HTML endpoint, which is public, unauthenticated and returns
    result links without a key. Failure is normal and cheap: a company we cannot
    resolve stays in the list as a lead with its original posting URL.
    """
    import re
    from urllib.parse import unquote, urlparse

    import httpx

    from ..config import get_settings

    cleaned = re.sub(r"[^\w\s&-]", " ", name).strip()
    if not cleaned:
        return None

    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"user-agent": settings.user_agent},
        ) as client:
            response = await client.get(
                "https://duckduckgo.com/html/",
                params={"q": f"{cleaned} official website careers"},
            )
            if response.status_code >= 400:
                return None
            html = response.text
    except Exception:
        return None

    blocked = (
        "duckduckgo",
        "linkedin",
        "glassdoor",
        "indeed",
        "naukri",
        "wikipedia",
        "facebook",
        "twitter",
        "x.com",
        "youtube",
        "crunchbase",
        "zaubacorp",
        "tofler",
        "instagram",
        "ambitionbox",
        "justdial",
        "bloomberg",
        "reuters",
    )
    for match in re.finditer(r'uddg=([^&"]+)', html):
        url = unquote(match.group(1))
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if host and not any(b in host for b in blocked) and "." in host:
            return host
    return None


# ─── ⑤ Ranking agent ────────────────────────────────────────────────────────


async def rank_agent(state: CampaignState) -> dict[str, Any]:
    """Score the enriched companies against the candidate and persist the run.

    Jobs read from a real ATS board go through the existing three-stage scorer, so a
    company discovered by an aggregator is ranked by exactly the same machinery as one
    from a seed list. Companies with no readable board keep their aggregator listing as
    a lead — worth showing, not worth pretending is applyable.
    """
    from ..models.candidate import SearchConfig
    from ..models.job import RawJob
    from ..pipeline.dedup import deduplicate
    from ..pipeline.normalize import normalize_all
    from ..pipeline.opportunity import decide, rank, score_opportunity
    from ..pipeline.score import ScoreContext, score_all

    run_id = state.get("run_id")
    enriched = state.get("enriched") or []
    listed = {c["key"]: c for c in (state.get("companies") or [])}
    for company in enriched:
        listed[company["key"]] = company

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
    if candidate is None:
        return {"error": "candidate disappeared mid-campaign"}

    raw_jobs: list[RawJob] = []
    for company in enriched:
        for job in company.get("ats_jobs") or []:
            raw_jobs.append(
                RawJob(
                    source=job["source"],
                    source_job_id=job["source_job_id"],
                    title=job["title"],
                    url=job["url"],
                    apply_url=job["url"],
                    company_name=company["name"],
                    location=job.get("location", ""),
                    description=job.get("description", ""),
                    ats=job["source"],
                )
            )

    ranked_jobs: list[dict[str, Any]] = []
    if raw_jobs:
        config = SearchConfig(
            roles=[f["label"] for f in (state.get("resolved_families") or [])],
            locations=list(state.get("locations") or []),
            max_job_age_days=state.get("max_age_days", 30),
        )
        deduped = deduplicate(normalize_all(raw_jobs))
        scored = score_all(deduped.jobs, ScoreContext(candidate=candidate, config=config))
        by_id = {job.job_id: job for job, _ in scored}

        from ..agents import audit

        trail = audit.Trail(run_id=state.get("run_id"))
        scores = [score_opportunity(job, det) for job, det in scored]
        for score in rank(scores):
            job = by_id[score.job_id]
            decision = decide(job, score)
            if decision.action != "reject":
                trail.note(
                    "opportunity_scoring",
                    f"{decision.action} via {decision.strategy}",
                    task=f"what should happen with {job.title} at {job.company}?",
                    subject_type="job",
                    subject_id=job.job_id,
                    confidence=score.final_score,
                    evidence={
                        "final_score": score.final_score,
                        "priority": score.priority.value,
                        "matched_skills": score.deterministic.matched_skills[:10],
                    },
                    tools_used=["deterministic_scorer"],
                    outcome=decision.reason,
                )
            ranked_jobs.append(
                {
                    "job_id": job.job_id,
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "url": job.primary_url,
                    "ats": job.ats,
                    "score": score.final_score,
                    "priority": score.priority.value,
                    "action": decision.action,
                    "reason": decision.reason,
                    "matched": score.deterministic.matched_skills[:8],
                    "missing": score.deterministic.missing_skills[:6],
                }
            )

        async with session_scope() as session:
            row = await repo.get_or_create_candidate(session)
            trail.candidate_id = row.id
            await trail.flush(session)
            await repo.upsert_jobs(session, deduped.jobs)
            for score in scores:
                await repo.save_score(session, row.id, score)
            for company in enriched:
                if company.get("ats_provider"):
                    stored = await repo.upsert_company(
                        session, company["name"], company.get("domain")
                    )
                    await repo.upsert_ats_board(
                        session,
                        company["ats_provider"],
                        company["ats_slug"],
                        stored.id,
                        "campaign",
                        company.get("ats_job_count", 0),
                        True,
                    )

    applyable = sum(1 for c in listed.values() if c.get("applyable"))
    stats = {
        "postings_seen": len(state.get("hits") or []),
        "companies": len(listed),
        "enriched": len(enriched),
        "applyable_companies": applyable,
        "jobs_found": len(raw_jobs),
        "jobs_ranked": len(ranked_jobs),
        "worth_applying": sum(1 for j in ranked_jobs if j["action"] == "apply"),
        "source_calls": state.get("source_calls") or {},
        "exhausted": state.get("exhausted") or [],
    }

    await _emit(
        EventType.RUN_FINISHED,
        f"{len(listed)} companies, {applyable} with an applyable board, "
        f"{len(ranked_jobs)} ranked jobs",
        run_id,
        **{k: v for k, v in stats.items() if k not in {"source_calls", "exhausted"}},
    )

    ordered = sorted(
        listed.values(),
        key=lambda c: (bool(c.get("applyable")), c.get("ats_job_count", 0), c.get("open_roles", 0)),
        reverse=True,
    )
    return {"companies": ordered, "jobs": ranked_jobs, "ranked": ranked_jobs, "stats": stats}


# ─── The graph ──────────────────────────────────────────────────────────────


def build_campaign_graph():
    """Wire the five agents. Compiled once and reused; it holds no per-run state."""
    graph = StateGraph(CampaignState)

    graph.add_node("profile_agent", profile_agent)
    graph.add_node("sourcing_agent", sourcing_agent)
    graph.add_node("aggregate_agent", aggregate_agent)
    graph.add_node("enrichment_agent", enrichment_agent)
    graph.add_node("rank_agent", rank_agent)

    graph.add_edge(START, "profile_agent")
    graph.add_conditional_edges("profile_agent", fan_out_sourcing, ["sourcing_agent", END])
    graph.add_edge("sourcing_agent", "aggregate_agent")
    graph.add_conditional_edges(
        "aggregate_agent", fan_out_enrichment, ["enrichment_agent", "rank_agent"]
    )
    graph.add_edge("enrichment_agent", "rank_agent")
    graph.add_edge("rank_agent", END)

    return graph.compile()


_graph = None


def get_campaign_graph():
    global _graph
    if _graph is None:
        _graph = build_campaign_graph()
    return _graph


async def run_campaign(
    *,
    candidate_id: str,
    locations: list[str],
    family_ids: list[str] | None = None,
    include_related: bool = True,
    max_age_days: int = 30,
    max_companies: int = 300,
    enrich_limit: int = 60,
    run_id: str | None = None,
) -> CampaignState:
    """Run one discovery campaign end to end."""
    import uuid as _uuid

    initial: CampaignState = {
        "run_id": run_id or str(_uuid.uuid4()),
        "candidate_id": candidate_id,
        "locations": locations,
        "family_ids": family_ids or [],
        "include_related": include_related,
        "max_age_days": max_age_days,
        "max_companies": max_companies,
        "enrich_limit": enrich_limit,
    }
    # The recursion limit has to clear the fan-out width: LangGraph counts each Send
    # branch as a step, and a wide campaign legitimately schedules hundreds.
    return await get_campaign_graph().ainvoke(initial, {"recursion_limit": 200})
