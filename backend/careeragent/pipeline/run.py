"""The discovery run, as one reusable service.

The CLI and the dashboard must execute the *same* pipeline. Two copies would
drift, and the one that drifted would be the one making decisions about someone's
job search.

Every stage emits an event, which is what the dashboard's live feed renders and
what `agent_event` records for audit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ..events import EventBus, EventType, Level, get_bus
from ..logging import get_logger
from ..models.candidate import Candidate, SearchConfig
from ..models.job import NormalizedJob
from ..models.scoring import JobScore
from ..sources.ats import ATSFetcher
from ..sources.ats.discover import domain_of
from . import embeddings as emb
from .dedup import deduplicate
from .normalize import normalize_all
from .opportunity import Decision, decide, missing_skill_gaps, rank, score_opportunity
from .score import ScoreContext, passes_llm_gate, score_all

log = get_logger("pipeline.run")


@dataclass
class RunStats:
    discovered: int = 0
    unique: int = 0
    duplicates_removed: int = 0
    rejected: int = 0
    past_gate: int = 0
    semantic_scored: int = 0
    llm_scored: int = 0
    worth_applying: int = 0
    boards_ok: int = 0
    boards_failed: int = 0
    companies_researched: int = 0
    outreach_opportunities: int = 0
    llm_cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class RunResult:
    run_id: str
    stats: RunStats = field(default_factory=RunStats)
    jobs: list[NormalizedJob] = field(default_factory=list)
    scores: dict[str, JobScore] = field(default_factory=dict)
    decisions: dict[str, Decision] = field(default_factory=dict)
    gaps: list[tuple[str, int]] = field(default_factory=list)
    error: str = ""

    def ranked(self, limit: int | None = None) -> list[tuple[NormalizedJob, JobScore]]:
        by_id = {j.job_id: j for j in self.jobs}
        ordered = [
            (by_id[s.job_id], s) for s in rank(list(self.scores.values())) if s.job_id in by_id
        ]
        return ordered[:limit] if limit else ordered


def company_name_for(target: str, slug: str) -> str:
    """Best available company name before company research fills it in.

    The board slug beats the target: an explicit "greenhouse:stripe" has no
    domain to read, and parsing one out yields the ATS name, not the employer.
    """
    if slug:
        head = slug.split("::")[0]
        if "." in head:  # Workday carries a tenant host
            head = head.split(".")[0]
        return head.replace("-", " ").replace("_", " ").title()
    if domain := domain_of(target):
        return domain.split(".")[0].replace("-", " ").title()
    return target


async def run_discovery(
    targets: list[str],
    candidate: Candidate,
    config: SearchConfig,
    *,
    include_descriptions: bool = True,
    use_semantic: bool = True,
    use_llm: bool = False,
    research_companies: bool = True,
    store: bool = True,
    autonomy_level: int = 4,
    weights: Any = None,
    bus: EventBus | None = None,
    run_id: str | None = None,
) -> RunResult:
    """Search, normalize, deduplicate, score and store. Never applies to anything.

    Submission is not reachable from here by design: this function ends at a
    ranked list and a recommendation.
    """
    bus = bus or get_bus()
    run_uuid = uuid.UUID(run_id) if run_id else uuid.uuid4()
    result = RunResult(run_id=str(run_uuid))

    await bus.emit_one(
        EventType.RUN_STARTED,
        f"Searching {len(targets)} target(s)",
        run_id=run_uuid,
        targets=len(targets),
        roles=config.roles,
        locations=config.locations,
    )

    # ─── Discover ───────────────────────────────────────────────────────────
    try:
        async with ATSFetcher() as fetcher:
            boards_by_target = await fetcher.fetch_many(targets, include_descriptions)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        await bus.emit_one(EventType.ERROR, result.error, run_id=run_uuid, level=Level.ERROR)
        return result

    raw_jobs = []
    board_rows: list[tuple[str, Any]] = []
    for target, boards in boards_by_target.items():
        for board in boards:
            board_rows.append((target, board))
            if board.jobs:
                result.stats.boards_ok += 1
                await bus.emit_one(
                    EventType.ATS_BOARD_DISCOVERED,
                    f"{company_name_for(target, board.slug)}: {len(board.jobs)} jobs via {board.provider}",
                    run_id=run_uuid,
                    provider=board.provider,
                    slug=board.slug,
                    jobs=len(board.jobs),
                    via=board.discovered_via,
                )
            else:
                result.stats.boards_failed += 1
            for raw in board.jobs:
                raw.company_name = raw.company_name or company_name_for(target, board.slug)
            raw_jobs.extend(board.jobs)

    result.stats.discovered = len(raw_jobs)
    if not raw_jobs:
        await bus.emit_one(
            EventType.RUN_FINISHED, "No jobs found", run_id=run_uuid, **result.stats.as_dict()
        )
        return result

    # ─── Normalize and deduplicate ──────────────────────────────────────────
    normalized = normalize_all(raw_jobs)
    deduped = deduplicate(normalized)
    result.jobs = deduped.jobs
    result.stats.unique = len(deduped.jobs)
    result.stats.duplicates_removed = deduped.duplicates_removed
    await bus.emit_one(
        EventType.JOB_DUPLICATE,
        f"{len(deduped.jobs)} unique jobs ({deduped.duplicates_removed} duplicates merged)",
        run_id=run_uuid,
        unique=len(deduped.jobs),
        removed=deduped.duplicates_removed,
    )

    # ─── Stage 1: deterministic ─────────────────────────────────────────────
    scored_pairs = score_all(deduped.jobs, ScoreContext(candidate=candidate, config=config))
    gated = [(job, det) for job, det in scored_pairs if passes_llm_gate(det)]
    result.stats.rejected = sum(1 for _, det in scored_pairs if det.hard_fail)
    result.stats.past_gate = len(gated)
    await bus.emit_one(
        EventType.JOB_SCORED,
        f"{len(gated)} jobs past the relevance gate ({result.stats.rejected} rejected)",
        run_id=run_uuid,
        past_gate=len(gated),
        rejected=result.stats.rejected,
    )

    # ─── Stage 2: on-device semantic ────────────────────────────────────────
    semantic: dict = {}
    if use_semantic and gated:
        semantic = emb.score_semantic(candidate, [job for job, _ in gated])
        result.stats.semantic_scored = len(semantic)
        if semantic:
            await bus.emit_one(
                EventType.JOB_ANALYZED,
                f"Semantic matching complete ({len(semantic)} jobs, on-device)",
                run_id=run_uuid,
                count=len(semantic),
            )

    # ─── Stage 3: LLM reasoning ─────────────────────────────────────────────
    llm_scores: dict = {}
    if use_llm and gated:
        from ..llm import get_router, score_jobs

        router = get_router()
        if router.available():
            llm_scores = score_jobs(candidate, [job for job, _ in gated], router)
            result.stats.llm_scored = len(llm_scores)
            result.stats.llm_cost_usd = router.stats["cost_usd"]
            await bus.emit_one(
                EventType.JOB_ANALYZED,
                f"LLM reasoning complete ({len(llm_scores)} jobs, ${result.stats.llm_cost_usd:.4f})",
                run_id=run_uuid,
                count=len(llm_scores),
                cost_usd=result.stats.llm_cost_usd,
            )
        else:
            await bus.emit_one(
                EventType.ERROR,
                "No LLM configured — continuing with deterministic scoring",
                run_id=run_uuid,
                level=Level.WARN,
            )

    # ─── Company intelligence ───────────────────────────────────────────────
    # Only for companies with jobs that got past the gate. Researching every
    # company found would mean fetching web pages for the 80% of jobs already
    # rejected, which is the same waste the LLM gate exists to prevent.
    company_fit: dict[str, float] = {}
    if research_companies and gated:
        company_fit = await _research_companies(
            [job for job, _ in gated], candidate, bus, run_uuid, store=store
        )
        result.stats.companies_researched = len(company_fit)

    # ─── Rank and decide ────────────────────────────────────────────────────
    from ..agents import audit

    trail = audit.Trail(run_id=str(run_uuid))
    for job, det in scored_pairs:
        score = score_opportunity(
            job,
            det,
            semantic=semantic.get(job.job_id),
            llm=llm_scores.get(job.job_id),
            company_fit=company_fit.get(_norm_company(job.company)),
            weights=weights,
        )
        result.scores[job.job_id] = score
        decision = decide(job, score, autonomy_level=autonomy_level)
        result.decisions[job.job_id] = decision
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
                    "components": {
                        "role": score.deterministic.role,
                        "skills": score.deterministic.skills,
                        "location": score.deterministic.location,
                        "company_fit": score.company_fit,
                    },
                    "matched_skills": score.deterministic.matched_skills[:10],
                    "missing_skills": score.deterministic.missing_skills[:10],
                },
                tools_used=["deterministic_scorer"]
                + (["embeddings"] if job.job_id in semantic else [])
                + (["llm"] if job.job_id in llm_scores else []),
                inputs={"autonomy_level": autonomy_level},
                outcome=decision.reason,
            )

    result.stats.worth_applying = sum(1 for d in result.decisions.values() if d.action == "apply")
    result.stats.outreach_opportunities = sum(
        1 for d in result.decisions.values() if d.action == "outreach"
    )
    result.gaps = missing_skill_gaps(list(result.scores.values()))

    # ─── Persist ────────────────────────────────────────────────────────────
    if store:
        try:
            async with session_scope_for_trail() as session:
                await trail.flush(session)
        except Exception:
            # An unwritable audit row must not cost the user a discovery run.
            log.debug("decision_trail_flush_failed", run_id=str(run_uuid))

        try:
            await _persist(
                board_rows,
                deduped.jobs,
                candidate,
                list(result.scores.values()),
                run_id=str(run_uuid),
            )
        except Exception as exc:
            log.exception("persist_failed", run_id=str(run_uuid))
            await bus.emit_one(
                EventType.ERROR,
                f"Could not store results: {type(exc).__name__}",
                run_id=run_uuid,
                level=Level.ERROR,
            )

    await bus.emit_one(
        EventType.RUN_FINISHED,
        f"{result.stats.unique} jobs, {result.stats.worth_applying} worth applying to",
        run_id=run_uuid,
        **result.stats.as_dict(),
    )
    return result


def _norm_company(name: str) -> str:
    from ..models.job import _norm

    return _norm(name)


async def _research_companies(
    jobs: list[NormalizedJob],
    candidate: Candidate,
    bus: EventBus,
    run_uuid: Any,
    *,
    store: bool = True,
    limit: int = 25,
) -> dict[str, float]:
    """Research the companies behind surviving jobs. Returns normalized name → fit.

    Already-researched companies are read from the database rather than re-fetched:
    a company's home page does not change between runs, and re-reading it would make
    every repeat run slower for no new information.
    """
    from .. import company as company_mod
    from ..models.job import _norm
    from ..storage import repository as repo
    from ..storage import session_scope

    domains: dict[str, str] = {}
    for job in jobs:
        key = _norm(job.company)
        if key and job.company_domain and key not in domains:
            domains[key] = job.company_domain

    fit: dict[str, float] = {}
    if store:
        try:
            async with session_scope() as session:
                fit.update(await repo.company_fit_map(session))
        except Exception as exc:
            log.debug("company_fit_read_failed", error=str(exc)[:120])

    todo = [(k, d) for k, d in domains.items() if k not in fit][:limit]
    if not todo:
        return fit

    import httpx

    from ..config import get_settings

    settings = get_settings()
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=True,
        headers={"user-agent": settings.user_agent, "accept": "text/html,*/*"},
    ) as client:
        for key, domain in todo:
            display = next((j.company for j in jobs if _norm(j.company) == key), key)
            profile = await company_mod.research(
                display, domain, client=client, candidate=candidate
            )
            if profile.fit_score is None:
                continue
            fit[key] = profile.fit_score
            await bus.emit_one(
                EventType.COMPANY_ANALYZED,
                f"{display}: company fit {profile.fit_score:.0%}"
                + (f" — {profile.fit_reasons[0]}" if profile.fit_reasons else ""),
                run_id=run_uuid,
                company=display,
                fit=profile.fit_score,
                industry=profile.industry,
            )
            if store:
                try:
                    async with session_scope() as session:
                        row = await repo.upsert_company(session, display, domain)
                        await repo.save_company_profile(session, row, profile)
                except Exception as exc:
                    log.debug("company_persist_failed", error=str(exc)[:120])
    return fit


def session_scope_for_trail() -> Any:
    from ..storage import session_scope

    return session_scope()


async def _persist(
    board_rows, jobs, candidate: Candidate, scores, run_id: str | None = None
) -> None:
    from ..llm.router import get_router
    from ..storage import repository as repo
    from ..storage import session_scope

    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session, candidate.name)
        for target, board in board_rows:
            company = None
            if board.jobs:
                company = await repo.upsert_company(
                    session, company_name_for(target, board.slug), domain_of(target) or None
                )
            await repo.upsert_ats_board(
                session,
                board.provider,
                board.slug,
                company.id if company else None,
                board.discovered_via,
                len(board.jobs),
                board.ok,
            )
        await repo.upsert_jobs(session, jobs)
        for score in scores:
            await repo.save_score(session, row.id, score)
        missing = [s for score in scores for s in score.deterministic.missing_skills]
        if missing:
            await repo.record_gaps(session, row.id, missing)
        await repo.record_llm_calls(session, get_router().drain_calls(), run_id)
