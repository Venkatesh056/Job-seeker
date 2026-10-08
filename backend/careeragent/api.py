"""HTTP API.

    uvicorn careeragent.api:app --reload

Read endpoints are cheap. Anything that costs money or touches the outside world
is a run, and anything irreversible needs an explicit approval — including the
natural-language command endpoint, which returns a parsed search config for
review and never triggers an action itself (docs/01-architecture.md §6).
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sse_starlette.sse import EventSourceResponse

from . import __version__
from .auth.deps import bind_principal
from .config import get_settings
from .events import DatabaseSink, get_bus
from .logging import configure_logging, get_logger
from .models.candidate import SearchConfig
from .pipeline import embeddings as emb
from .routes import ROUTERS
from .sources.ats import PROVIDERS
from .sources.seeds import SEGMENTS
from .sources.seeds import segment as seed_segment
from .storage import init_db, resolve_url, session_scope
from .storage import repository as repo
from .storage.models import ATSBoard, Company, Gap, Job, JobListing, JobScore

log = get_logger("api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    await init_db()
    # Persist events as well as streaming them, so the activity feed and the
    # audit trail survive a restart.
    event_sink = DatabaseSink()
    get_bus().add_sink(event_sink)

    # Durable checkpoints, so an approval parked overnight resumes at the submit step
    # rather than re-tailoring and re-opening the form.
    from .graph.checkpoints import close_checkpointer, describe, open_checkpointer

    await open_checkpointer()

    # Career monitoring. Off unless `monitor_enabled` is set — a process that reaches out
    # to other people's servers on a timer should be switched on knowingly.
    from .monitoring import start as start_monitor
    from .net import tracing

    tracing.configure()
    monitoring_started = start_monitor()
    log.info(
        "api_started",
        database=resolve_url().split(":")[0],
        checkpoints=describe()["backend"],
        monitoring=monitoring_started,
    )

    # An abandoned approval leaves a Chromium running. The sweeper closes sessions
    # whose TTL expired; without it, walking away from the approval queue would leak
    # a browser per application.
    sweeper = asyncio.create_task(_sweep_sessions())
    try:
        yield
    finally:
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper
        from .applications.sessions import get_registry

        await get_registry().close_all()
        await close_checkpointer()

        from .monitoring import stop as stop_monitor

        await stop_monitor()
        # The bus outlives the app object, so the sink this lifespan added is this
        # lifespan's to take away.
        get_bus().remove_sink(event_sink)


async def _sweep_sessions(interval: float = 300.0) -> None:
    from .applications.sessions import get_registry

    registry = get_registry()
    while True:
        try:
            await asyncio.sleep(interval)
            await registry.sweep()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failing sweep must not kill the server
            log.warning("session_sweep_failed", error=str(exc)[:160])


# FastAPI serializes via Pydantic directly now; a custom ORJSON response class
# is both slower and deprecated.
# The identity dependency is attached to the *application*, not to the routers, and not
# decorated onto the endpoints that look like they need it.
#
# It was attached at `include_router` first, and the isolation tests caught it immediately:
# this module declares its own endpoints with `@app.get`, so `/api/v1/candidate` — the
# parsed resume — served any caller with no token at all. That is the whole argument against
# opt-in auth in one example. An app-level dependency runs for every route that exists now
# and every route anybody adds later; the few genuinely public paths declare themselves in
# `auth.deps.PUBLIC_PATHS`.
app = FastAPI(
    title="AI Career Agent",
    version=__version__,
    lifespan=lifespan,
    dependencies=[Depends(bind_principal)],
)

# The dashboard is a separate Next.js origin in development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Domain routers. This module keeps status, candidate, jobs, events and discovery
# runs; everything else lives in exactly one router under `routes/`.
for _router in ROUTERS:
    app.include_router(_router)


# ─── Schemas ────────────────────────────────────────────────────────────────


class JobOut(BaseModel):
    id: str
    title: str
    company: str
    location: str
    work_mode: str
    employment_type: str
    seniority: str
    posted_date: str | None = None
    ats: str | None = None
    urls: list[str] = Field(default_factory=list)
    final_score: float | None = None
    priority: str | None = None
    reasons: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)


class StatusOut(BaseModel):
    version: str
    database: str
    candidate: str | None
    facts: int
    verified_facts: int
    skills: int
    jobs: int
    boards: int
    companies_researched: int
    applications: int
    awaiting_approval: int
    semantic_matching: bool
    llm_available: bool
    ats_platforms: int
    resume_archetypes: int
    autonomy_level: int
    autonomy_description: str
    portal_adapters_enabled: bool
    checkpoints: dict[str, Any]
    decisions_recorded: int
    monitoring: dict[str, Any]
    # What the moving parts underneath are actually doing. Reported rather than
    # assumed: "are my rate limits shared across workers" and "where do screenshots
    # live" are questions whose wrong answer is invisible until it matters.
    infrastructure: dict[str, Any]


class CommandIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class CommandOut(BaseModel):
    """A parsed command, for review. Deliberately inert.

    An LLM misreading "apply to the top 10" must not be able to submit ten
    applications, so parsing and acting are separate endpoints.
    """

    understood: SearchConfig
    would_do: str
    confirmed: bool = False


# ─── System ─────────────────────────────────────────────────────────────────


@app.get("/api/v1/status", response_model=StatusOut)
async def status() -> StatusOut:
    from sqlalchemy import func

    from .applications import tracker
    from .llm.router import get_router
    from .resume import ARCHETYPES
    from .routes.settings import AUTONOMY_LEVELS

    settings = get_settings()
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        row = await repo.get_or_create_candidate(session)
        jobs = await repo.job_count(session)
        boards = len(await repo.known_boards(session))
        researched = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Company)
                    .where(Company.researched_at.is_not(None))
                )
            ).scalar()
            or 0
        )
        app_summary = await tracker.summary(session, row.id)
        from .agents import audit

        decisions = (await audit.summarize(session))["total"]

    from .graph.checkpoints import describe as describe_checkpoints
    from .monitoring import status as monitor_status
    from .net import tracing
    from .net.limits import get_limiter
    from .storage.objects import get_store

    async with session_scope() as session:
        monitoring = await monitor_status(session)

    return StatusOut(
        version=__version__,
        database="postgresql" if resolve_url().startswith("postgresql") else "sqlite",
        candidate=candidate.name if candidate else None,
        facts=len(candidate.facts) if candidate else 0,
        verified_facts=sum(1 for f in candidate.facts if f.verified) if candidate else 0,
        skills=len(candidate.skill_labels()) if candidate else 0,
        jobs=jobs,
        boards=boards,
        companies_researched=researched,
        applications=app_summary["total"],
        awaiting_approval=app_summary["needs_human"],
        semantic_matching=emb.available(),
        llm_available=get_router().available(),
        ats_platforms=len(PROVIDERS),
        resume_archetypes=len(ARCHETYPES),
        autonomy_level=settings.autonomy_level,
        autonomy_description=AUTONOMY_LEVELS[settings.autonomy_level],
        portal_adapters_enabled=settings.enable_portal_adapters,
        checkpoints=describe_checkpoints(),
        decisions_recorded=decisions,
        monitoring=monitoring,
        infrastructure={
            "rate_limits": get_limiter().describe(),
            "object_store": get_store().describe(),
            "tracing": tracing.describe(),
            "multi_user": settings.multi_user,
        },
    )


@app.get("/api/v1/providers")
async def providers() -> list[dict[str, Any]]:
    return [
        {
            "id": p.id,
            "label": p.label,
            "method": p.method,
            "empty_means_exists": p.empty_means_exists,
        }
        for p in PROVIDERS
    ]


# ─── Candidate ──────────────────────────────────────────────────────────────


@app.get("/api/v1/candidate")
async def candidate() -> dict[str, Any]:
    async with session_scope() as session:
        loaded = await repo.load_candidate(session)
    if loaded is None:
        raise HTTPException(404, "No candidate yet — run 'careeragent ingest-resume <file>'")
    return {
        "name": loaded.name,
        "email": str(loaded.email) if loaded.email else None,
        "location": loaded.location,
        "links": loaded.links,
        "skills": sorted(loaded.skill_labels()),
        "total_years_experience": loaded.total_years_experience(),
        "facts": len(loaded.facts),
    }


@app.get("/api/v1/candidate/facts")
async def facts(
    kind: str | None = None,
    unverified_only: bool = Query(False, description="Only facts still awaiting review."),
) -> list[dict[str, Any]]:
    """The Fact Bank.

    `id` is returned because a fact that cannot be addressed cannot be confirmed, and
    for a long time this endpoint did not return one — which is the whole reason the
    verified count sat at zero.
    """
    from .storage.models import Fact as FactRow

    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        rows = (
            (
                await session.execute(
                    select(FactRow)
                    .where(FactRow.candidate_id == row.id)
                    .order_by(FactRow.kind, FactRow.label)
                )
            )
            .scalars()
            .all()
        )

    return [
        {
            "id": f.id,
            "kind": f.kind,
            "label": f.label,
            "body": f.body,
            "evidence_source": f.evidence_source,
            "evidence_locator": f.evidence_locator,
            "strength": f.strength,
            "verified": f.verified,
            "first_used_at": f.first_used_at.isoformat() if f.first_used_at else None,
            "last_used_at": f.last_used_at.isoformat() if f.last_used_at else None,
        }
        for f in rows
        if (kind is None or f.kind == kind) and (not unverified_only or not f.verified)
    ]


class FactReview(BaseModel):
    """Which facts a person confirmed, and whether they confirmed or withdrew them."""

    fact_ids: list[str] = Field(min_length=1, max_length=500)
    verified: bool = True


@app.post("/api/v1/candidate/facts/verify")
async def verify_facts(body: FactReview) -> dict[str, Any]:
    """Confirm — or withdraw confirmation from — facts a person has actually read.

    Takes explicit ids and nothing else. There is deliberately no "verify everything"
    parameter: this flag is what lets a claim reach a generated resume, and an endpoint
    that could set it across the whole table in one call would make the Fact Bank's
    central guarantee a single misplaced request away from meaningless.

    Unverifying is allowed and is not an error. Somebody re-reading their own CV and
    deciding a line overstates things is the system working.
    """
    from .storage.models import Fact as FactRow

    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        rows = (
            (
                await session.execute(
                    select(FactRow).where(
                        FactRow.candidate_id == row.id,
                        FactRow.id.in_(body.fact_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        for fact in rows:
            fact.verified = body.verified
        changed = len(rows)

        # Flushed explicitly before counting. The counts are queries, and a query in a
        # session with pending changes triggers an autoflush — so a failure in the *write*
        # surfaced from the *count*, which made a locked database look like a broken
        # statistic.
        await session.flush()

        total = await session.scalar(
            select(func.count()).select_from(FactRow).where(FactRow.candidate_id == row.id)
        )
        verified = await session.scalar(
            select(func.count())
            .select_from(FactRow)
            .where(FactRow.candidate_id == row.id, FactRow.verified.is_(True))
        )

    missing = len(body.fact_ids) - changed
    log.info("facts_reviewed", changed=changed, verified=body.verified)
    return {
        "changed": changed,
        # Reported rather than silently ignored: an id that matched nothing usually
        # means a stale page, and the caller should know its list was out of date.
        "not_found": missing,
        "verified_total": int(verified or 0),
        "facts_total": int(total or 0),
    }


@app.get("/api/v1/candidate/gaps")
async def gaps(limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
    async with session_scope() as session:
        rows = (
            (await session.execute(select(Gap).order_by(Gap.seen_in_job_count.desc()).limit(limit)))
            .scalars()
            .all()
        )
    return [{"skill": r.skill, "seen_in_job_count": r.seen_in_job_count} for r in rows]


# ─── Jobs ───────────────────────────────────────────────────────────────────


def _to_job_out(job: Job, score: JobScore | None, urls: list[str]) -> JobOut:
    deterministic = (score.deterministic if score else {}) or {}
    return JobOut(
        id=job.id,
        title=job.title,
        company=job.company,
        location=job.location or "",
        work_mode=job.work_mode,
        employment_type=job.employment_type,
        seniority=job.seniority,
        posted_date=job.posted_date.isoformat() if job.posted_date else None,
        ats=job.ats,
        urls=urls,
        final_score=score.final_score if score else None,
        priority=score.priority if score else None,
        reasons=list(deterministic.get("reasons") or []),
        missing_skills=list(deterministic.get("missing_skills") or []),
    )


@app.get("/api/v1/jobs", response_model=list[JobOut])
async def jobs(
    limit: int = Query(25, ge=1, le=200),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    priority: str | None = None,
) -> list[JobOut]:
    async with session_scope() as session:
        candidate_row = await repo.get_or_create_candidate(session)
        rows = await repo.top_jobs(session, candidate_row.id, limit, min_score, priority)
        out = []
        for job, score in rows:
            listings = (
                (await session.execute(select(JobListing).where(JobListing.job_id == job.id)))
                .scalars()
                .all()
            )
            out.append(_to_job_out(job, score, [x.url for x in listings]))
    return out


@app.get("/api/v1/jobs/{job_id}")
async def job_detail(job_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        listings = (
            (await session.execute(select(JobListing).where(JobListing.job_id == job_id)))
            .scalars()
            .all()
        )
        score = (
            (await session.execute(select(JobScore).where(JobScore.job_id == job_id)))
            .scalars()
            .first()
        )
        return {
            **_to_job_out(job, score, [x.url for x in listings]).model_dump(),
            "description": job.description,
            "listings": [
                {"source": x.source, "url": x.url, "ats": x.ats, "scraped_at": x.scraped_at}
                for x in listings
            ],
            "score_detail": {
                "deterministic": score.deterministic if score else None,
                "semantic": score.semantic if score else None,
                "llm": score.llm if score else None,
                "weights_version": score.weights_version if score else None,
            },
        }


class RejectJobIn(BaseModel):
    reason: str = Field(default="", max_length=500)


@app.post("/api/v1/jobs/{job_id}/reject")
async def reject_job(job_id: str, body: RejectJobIn) -> dict[str, Any]:
    """Mark a job as not wanted.

    Recorded rather than deleted: a rejection with a reason is signal for the
    learning engine, and a deleted job comes straight back on the next discovery run.
    """
    from .applications import service
    from .applications.service import ApplicationError

    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        row = await repo.get_or_create_candidate(session)
        application = await repo.application_for_job(session, job_id, row.id)
        if application is None:
            application = await service.shortlist(session, job_id, row.id)
        try:
            await service.reject(session, application.id, body.reason)
        except ApplicationError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"job_id": job_id, "status": application.status, "reason": body.reason}


@app.get("/api/v1/companies")
async def companies(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    async with session_scope() as session:
        rows = (await session.execute(select(Company).limit(limit))).scalars().all()
        boards = (await session.execute(select(ATSBoard))).scalars().all()
    by_company: dict[str, list[dict[str, str]]] = {}
    for board in boards:
        if board.company_id:
            by_company.setdefault(board.company_id, []).append(
                {"provider": board.provider, "slug": board.slug}
            )
    return [
        {
            "id": c.id,
            "name": c.name,
            "domain": c.domain,
            "fit_score": c.fit_score,
            "boards": by_company.get(c.id, []),
        }
        for c in rows
    ]


# ─── Commands (parse only) ──────────────────────────────────────────────────


@app.post("/api/v1/commands", response_model=CommandOut)
async def parse_command(body: CommandIn) -> CommandOut:
    """Turn a natural-language instruction into a reviewable search config.

    Returns what it understood and what it *would* do. It never runs anything:
    acting on it is a separate, explicit call. Without an LLM configured this
    falls back to keyword parsing, which is deliberately conservative.
    """
    from .nl import parse_search_command

    config, summary = parse_search_command(body.text)
    return CommandOut(understood=config, would_do=summary)


# ─── Live activity ──────────────────────────────────────────────────────────


@app.get("/api/v1/events/stream")
async def event_stream() -> EventSourceResponse:
    """Server-sent stream of agent events, for the live activity feed."""
    bus = get_bus()

    async def generator():
        try:
            async for event in bus.subscribe():
                yield {
                    "event": event.type.value,
                    "data": event.model_dump_json(),
                }
        except asyncio.CancelledError:  # client disconnected
            raise

    return EventSourceResponse(generator())


@app.get("/api/v1/events/recent")
async def recent_events(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    async with session_scope() as session:
        rows = await repo.recent_events(session, limit)
    return [
        {
            "type": r.type,
            "level": r.level,
            "message": r.message,
            "job_id": r.job_id,
            "payload": r.payload,
            "at": r.at.isoformat(),
        }
        for r in rows
    ]


# ─── Dashboard ──────────────────────────────────────────────────────────────

WEB_DIR = Path(__file__).parent / "web"


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/v1/segments")
async def segments() -> dict[str, Any]:
    """Curated target lists and how many companies each covers."""
    return {
        name: {"companies": len(domains), "sample": list(domains[:6])}
        for name, domains in SEGMENTS.items()
    }


# ─── Resume upload ──────────────────────────────────────────────────────────

ALLOWED_RESUME_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}
MAX_RESUME_BYTES = 10 * 1024 * 1024


@app.post("/api/v1/resume/upload")
async def upload_resume(file: UploadFile = File(...)) -> dict[str, Any]:
    """Parse a resume into the Fact Bank.

    Every extracted fact records the line it came from, and all land unverified
    until a human confirms them. Nothing is invented to fill a gap.
    """
    from .resume import parse_resume, to_candidate
    from .storage.models import ResumeMaster

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_RESUME_SUFFIXES:
        raise HTTPException(
            400,
            f"Unsupported format '{suffix or 'unknown'}'. "
            f"Use one of: {', '.join(sorted(ALLOWED_RESUME_SUFFIXES))}",
        )

    payload = await file.read()
    if len(payload) > MAX_RESUME_BYTES:
        raise HTTPException(400, "Resume is larger than 10 MB")
    if not payload:
        raise HTTPException(400, "File is empty")

    # The key is generated, never derived from `file.filename`. That name belongs to
    # whoever is uploading: Starlette does not sanitise it, and the suffix allowlist above
    # does not help, because "../../../pwned.pdf" ends in .pdf and passes every check.
    # Sanitising is something you have to keep getting right; a generated key cannot be
    # wrong. The original name is kept below as metadata, for display only.
    from .storage import objects

    store = objects.get_store()
    # Display only. `Path(...).name` strips every directory component, so this can be shown
    # and stored in a column but can never become part of a path again.
    original_name = Path(file.filename or "resume").name or "resume"
    key = f"resumes/{uuid4().hex}{suffix}"
    stored = await store.put(key, payload, "application/octet-stream")

    scratch: Path | None = None
    saved = stored.local_path
    if saved is None:
        # A remote backend holds the bytes and the parser needs a real file. Deleted in the
        # `finally` below, including when parsing raises — a resume left in the system
        # temp directory is the candidate's employment history sitting somewhere nobody
        # is looking after it.
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(payload)
        scratch = saved = Path(handle.name)

    try:
        parsed = parse_resume(saved)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)

    if not parsed.facts:
        detail = parsed.warnings[0] if parsed.warnings else "The file may be a scanned image."
        raise HTTPException(422, f"No facts could be extracted. {detail}")

    candidate_model = to_candidate(parsed)
    async with session_scope() as db:
        row = await repo.save_candidate_profile(db, candidate_model)
        added, existing = await repo.replace_facts(db, row.id, parsed.facts)
        db.add(
            ResumeMaster(
                candidate_id=row.id,
                filename=original_name,
                storage_key=key,
                content_hash=parsed.content_hash,
                raw_text=parsed.raw_text,
                parsed={"sections": sorted(parsed.sections), "contact": parsed.contact},
            )
        )

    by_kind: dict[str, int] = {}
    for fact in parsed.facts:
        by_kind[fact.kind.value] = by_kind.get(fact.kind.value, 0) + 1

    return {
        "filename": original_name,
        "name": candidate_model.name,
        "contact": parsed.contact,
        "facts_added": added,
        "facts_existing": existing,
        "by_kind": by_kind,
        "skills": sorted(candidate_model.skill_labels()),
        "warnings": parsed.warnings,
    }


# ─── Discovery runs ─────────────────────────────────────────────────────────


class RunRequest(BaseModel):
    segment: str = "india"
    targets: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    experience: str | None = None
    max_age_days: int = Field(default=30, ge=1, le=3650)
    use_llm: bool = False
    use_semantic: bool = True
    research_companies: bool = True


class RunAccepted(BaseModel):
    run_id: str
    targets: int
    message: str


# Runs started by this process. The durable record is the agent_run table.
_RUNS: dict[str, dict[str, Any]] = {}


@app.post("/api/v1/runs/discovery", response_model=RunAccepted, status_code=202)
async def start_discovery(body: RunRequest, background: BackgroundTasks) -> RunAccepted:
    """Start a discovery run.

    Searches, scores and ranks. It does not apply to anything: submission is not
    reachable from this endpoint by design.
    """
    import uuid as _uuid

    targets = list(body.targets)
    if body.segment and body.segment != "none":
        try:
            targets += list(seed_segment(body.segment))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    targets = list(dict.fromkeys(t.strip() for t in targets if t.strip()))
    if not targets:
        raise HTTPException(400, "No targets: choose a segment or list company domains")

    async with session_scope() as db:
        loaded = await repo.load_candidate(db)
    if loaded is None or not loaded.skill_labels():
        raise HTTPException(
            409, "Upload your resume first — scoring needs a Fact Bank to compare against"
        )

    run_id = str(_uuid.uuid4())
    _RUNS[run_id] = {"status": "running", "stats": {}, "error": ""}

    config = SearchConfig(
        roles=body.roles,
        locations=body.locations,
        experience_band=body.experience or None,
        max_job_age_days=body.max_age_days,
    )
    background.add_task(
        _execute_run,
        run_id,
        targets,
        loaded,
        config,
        body.use_semantic,
        body.use_llm,
        body.research_companies,
    )
    return RunAccepted(
        run_id=run_id,
        targets=len(targets),
        message=f"Searching {len(targets)} companies. This usually takes 5-10 minutes.",
    )


async def _execute_run(
    run_id: str,
    targets: list[str],
    candidate_model: Any,
    config: SearchConfig,
    use_semantic: bool,
    use_llm: bool,
    research_companies: bool = True,
) -> None:
    from .learning import active_weight_model
    from .pipeline.run import run_discovery

    try:
        async with session_scope() as db:
            row = await repo.get_or_create_candidate(db)
            weights = await active_weight_model(db, row.id)

        result = await run_discovery(
            targets,
            candidate_model,
            config,
            use_semantic=use_semantic,
            use_llm=use_llm,
            research_companies=research_companies,
            store=True,
            autonomy_level=get_settings().autonomy_level,
            weights=weights,
            run_id=run_id,
        )
        _RUNS[run_id] = {
            "status": "failed" if result.error else "completed",
            "stats": result.stats.as_dict(),
            "error": result.error,
            "gaps": result.gaps[:10],
        }
    except Exception as exc:  # a failed run must not take the server down
        log.exception("run_failed", run_id=run_id)
        _RUNS[run_id] = {
            "status": "failed",
            "stats": {},
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }


@app.get("/api/v1/runs/{run_id}")
async def run_status(run_id: str) -> dict[str, Any]:
    if run_id not in _RUNS:
        raise HTTPException(404, "Unknown run")
    return {"run_id": run_id, **_RUNS[run_id]}


@app.get("/api/v1/runs")
async def list_runs() -> list[dict[str, Any]]:
    return [{"run_id": rid, **info} for rid, info in list(_RUNS.items())[-20:]]
