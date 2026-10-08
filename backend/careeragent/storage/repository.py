"""Persistence operations.

Kept as functions over an `AsyncSession` rather than a repository class per
table: the useful unit here is "persist a discovery run", which spans jobs,
listings, companies and boards, and splitting that across four objects would
just move the coordination somewhere less visible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..models.candidate import Candidate as CandidateModel
from ..models.job import NormalizedJob, _norm
from ..models.scoring import JobScore as JobScoreModel
from .models import (
    AgentEventRow,
    AgentRun,
    Application,
    ApplicationAnswer,
    ApplicationEvent,
    ApplicationStatus,
    ATSBoard,
    Candidate,
    Company,
    CoverLetter,
    Fact,
    Gap,
    Job,
    JobListing,
    JobScore,
    LLMCall,
    QuestionMemory,
    RankingWeights,
    ResumeMaster,
    ResumeRender,
)

log = get_logger("storage.repository")


# ─── Candidate ──────────────────────────────────────────────────────────────


async def _scoped_candidate(session: AsyncSession) -> Candidate | None:
    """The candidate this unit of work is for.

    This function is the whole of multi-tenancy. `candidate_id` has been threaded through
    every per-user table since the first migration; what made the system single-user was one
    line here — `select(Candidate).limit(1)`, the first row in the table.

    With an owner bound, it matches on the owner. Without one:

    * single-user mode falls back to the sole row, which is correct when there is one; and
    * multi-user mode raises, because there the first row is somebody else's.

    The refusal is the point. A silent fallback would not fail any test — it would serve the
    wrong person's applications, resumes and outreach, and look exactly like working.
    """
    from ..auth import tenancy

    principal = tenancy.current()
    if principal is not None:
        return (
            await session.execute(select(Candidate).where(Candidate.user_id == principal.user_id))
        ).scalar_one_or_none()

    # Raises in multi-user mode; returns None in single-user mode.
    tenancy.current_candidate_id()
    return (await session.execute(select(Candidate).limit(1))).scalar_one_or_none()


async def get_or_create_candidate(session: AsyncSession, name: str = "me") -> Candidate:
    """The candidate for the current owner, created on first use.

    Keeps its signature. There are 111 call sites downstream of this function, and threading
    an owner through each one would have been 111 chances to forget — and a forgotten one
    does not fail, it quietly returns somebody else's profile. Changing what the lookup
    *means* fixes them all at once.
    """
    from ..auth import tenancy

    row = await _scoped_candidate(session)
    if row is None:
        principal = tenancy.current()
        row = Candidate(name=name, user_id=principal.user_id if principal else None)
        session.add(row)
        await session.flush()
        log.info("candidate_created", id=row.id, name=name, owned=bool(principal))
    return row


async def save_candidate_profile(session: AsyncSession, candidate: CandidateModel) -> Candidate:
    row = await get_or_create_candidate(session, candidate.name)
    row.name = candidate.name
    row.email = str(candidate.email) if candidate.email else None
    row.phone = candidate.phone
    row.location = candidate.location
    row.preferred_locations = list(candidate.preferred_locations)
    row.links = dict(candidate.links)
    row.work_authorization = candidate.work_authorization.model_dump(mode="json")
    row.salary_preferences = candidate.salary_preferences.model_dump(mode="json")
    row.notice_period = candidate.notice_period
    row.availability = candidate.availability
    await session.flush()
    return row


async def replace_facts(
    session: AsyncSession, candidate_id: str, facts: list[Any]
) -> tuple[int, int]:
    """Insert facts, skipping ones already present.

    Matching on (kind, lowercased label) rather than id, because re-parsing the
    same resume must not duplicate the bank — and must not silently discard the
    `verified` flag a human already set on an existing fact.
    """
    existing = (
        (await session.execute(select(Fact).where(Fact.candidate_id == candidate_id)))
        .scalars()
        .all()
    )
    seen = {(f.kind, f.label.strip().lower()) for f in existing}

    added = 0
    for fact in facts:
        key = (str(fact.kind), fact.label.strip().lower())
        if key in seen:
            continue
        session.add(
            Fact(
                candidate_id=candidate_id,
                kind=str(fact.kind),
                label=fact.label,
                body=fact.body,
                evidence_source=str(fact.evidence_source),
                evidence_locator=fact.evidence_locator,
                first_used_at=fact.first_used_at,
                last_used_at=fact.last_used_at,
                strength=fact.strength,
                verified=fact.verified,
                aliases=list(fact.aliases),
            )
        )
        seen.add(key)
        added += 1

    await session.flush()
    return added, len(existing)


async def load_candidate(session: AsyncSession) -> CandidateModel | None:
    """Rebuild the in-memory candidate (with Fact Bank) used by the scorers."""
    from ..models.candidate import EvidenceSource, FactKind
    from ..models.candidate import Fact as FactModel

    row = await _scoped_candidate(session)
    if row is None:
        return None

    fact_rows = (
        (await session.execute(select(Fact).where(Fact.candidate_id == row.id))).scalars().all()
    )

    return CandidateModel(
        name=row.name,
        email=row.email,
        phone=row.phone,
        location=row.location or "",
        preferred_locations=row.preferred_locations or [],
        links=row.links or {},
        notice_period=row.notice_period or "",
        availability=row.availability or "",
        facts=[
            FactModel(
                kind=FactKind(f.kind),
                label=f.label,
                body=f.body or "",
                evidence_source=EvidenceSource(f.evidence_source),
                evidence_locator=f.evidence_locator,
                first_used_at=f.first_used_at,
                last_used_at=f.last_used_at,
                strength=f.strength,
                verified=f.verified,
                aliases=f.aliases or [],
            )
            for f in fact_rows
        ],
    )


async def record_gaps(session: AsyncSession, candidate_id: str, skills: list[str]) -> None:
    """A skill a job wanted that the candidate has no evidence for.

    Counted rather than merely listed: the count is what turns a pile of misses
    into "this is the one thing costing you applications".
    """
    for skill in {s.strip().lower() for s in skills if s.strip()}:
        row = (
            await session.execute(
                select(Gap).where(Gap.candidate_id == candidate_id, Gap.skill == skill)
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(Gap(candidate_id=candidate_id, skill=skill))
        else:
            row.seen_in_job_count += 1
    await session.flush()


# ─── Companies and boards ───────────────────────────────────────────────────


async def upsert_company(session: AsyncSession, name: str, domain: str | None = None) -> Company:
    normalized = _norm(name)
    row = (
        await session.execute(select(Company).where(Company.normalized_name == normalized))
    ).scalar_one_or_none()
    if row is None:
        row = Company(name=name, normalized_name=normalized, domain=domain)
        session.add(row)
        await session.flush()
    elif domain and not row.domain:
        row.domain = domain
    return row


async def link_jobs_to_companies(session: AsyncSession, *, dry_run: bool = True) -> dict[str, int]:
    """Attach already-stored jobs to the company rows they refer to.

    The forward fix only helps jobs harvested from now on. This is for the corpus that was
    collected while the link was never written — and it stays, rather than being a one-off
    script, because a company researched *after* its jobs were harvested lands in exactly
    the same state: rows that should be joined and are not.

    Matches on the normalized name and nothing else. A fuzzy match here would attach
    somebody's people, research and outreach history to a different company with a similar
    name, and every answer built on that join would then be confidently wrong.
    """
    companies = {
        str(normalized): str(company_id)
        for company_id, normalized in (
            await session.execute(select(Company.id, Company.normalized_name))
        ).all()
    }
    rows = (await session.execute(select(Job).where(Job.company_id.is_(None)))).scalars().all()

    linked = 0
    for row in rows:
        company_id = companies.get(_norm(row.company or ""))
        if not company_id:
            continue
        linked += 1
        if not dry_run:
            row.company_id = company_id

    if not dry_run:
        await session.flush()

    return {"unlinked": len(rows), "linked": linked, "still_unlinked": len(rows) - linked}


async def upsert_ats_board(
    session: AsyncSession,
    provider: str,
    slug: str,
    company_id: str | None,
    discovered_via: str,
    job_count: int,
    ok: bool,
) -> ATSBoard:
    row = (
        await session.execute(
            select(ATSBoard).where(ATSBoard.provider == provider, ATSBoard.slug == slug)
        )
    ).scalar_one_or_none()
    if row is None:
        row = ATSBoard(provider=provider, slug=slug, company_id=company_id)
        session.add(row)

    row.discovered_via = discovered_via
    if company_id and not row.company_id:
        row.company_id = company_id
    if ok:
        row.last_ok_at = datetime.now(UTC)
        row.last_job_count = job_count
        row.consecutive_failures = 0
    else:
        row.consecutive_failures += 1
    await session.flush()
    return row


async def known_boards(session: AsyncSession) -> list[ATSBoard]:
    """Boards already resolved, so a repeat run skips discovery entirely."""
    return list(
        (
            await session.execute(
                select(ATSBoard)
                .where(ATSBoard.consecutive_failures < 5)
                .order_by(ATSBoard.last_job_count.desc())
            )
        )
        .scalars()
        .all()
    )


# ─── Jobs ───────────────────────────────────────────────────────────────────


async def _resolve_company_id(session: AsyncSession, name: str) -> str | None:
    """The company row this job's name refers to, if we already have one.

    Deliberately does not create one. `company` means "a company we know something about" —
    it is what the dashboard counts and what people, research and outreach hang off — and
    inserting a bare row for every name that appears in a search result would quietly
    redefine that number from 66 researched companies to several hundred strings.

    So this links where a link exists and leaves the rest NULL, which is the honest state:
    we have the job, and we do not yet have the company.
    """
    normalized = _norm(name or "")
    if not normalized:
        return None
    row = (
        await session.execute(select(Company.id).where(Company.normalized_name == normalized))
    ).scalar_one_or_none()
    return str(row) if row else None


async def upsert_jobs(
    session: AsyncSession, jobs: list[NormalizedJob], company_id: str | None = None
) -> tuple[int, int]:
    """Persist deduplicated jobs. Returns (inserted, updated).

    A job already present is refreshed rather than replaced: `first_seen_at` is
    how job age survives across runs, and overwriting it would make everything
    look newly posted every time the agent runs.
    """
    inserted = updated = 0
    # One lookup per distinct name, not one per job: a 700-row harvest off a single board
    # would otherwise issue 700 identical queries.
    resolved: dict[str, str | None] = {}

    for job in jobs:
        job_company_id = company_id
        if job_company_id is None and job.company:
            key = _norm(job.company)
            if key not in resolved:
                resolved[key] = await _resolve_company_id(session, job.company)
            job_company_id = resolved[key]

        row = await session.get(Job, job.job_id)
        if row is None:
            row = Job(
                id=job.job_id,
                company_id=job_company_id,
                company=job.company,
                company_domain=job.company_domain,
                title=job.title,
                normalized_title=job.normalized_title,
                location=job.location,
                work_mode=job.work_mode.value,
                employment_type=job.employment_type.value,
                seniority=job.seniority.value,
                salary=job.salary.model_dump(mode="json") if job.salary else None,
                description=job.description,
                requirements=job.requirements,
                preferred_skills=job.preferred_skills,
                education_requirements=job.education_requirements,
                min_years_experience=job.min_years_experience,
                max_years_experience=job.max_years_experience,
                posted_date=job.posted_date,
                first_seen_at=job.first_seen_at,
                last_seen_at=job.last_seen_at,
                status=job.status.value,
                ats=job.ats,
            )
            session.add(row)
            await session.flush()
            inserted += 1
        else:
            row.last_seen_at = max(row.last_seen_at, job.last_seen_at)
            if len(job.description) > len(row.description or ""):
                row.description = job.description
            if row.posted_date is None:
                row.posted_date = job.posted_date
            if job_company_id and not row.company_id:
                row.company_id = job_company_id
            updated += 1

        # Uniqueness on (source, source_job_id) is GLOBAL, not per job, so the
        # lookup has to be global too. Checking only this job's listings missed
        # the real case: a posting whose fingerprint shifts between runs — the
        # company name resolves differently, say — lands on a new job row while
        # its old listing still exists, and the insert fails the constraint.
        #
        # A listing belongs to exactly one logical job, so when dedup now places
        # it elsewhere, the existing row is re-pointed rather than duplicated.
        for listing in job.listings:
            existing = (
                await session.execute(
                    select(JobListing).where(
                        JobListing.source == listing.source,
                        JobListing.source_job_id == listing.source_job_id,
                    )
                )
            ).scalar_one_or_none()

            if existing is not None:
                if existing.job_id != row.id:
                    log.debug(
                        "listing_repointed",
                        source=listing.source,
                        source_job_id=listing.source_job_id,
                        old_job=existing.job_id,
                        new_job=row.id,
                    )
                    existing.job_id = row.id
                existing.url = listing.url or existing.url
                existing.application_url = listing.application_url or existing.application_url
                existing.scraped_at = listing.scraped_at
                existing.is_alive = True
                continue

            session.add(
                JobListing(
                    job_id=row.id,
                    source=listing.source,
                    source_job_id=listing.source_job_id,
                    url=listing.url,
                    application_url=listing.application_url,
                    ats=listing.ats,
                    scraped_at=listing.scraped_at,
                )
            )
            # Flush per listing so a collision surfaces against the row that
            # caused it rather than as an opaque failure of the whole batch.
            await session.flush()

        await session.flush()

    return inserted, updated


async def save_score(session: AsyncSession, candidate_id: str, score: JobScoreModel) -> JobScore:
    row = (
        await session.execute(
            select(JobScore).where(
                JobScore.job_id == score.job_id, JobScore.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = JobScore(job_id=score.job_id, candidate_id=candidate_id)
        session.add(row)

    row.deterministic = score.deterministic.model_dump(mode="json")
    row.semantic = score.semantic.model_dump(mode="json") if score.semantic else None
    row.llm = score.llm.model_dump(mode="json") if score.llm else None
    row.job_fit = score.job_fit
    row.company_fit = score.company_fit
    row.final_score = score.final_score
    row.priority = score.priority.value
    row.weights_version = score.weights_version
    await session.flush()
    return row


async def top_jobs(
    session: AsyncSession,
    candidate_id: str,
    limit: int = 25,
    min_score: float = 0.0,
    priority: str | None = None,
) -> list[tuple[Job, JobScore]]:
    stmt = (
        select(Job, JobScore)
        .join(JobScore, JobScore.job_id == Job.id)
        .where(JobScore.candidate_id == candidate_id, JobScore.final_score >= min_score)
        .order_by(JobScore.final_score.desc())
        .limit(limit)
    )
    if priority:
        stmt = stmt.where(JobScore.priority == priority)
    return list((await session.execute(stmt)).all())  # type: ignore[arg-type]


async def job_count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(Job))).scalar() or 0)


# ─── Applications ───────────────────────────────────────────────────────────


async def already_applied(session: AsyncSession, job_id: str, candidate_id: str) -> bool:
    """The duplicate-application guard, checked before work begins.

    The unique constraint is the real enforcement; this exists so the agent can
    skip cheaply instead of doing a full tailoring run and then failing on insert.
    """
    row = (
        await session.execute(
            select(Application).where(
                Application.job_id == job_id, Application.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def create_application(
    session: AsyncSession,
    job_id: str,
    candidate_id: str,
    strategy: str = "ats_direct",
    **fields: Any,
) -> Application | None:
    """Returns None when an application already exists for this job."""
    if await already_applied(session, job_id, candidate_id):
        log.info("application_skipped_duplicate", job_id=job_id)
        return None

    row = Application(
        job_id=job_id,
        candidate_id=candidate_id,
        strategy=strategy,
        status=ApplicationStatus.SHORTLISTED,
        **fields,
    )
    session.add(row)
    await session.flush()
    session.add(
        ApplicationEvent(application_id=row.id, type="created", payload={"strategy": strategy})
    )
    await session.flush()
    return row


async def add_application_event(
    session: AsyncSession, application_id: str, type_: str, **payload: Any
) -> None:
    session.add(ApplicationEvent(application_id=application_id, type=type_, payload=payload))
    await session.flush()


# ─── Runs and events ────────────────────────────────────────────────────────


async def start_run(session: AsyncSession, kind: str, config: dict | None = None) -> AgentRun:
    row = AgentRun(kind=kind, search_config=config or {})
    session.add(row)
    await session.flush()
    return row


async def finish_run(
    session: AsyncSession, run: AgentRun, stats: dict, status: str = "completed"
) -> None:
    run.status = status
    run.stats = stats
    run.ended_at = datetime.now(UTC)
    await session.flush()


async def record_event(session: AsyncSession, event: Any) -> None:
    session.add(
        AgentEventRow(
            run_id=str(event.run_id) if event.run_id else None,
            type=str(event.type),
            level=str(event.level),
            message=event.message,
            job_id=event.job_id,
            application_id=event.application_id,
            payload=event.payload,
            at=event.at,
        )
    )


async def recent_events(session: AsyncSession, limit: int = 100) -> list[AgentEventRow]:
    return list(
        (
            await session.execute(
                select(AgentEventRow).order_by(AgentEventRow.at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )


# ─── Application queries ────────────────────────────────────────────────────


async def get_application(session: AsyncSession, application_id: str) -> Application | None:
    return await session.get(Application, application_id)


async def application_for_job(
    session: AsyncSession, job_id: str, candidate_id: str
) -> Application | None:
    return (
        await session.execute(
            select(Application).where(
                Application.job_id == job_id, Application.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()


async def list_applications(
    session: AsyncSession,
    candidate_id: str,
    *,
    statuses: list[str] | None = None,
    limit: int = 200,
) -> list[tuple[Application, Job]]:
    """Applications with their jobs, newest first.

    Joined rather than lazy-loaded: every caller needs the job title, and N+1
    lookups behind a Kanban board is the kind of slowness that gets blamed on the
    agent rather than on the query.
    """
    stmt = (
        select(Application, Job)
        .join(Job, Job.id == Application.job_id)
        .where(Application.candidate_id == candidate_id)
        .order_by(Application.updated_at.desc())
        .limit(limit)
    )
    if statuses:
        stmt = stmt.where(Application.status.in_(statuses))
    return list((await session.execute(stmt)).all())  # type: ignore[arg-type]


async def application_events(session: AsyncSession, application_id: str) -> list[ApplicationEvent]:
    return list(
        (
            await session.execute(
                select(ApplicationEvent)
                .where(ApplicationEvent.application_id == application_id)
                .order_by(ApplicationEvent.at.asc())
            )
        )
        .scalars()
        .all()
    )


async def applications_submitted_today(session: AsyncSession, candidate_id: str) -> int:
    """Counts against the daily cap.

    Counts SUBMISSION_UNVERIFIED as well as APPLIED: an unverified submission may
    well have gone through, and the cap exists to stop a runaway loop, so it must
    err towards counting.
    """
    from datetime import timedelta

    since = datetime.now(UTC) - timedelta(hours=24)
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Application)
                .where(
                    Application.candidate_id == candidate_id,
                    Application.submitted_at.is_not(None),
                    Application.submitted_at >= since,
                )
            )
        ).scalar()
        or 0
    )


async def update_application(
    session: AsyncSession, application: Application, **fields: Any
) -> Application:
    for key, value in fields.items():
        setattr(application, key, value)
    await session.flush()
    return application


# ─── Resume renders and cover letters ───────────────────────────────────────


async def save_resume_render(
    session: AsyncSession,
    candidate_id: str,
    job_id: str | None,
    archetype: str,
    content: dict,
    fact_ids: list[str],
    validation: dict,
    storage_key: str | None = None,
) -> ResumeRender:
    row = ResumeRender(
        candidate_id=candidate_id,
        job_id=job_id,
        archetype=archetype,
        content=content,
        fact_ids=fact_ids,
        validation=validation,
        storage_key=storage_key,
        ats_score=validation.get("ats_score"),
        evidence_integrity=validation.get("evidence_integrity"),
        keyword_match=validation.get("keyword_match"),
    )
    session.add(row)
    await session.flush()
    return row


async def get_resume_render(session: AsyncSession, render_id: str) -> ResumeRender | None:
    return await session.get(ResumeRender, render_id)


async def latest_render_for_job(
    session: AsyncSession, candidate_id: str, job_id: str
) -> ResumeRender | None:
    return (
        await session.execute(
            select(ResumeRender)
            .where(ResumeRender.candidate_id == candidate_id, ResumeRender.job_id == job_id)
            .order_by(ResumeRender.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_renders(
    session: AsyncSession, candidate_id: str, limit: int = 50
) -> list[ResumeRender]:
    return list(
        (
            await session.execute(
                select(ResumeRender)
                .where(ResumeRender.candidate_id == candidate_id)
                .order_by(ResumeRender.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def active_master_resume(session: AsyncSession, candidate_id: str) -> ResumeMaster | None:
    return (
        await session.execute(
            select(ResumeMaster)
            .where(ResumeMaster.candidate_id == candidate_id, ResumeMaster.is_active.is_(True))
            .order_by(ResumeMaster.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def save_cover_letter(
    session: AsyncSession,
    candidate_id: str,
    job_id: str | None,
    body: str,
    fact_ids: list[str],
    validation: dict,
    model: str | None = None,
) -> CoverLetter:
    row = CoverLetter(
        candidate_id=candidate_id,
        job_id=job_id,
        body=body,
        fact_ids=fact_ids,
        validation=validation,
        model=model,
    )
    session.add(row)
    await session.flush()
    return row


async def get_cover_letter(session: AsyncSession, letter_id: str) -> CoverLetter | None:
    return await session.get(CoverLetter, letter_id)


# ─── Answers and question memory ────────────────────────────────────────────


async def save_answers(session: AsyncSession, application_id: str, answers: list[Any]) -> int:
    """Replace this application's stored answers.

    Replaced rather than appended: the answer set belongs to the current
    preparation, and keeping stale ones around invites filling a form from a
    superseded draft.
    """
    existing = (
        (
            await session.execute(
                select(ApplicationAnswer).where(ApplicationAnswer.application_id == application_id)
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await session.delete(row)

    for item in answers:
        session.add(
            ApplicationAnswer(
                application_id=application_id,
                canonical_field=item.canonical_field[:120],
                question_text=item.question,
                answer=item.text,
                fact_ids=list(item.fact_ids),
                confidence=item.confidence,
                source=item.source[:20],
            )
        )
    await session.flush()
    return len(answers)


async def application_answers(
    session: AsyncSession, application_id: str
) -> list[ApplicationAnswer]:
    return list(
        (
            await session.execute(
                select(ApplicationAnswer).where(ApplicationAnswer.application_id == application_id)
            )
        )
        .scalars()
        .all()
    )


async def question_memory(session: AsyncSession, candidate_id: str) -> dict[str, str]:
    """Previously given answers, keyed by normalized question.

    Entries flagged `requires_recheck` are withheld: the reuse path cannot verify a
    changed circumstance, so a flagged answer goes back to the user instead.
    """
    rows = (
        (
            await session.execute(
                select(QuestionMemory).where(
                    QuestionMemory.candidate_id == candidate_id,
                    QuestionMemory.requires_recheck.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.question_norm: r.answer for r in rows}


async def remember_answer(
    session: AsyncSession,
    candidate_id: str,
    canonical_field: str,
    question_norm: str,
    answer: str,
) -> QuestionMemory:
    row = (
        await session.execute(
            select(QuestionMemory).where(
                QuestionMemory.candidate_id == candidate_id,
                QuestionMemory.question_norm == question_norm,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = QuestionMemory(
            candidate_id=candidate_id,
            canonical_field=canonical_field[:120],
            question_norm=question_norm[:500],
            answer=answer,
        )
        session.add(row)
    else:
        row.answer = answer
        row.requires_recheck = False
    row.times_used += 1
    row.last_used_at = datetime.now(UTC)
    await session.flush()
    return row


# ─── Companies ──────────────────────────────────────────────────────────────


async def get_company(session: AsyncSession, company_id: str) -> Company | None:
    return await session.get(Company, company_id)


async def company_by_name(session: AsyncSession, name: str) -> Company | None:
    return (
        await session.execute(select(Company).where(Company.normalized_name == _norm(name)))
    ).scalar_one_or_none()


async def save_company_profile(session: AsyncSession, company: Company, profile: Any) -> Company:
    """Persist a research result. Only overwrites fields the research resolved."""
    company.website = profile.website or company.website
    company.domain = profile.domain or company.domain
    company.description = profile.description or company.description
    company.industry = profile.industry or company.industry
    company.size_band = profile.size_band or company.size_band
    company.hq_location = profile.hq_location or company.hq_location
    if profile.tech_focus:
        company.tech_focus = list(profile.tech_focus)
    if profile.signals:
        company.signals = {**(company.signals or {}), **profile.signals}
    if profile.ai_relevance is not None:
        company.ai_relevance = profile.ai_relevance
    if profile.fit_score is not None:
        company.fit_score = profile.fit_score
    company.researched_at = profile.researched_at
    await session.flush()
    return company


async def companies_needing_research(session: AsyncSession, limit: int = 20) -> list[Company]:
    return list(
        (
            await session.execute(
                select(Company)
                .where(Company.researched_at.is_(None), Company.domain.is_not(None))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def company_fit_map(session: AsyncSession) -> dict[str, float]:
    """Normalized company name → fit score, for the ranking stage."""
    rows = (
        (await session.execute(select(Company).where(Company.fit_score.is_not(None))))
        .scalars()
        .all()
    )
    return {c.normalized_name: float(c.fit_score) for c in rows if c.fit_score is not None}


# ─── Ranking weights ────────────────────────────────────────────────────────


async def active_weights(session: AsyncSession, candidate_id: str) -> RankingWeights | None:
    return (
        await session.execute(
            select(RankingWeights)
            .where(RankingWeights.candidate_id == candidate_id, RankingWeights.active.is_(True))
            .order_by(RankingWeights.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def save_weights(
    session: AsyncSession,
    candidate_id: str,
    weights: dict,
    rationale: str = "",
    derived_from_run_id: str | None = None,
) -> RankingWeights:
    """Store a new active weight version, deactivating the previous one.

    Versioned rather than updated in place: a score records which version produced
    it, so rewriting a version would make old scores unexplainable.
    """
    current = await active_weights(session, candidate_id)
    version = (current.version + 1) if current else 1
    if current is not None:
        current.active = False
    row = RankingWeights(
        candidate_id=candidate_id,
        version=version,
        weights=weights,
        active=True,
        rationale=rationale,
        derived_from_run_id=derived_from_run_id,
    )
    session.add(row)
    await session.flush()
    return row


# ─── LLM accounting ─────────────────────────────────────────────────────────


async def record_llm_calls(
    session: AsyncSession, calls: list[Any], run_id: str | None = None
) -> int:
    """Persist the router's call log.

    Cost per application is the number that tells you whether the funnel is tuned
    correctly, and it can only be computed from real recorded calls — so these rows
    are written even for cache hits, which are the cheap half of the story.
    """
    for call in calls:
        session.add(
            LLMCall(
                run_id=run_id,
                agent=getattr(call, "agent", "unknown"),
                provider=str(call.model).split("/", 1)[0],
                model=call.model,
                prompt_tokens=call.prompt_tokens,
                completion_tokens=call.completion_tokens,
                cost_usd=call.cost_usd,
                cache_hit=call.cache_hit,
                latency_ms=call.latency_ms,
            )
        )
    if calls:
        await session.flush()
    return len(calls)
