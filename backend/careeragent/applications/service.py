"""The application pipeline: shortlist → prepare → approve → submit → track.

This is where every other module is spent, and it is the only place in the system
that can cause an irreversible external action. So the guards are here, in order,
and each one is checked before work begins rather than after:

  1. **Duplicate guard.** Enforced by a unique constraint on (job, candidate) and
     checked up front so a full tailoring run is not wasted on a job already applied to.
  2. **Daily cap.** Counted from submissions in the last 24 hours, including
     unverified ones — a runaway loop must be stopped by the cap, so the cap counts
     generously.
  3. **Autonomy level.** Level ≤2 never opens a browser. Level 5 is the only level
     that can submit without a human, and it additionally requires an allow-listed
     ATS, an empty pre-submit verdict, no unanswered required field, and a score
     above the threshold.
  4. **Evidence for APPLIED.** The state machine refuses APPLIED without it.

Nothing here marks an application as submitted on the strength of a click.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import audit, check_freshness, prescribe
from ..agents import verify as verify_application
from ..agents.recovery import target_status
from ..agents.verification import blocks_submission
from ..browser.catalog import is_freeform
from ..browser.runner import BrowserRunner, Outcome
from ..config import get_settings
from ..events import EventType, Level, get_bus
from ..logging import get_logger
from ..models.candidate import Candidate
from ..models.job import NormalizedJob
from ..prompts import get as get_prompt
from ..storage import repository as repo
from ..storage.models import Application, Job, ResumeRender
from ..storage.models import ApplicationStatus as S
from . import answers as qa
from .sessions import LiveSession, get_registry
from .states import IllegalTransition, transition

log = get_logger("applications.service")

FOLLOW_UP_DAYS = 10


class ApplicationError(RuntimeError):
    """A refusal the user needs to see, not a bug. Carries a readable reason."""


@dataclass
class PrepareResult:
    application_id: str
    status: str
    ats: str = "generic"
    url: str = ""
    resume_render_id: str | None = None
    cover_letter_id: str | None = None
    filled: list[dict[str, Any]] = field(default_factory=list)
    needs_user: list[str] = field(default_factory=list)
    never_filled: list[str] = field(default_factory=list)
    holds: list[str] = field(default_factory=list)
    answers: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    blocker: str = ""
    validation: dict[str, Any] = field(default_factory=dict)
    session_held: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    # Why we believed the posting was still open, and what the cross-document check
    # found. Both travel with the result so the approval screen can show them.
    freshness: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "status": self.status,
            "ats": self.ats,
            "url": self.url,
            "resume_render_id": self.resume_render_id,
            "cover_letter_id": self.cover_letter_id,
            "filled": list(self.filled),
            "needs_user": list(self.needs_user),
            "never_filled": list(self.never_filled),
            "holds": list(self.holds),
            "answers": list(self.answers),
            "screenshots": list(self.screenshots),
            "blocker": self.blocker,
            "validation": dict(self.validation),
            "session_held": self.session_held,
            "evidence": dict(self.evidence),
            "freshness": dict(self.freshness),
            "verification": dict(self.verification),
            "recovery": dict(self.recovery),
            "error": self.error,
        }


# ─── Helpers ────────────────────────────────────────────────────────────────


def _job_to_normalized(job: Job) -> NormalizedJob:
    """The ORM row as the in-memory model the intelligence layer speaks.

    One conversion in one place: the alternative is every consumer reaching into
    ORM attributes and the two representations drifting.
    """
    from ..models.job import EmploymentType, Seniority, WorkMode
    from ..models.job import JobListing as ListingModel

    return NormalizedJob(
        job_id=job.id,
        company=job.company,
        company_domain=job.company_domain,
        title=job.title,
        normalized_title=job.normalized_title,
        location=job.location or "",
        work_mode=WorkMode(job.work_mode),
        employment_type=EmploymentType(job.employment_type),
        seniority=Seniority(job.seniority),
        description=job.description or "",
        requirements=list(job.requirements or []),
        preferred_skills=list(job.preferred_skills or []),
        education_requirements=list(job.education_requirements or []),
        min_years_experience=job.min_years_experience,
        max_years_experience=job.max_years_experience,
        posted_date=job.posted_date,
        ats=job.ats,
        listings=[
            ListingModel(
                source=listing.source,
                source_job_id=listing.source_job_id,
                url=listing.url,
                application_url=listing.application_url,
                ats=listing.ats,
            )
            for listing in (job.listings or [])
        ],
    )


def _alternate_route(job: Job, tried: str) -> str:
    """Another door at the same employer, when the one we tried could not be read.

    A job seen on three boards has three application URLs; if the aggregator's copy is
    an unreadable JavaScript shell, the company's own ATS listing very often is not.
    """
    for listing in job.listings or []:
        candidate_url = listing.application_url or listing.url or ""
        if candidate_url and candidate_url != tried:
            return candidate_url
    return ""


def _application_url(job: Job) -> str:
    """Where to apply. Prefers a direct ATS application URL over a portal repost —
    better conversion, and it avoids automating a platform that forbids it."""
    for listing in job.listings or []:
        if listing.ats and listing.application_url:
            return listing.application_url
    for listing in job.listings or []:
        if listing.application_url:
            return listing.application_url
    for listing in job.listings or []:
        if listing.url:
            return listing.url
    return ""


async def _emit(type_: EventType, message: str, application_id: str, **payload: Any) -> None:
    await get_bus().emit_one(type_, message, application_id=application_id, **payload)


async def _record_llm(session: AsyncSession) -> None:
    """Persist whatever the router has spent since the last drain.

    Called at every boundary that can spend. Without it the cost page understates
    everything except discovery, which is the cheap half.
    """
    from ..llm.router import get_router

    try:
        await repo.record_llm_calls(session, get_router().drain_calls())
    except Exception as exc:  # accounting must never fail the work it accounts for
        log.debug("llm_accounting_failed", error=str(exc)[:120])


async def _move(
    session: AsyncSession,
    application: Application,
    target: S,
    *,
    evidence: dict[str, Any] | None = None,
    reason: str = "",
) -> None:
    """Apply a state transition and record it. Raises on an illegal move."""
    result = transition(
        application.status,
        target,
        evidence=evidence,
        attempts=application.attempts,
        reason=reason,
    )
    application.status = result.status
    if evidence:
        application.evidence = {**(application.evidence or {}), **evidence}
    await repo.add_application_event(session, application.id, result.event_type, **result.payload)
    await session.flush()


# ─── Shortlist ──────────────────────────────────────────────────────────────


async def shortlist(
    session: AsyncSession,
    job_id: str,
    candidate_id: str,
    *,
    strategy: str = "ats_direct",
) -> Application:
    """Create the application record. Idempotent; never creates a duplicate."""
    job = await session.get(Job, job_id)
    if job is None:
        raise ApplicationError(f"job {job_id} not found")

    if existing := await repo.application_for_job(session, job_id, candidate_id):
        return existing

    # Queried rather than reached through `job.scores`: that relationship is lazy, and
    # touching it here raises MissingGreenlet under the async session.
    from sqlalchemy import select

    from ..storage.models import JobScore

    score = (
        await session.execute(
            select(JobScore).where(JobScore.job_id == job_id, JobScore.candidate_id == candidate_id)
        )
    ).scalar_one_or_none()

    application = await repo.create_application(
        session,
        job_id,
        candidate_id,
        strategy=strategy,
        ats=job.ats,
        application_url=_application_url(job) or None,
        listing_id=(job.listings[0].id if job.listings else None),
        match_score=score.job_fit if score else None,
        company_score=score.company_fit if score else None,
        final_score=score.final_score if score else None,
    )
    if application is None:  # lost a race against another request
        existing = await repo.application_for_job(session, job_id, candidate_id)
        if existing is None:
            raise ApplicationError("could not create the application")
        return existing

    await _emit(
        EventType.JOB_SHORTLISTED,
        f"Shortlisted {job.title} at {job.company}",
        application.id,
        job_id=job_id,
    )
    return application


# ─── Tailoring ──────────────────────────────────────────────────────────────


async def _reusable_render(
    session: AsyncSession,
    application: Application,
    *,
    archetype: str | None = None,
) -> ResumeRender | None:
    """The most recent render for this job, if it can actually be attached to a form.

    Four conditions, each of which has to hold for reuse to be safe rather than merely
    cheap:

      * it passed validation — an unevidenced document must never be silently reused,
        because the whole point of rejecting it was that it cannot be sent;
      * the bytes are still there — the row outlives the file when `data/` is cleared;
      * the archetype matches what was asked for, when something was asked for;
      * it is for this job. A render for a different opening is a different document.

    The second condition was `Path(key).exists()`, which was true only while
    `storage_key` held an absolute path. Once it became an object key that check went
    permanently false, reuse stopped happening, and every application quietly paid for a
    second strong-tier LLM rewrite and a second PDF. Nothing failed — it just cost twice
    as much, which is the kind of regression that survives for months.
    """
    from ..storage.objects import get_store

    render = await repo.latest_render_for_job(session, application.candidate_id, application.job_id)
    if render is None:
        return None
    if (render.validation or {}).get("violations"):
        return None
    key = str(render.storage_key or "")
    if not key.endswith(".pdf"):
        return None
    if await get_store().materialise(key) is None:
        return None
    if archetype and render.archetype != archetype:
        return None
    return render


async def tailor_for(
    session: AsyncSession,
    application: Application,
    candidate: Candidate,
    *,
    archetype: str | None = None,
    use_llm: bool = True,
    render_pdf: bool = True,
    force: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    """Tailor, validate, render and store a resume for this application.

    Returns (render_id, validation). A document with evidence violations is stored
    — so the user can see what is wrong — but no PDF is written for it, which means
    it cannot be attached to anything.

    A usable render for this job is reused rather than rebuilt. That is not a cache for
    speed: `prepare` calls this, and so does the graph's tailor step before it, so
    without reuse every application through the graph paid for two strong-tier LLM
    rewrites and wrote two PDFs of the same document. `force=True` is for when the user
    deliberately asks again — a different archetype, or a retry after editing the Fact
    Bank.
    """
    if not force and (
        existing := await _reusable_render(session, application, archetype=archetype)
    ):
        application.resume_render_id = existing.id
        if application.status in {S.DISCOVERED, S.SHORTLISTED}:
            await _move(session, application, S.RESUME_TAILORED)
        await session.flush()
        log.debug("resume_render_reused", render_id=existing.id, job_id=application.job_id)
        return existing.id, dict(existing.validation or {})

    # Imported as a function, not as a module: `resume/__init__` exports `tailor` the
    # function, so `from ..resume import tailor` gives the callable and reaching for
    # `tailor.tailor` fails. Importing what we actually call avoids the ambiguity.
    from ..resume import render as render_mod
    from ..resume.tailor import tailor as tailor_resume

    job = await session.get(Job, application.job_id)
    if job is None:
        raise ApplicationError("job not found")
    normalized = _job_to_normalized(job)

    await _emit(
        EventType.RESUME_TAILORING_STARTED,
        f"Tailoring your resume for {job.title}",
        application.id,
        job_id=job.id,
    )

    result = tailor_resume(candidate, normalized, archetype=archetype, use_llm=use_llm)
    validation = result.report.to_dict()

    storage_key: str | None = None
    if result.report.ok and render_pdf:
        # Rendered to a scratch directory, then handed to the object store, which decides
        # where the bytes actually live. The renderer drives Chromium and needs a real
        # path, so the file has to exist somewhere first — but what gets recorded is a
        # key, not `D:\...\renders\x.pdf`. An absolute path in a database column pins
        # that database to one machine.
        import tempfile

        from ..storage.objects import get_store

        store = get_store()
        with tempfile.TemporaryDirectory(prefix="careeragent-render-") as scratch:
            target_dir = Path(scratch)
            try:
                written = await render_mod.render_pdf(result.resume, result.report, target_dir)
            except Exception as exc:
                # Chromium missing is the usual cause. HTML is still reviewable and
                # still attachable by hand, so the failure is recorded, not fatal.
                log.warning("pdf_render_failed", error=f"{type(exc).__name__}: {exc}"[:160])
                written = render_mod.write_html(result.resume, target_dir)
                validation["warnings"] = [
                    *validation.get("warnings", []),
                    f"PDF render failed ({type(exc).__name__}); HTML written instead",
                ]

            written = Path(written)
            key = f"renders/{uuid4().hex}{written.suffix}"
            await store.put_file(
                key,
                written,
                "application/pdf" if written.suffix == ".pdf" else "text/html",
            )
            storage_key = key

    row = await repo.save_resume_render(
        session,
        application.candidate_id,
        application.job_id,
        result.resume.archetype,
        result.resume.to_dict(),
        result.resume.all_fact_ids(),
        validation,
        storage_key,
    )
    application.resume_render_id = row.id
    if application.status in {S.DISCOVERED, S.SHORTLISTED}:
        await _move(session, application, S.RESUME_TAILORED)
    await session.flush()
    await _record_llm(session)

    await audit.record(
        session,
        audit.Decision(
            agent="resume_optimization",
            task=f"tailor a resume for {job.title}",
            subject_type="job",
            subject_id=job.id,
            decision=f"{result.resume.archetype} ({result.resume.generated_by})",
            confidence=result.report.evidence_integrity,
            evidence={
                "ats_score": result.report.ats_score,
                "evidence_integrity": result.report.evidence_integrity,
                "keyword_match": result.report.keyword_match,
                "violations": len(result.report.violations),
                "fact_ids": result.resume.all_fact_ids()[:20],
            },
            tools_used=["fact_bank", "evidence_validator"]
            + (["llm"] if result.resume.generated_by == "llm" else []),
            inputs={"archetype": archetype or "auto", "use_llm": use_llm},
            # The fallback reason is the single most useful thing here: it is where a
            # rejected model rewrite gets explained.
            outcome=result.fallback_reason or "accepted",
            model=result.resume.model or None,
            # Only when a model was actually involved. Stamping a prompt version on a
            # deterministic document would claim a provenance it does not have.
            prompt_version=(
                get_prompt("tailor_resume").version if result.resume.generated_by == "llm" else None
            ),
        ),
        candidate_id=application.candidate_id,
    )

    await _emit(
        EventType.RESUME_GENERATED if result.report.ok else EventType.EVIDENCE_REJECTED,
        (
            f"Resume tailored — ATS {result.report.ats_score:.0%}, "
            f"evidence {result.report.evidence_integrity:.0%}, "
            f"keywords {result.report.keyword_match:.0%}"
        )
        if result.report.ok
        else f"Resume held back: {len(result.report.violations)} evidence violation(s)",
        application.id,
        job_id=job.id,
        render_id=row.id,
        archetype=result.resume.archetype,
        fell_back=result.fell_back,
    )
    return (row.id, validation)


async def cover_letter_for(
    session: AsyncSession,
    application: Application,
    candidate: Candidate,
) -> str | None:
    """Draft a cover letter using only stored company research. None on refusal."""
    from . import cover_letter as cl

    job = await session.get(Job, application.job_id)
    if job is None:
        return None

    notes = ""
    if job.company_id and (company := await repo.get_company(session, job.company_id)):
        bits = [company.description or ""]
        if company.industry:
            bits.append(f"Industry: {company.industry}.")
        if company.tech_focus:
            bits.append(f"Technology on their site: {', '.join(company.tech_focus[:10])}.")
        notes = " ".join(b for b in bits if b)[:900]

    draft = cl.generate(candidate, _job_to_normalized(job), company_notes=notes)
    if not draft.ok:
        log.info("cover_letter_skipped", job_id=job.id, reason=draft.reason)
        return None

    row = await repo.save_cover_letter(
        session,
        application.candidate_id,
        application.job_id,
        draft.with_greeting(job.company),
        draft.fact_ids,
        draft.to_dict(),
        draft.model,
    )
    application.cover_letter_id = row.id
    await session.flush()
    await _record_llm(session)
    return row.id


# ─── Prepare ────────────────────────────────────────────────────────────────


async def prepare(
    session: AsyncSession,
    application_id: str,
    candidate: Candidate,
    *,
    use_llm: bool = True,
    headless: bool = True,
    write_cover_letter: bool = False,
    autonomy_level: int | None = None,
    check_freshness_first: bool = True,
) -> PrepareResult:
    """Tailor, open the form, answer what can be answered, stop.

    This never submits. It ends in one of four states, all of which are honest
    about what happened:

        AWAITING_APPROVAL   filled and clean; a human decides
        HUMAN_REQUIRED      CAPTCHA, MFA, login, or a field only you can answer
        READY               filled, but the pre-submit verdict is holding it
        FAILED              the form could not be read at all
    """
    settings = get_settings()
    level = settings.autonomy_level if autonomy_level is None else autonomy_level

    application = await repo.get_application(session, application_id)
    if application is None:
        raise ApplicationError(f"application {application_id} not found")
    if application.status in {S.APPLIED, S.SUBMISSION_UNVERIFIED}:
        raise ApplicationError(
            f"this application is already {application.status}; preparing it again "
            "would risk a duplicate submission"
        )
    if level <= 2:
        raise ApplicationError(
            f"autonomy level {level} does not permit opening a browser "
            "(level 3 fills forms, level 4 queues them for your approval)"
        )

    job = await session.get(Job, application.job_id)
    if job is None:
        raise ApplicationError("job not found")

    result = PrepareResult(application_id=application_id, status=application.status)

    # ─── Is it still open? ──────────────────────────────────────────────────
    # First, because everything below costs something: tailoring is a strong-tier LLM
    # call and the browser is 400MB of Chromium. A posting that closed three weeks ago
    # should cost one HTTP request to discover, not both of those.
    check_url = application.application_url or _application_url(job)
    if check_url and check_freshness_first:
        freshness = await check_freshness(check_url, posted=job.posted_date)
        result.freshness = freshness.to_dict()
        await audit.record(
            session,
            audit.Decision(
                agent="freshness",
                task="is this posting still open?",
                subject_type="job",
                subject_id=job.id,
                decision=freshness.state.value,
                confidence=1.0 if freshness.state.value != "unknown" else 0.3,
                evidence={
                    "status_code": freshness.status_code,
                    "age_days": freshness.age_days,
                    "marker": freshness.evidence,
                },
                tools_used=["http_get"],
                inputs={"url": check_url[:200]},
                outcome=freshness.reason,
            ),
            candidate_id=application.candidate_id,
        )
        if not freshness.safe_to_apply:
            application.last_error = freshness.reason
            # REJECTED, not FAILED_TERMINAL. A closed requisition is not a failure of
            # this system — nothing broke, the market moved — and REJECTED is the one
            # state legal from every point `prepare` can be called from.
            await _move(session, application, S.REJECTED, reason=freshness.reason)
            result.status = application.status
            result.error = f"this job is no longer open — {freshness.reason}"
            await _emit(
                EventType.JOB_REJECTED,
                f"Skipped {job.title} at {job.company}: {freshness.reason}",
                application_id,
                job_id=job.id,
                level=Level.WARN,
            )
            return result

    # ─── Resume ─────────────────────────────────────────────────────────────
    render_id, validation = await tailor_for(session, application, candidate, use_llm=use_llm)
    result.resume_render_id = render_id
    result.validation = validation

    # A form upload needs a real file, and `storage_key` is now a key rather than a
    # path. `materialise` resolves either — including the absolute paths recorded before
    # the object store existed, which are still in databases and still valid.
    from ..storage.objects import get_store

    store = get_store()

    resume_path: str | None = None
    render_row = await repo.get_resume_render(session, render_id) if render_id else None
    if render_row and str(render_row.storage_key or "").endswith(".pdf"):
        local = await store.materialise(str(render_row.storage_key))
        resume_path = str(local) if local else None
    if resume_path is None and (
        # Fall back to the master resume: attaching the document the candidate
        # wrote is always better than attaching nothing.
        master := await repo.active_master_resume(session, application.candidate_id)
    ):
        local = await store.materialise(str(master.storage_key or ""))
        resume_path = str(local) if local else None
        result.validation["warnings"] = [
            *result.validation.get("warnings", []),
            "no tailored PDF available; attaching your master resume",
        ]

    if write_cover_letter and use_llm:
        result.cover_letter_id = await cover_letter_for(session, application, candidate)

    cover_text = ""
    if result.cover_letter_id and (
        letter := await repo.get_cover_letter(session, result.cover_letter_id)
    ):
        cover_text = letter.body

    url = application.application_url or _application_url(job)
    if not url:
        await _move(session, application, S.FAILED, reason="no application URL")
        result.status = application.status
        result.error = "no application URL on any listing for this job"
        return result
    result.url = url

    # ─── Browser ────────────────────────────────────────────────────────────
    await _emit(
        EventType.APPLICATION_STARTED,
        f"Opening the application for {job.title} at {job.company}",
        application_id,
        job_id=job.id,
        url=url,
    )

    normalized = _job_to_normalized(job)
    memory = await repo.question_memory(session, application.candidate_id)

    runner = BrowserRunner(headless=headless)
    await runner.__aenter__()
    keep = False
    try:
        run = await runner.apply(
            url,
            candidate,
            resume_path=resume_path,
            autonomy_level=min(level, 4),  # prepare never submits, whatever the level
            auto_submit=False,
            cover_letter=cover_text or None,
            keep_open=True,
        )
        result.ats = run.ats
        result.screenshots = list(run.screenshots)
        result.blocker = run.blocker

        if run.outcome is Outcome.HUMAN_REQUIRED:
            await _move(session, application, S.HUMAN_REQUIRED, reason=run.blocker)
            application.last_error = run.error
            result.status = application.status
            result.error = run.error
            await _emit(
                EventType.HUMAN_APPROVAL_REQUIRED,
                f"{run.blocker or 'A challenge'} — handing this one to you",
                application_id,
                job_id=job.id,
                blocker=run.blocker,
            )
            return result

        if run.outcome is Outcome.FAILED or run.spec is None:
            application.attempts += 1
            application.last_error = run.error or "the form could not be read"
            # Classify before deciding. A timeout, a closed posting and an unreadable
            # JavaScript page are three different failures that used to share one
            # counter and one outcome.
            prescription = prescribe(
                application.last_error,
                attempt=application.attempts,
                blocker=run.blocker,
                alternate_route=_alternate_route(job, result.url),
            )
            result.recovery = prescription.to_dict()
            await audit.record(
                session,
                audit.Decision(
                    agent="recovery",
                    task="what should happen after this failure?",
                    subject_type="application",
                    subject_id=application_id,
                    decision=prescription.action.value,
                    confidence=0.9 if prescription.kind.value != "unknown" else 0.4,
                    evidence={
                        "kind": prescription.kind.value,
                        "attempt": prescription.attempt,
                        "blocker": run.blocker,
                        "delay_seconds": prescription.delay_seconds,
                    },
                    inputs={"error": application.last_error[:300]},
                    outcome=prescription.reason,
                ),
                candidate_id=application.candidate_id,
            )
            await _move(
                session,
                application,
                S(target_status(prescription)),
                reason=prescription.reason,
            )
            result.status = application.status
            result.error = prescription.user_message or application.last_error
            await _emit(
                EventType.APPLICATION_FAILED,
                f"{prescription.kind.value}: {prescription.user_message}",
                application_id,
                job_id=job.id,
                level=Level.ERROR,
                action=prescription.action.value,
            )
            return result

        await _emit(
            EventType.ATS_DETECTED,
            f"Detected {run.ats} ({run.confidence:.0%} confidence), {len(run.spec.fields)} fields",
            application_id,
            job_id=job.id,
            ats=run.ats,
            fields=len(run.spec.fields),
        )

        # ─── Open questions ─────────────────────────────────────────────────
        open_questions = [
            (f.label, f.canonical_field)
            for f in run.spec.fields
            if is_freeform(f.canonical_field) or f.canonical_field == "cover_letter.text"
        ]
        generated: list[qa.Answer] = []
        if open_questions:
            generated = qa.answer_all(
                open_questions,
                candidate,
                normalized,
                memory=memory,
                use_llm=use_llm,
            )
            await repo.save_answers(session, application_id, generated)
            result.answers = [a.to_dict() for a in generated]
            usable = {a.canonical_field: a.text for a in generated if a.usable}
            if usable:
                # Second pass on the same page: the questions could not be answered
                # before the form was read, and re-opening would lose the fill.
                page = runner.held_page
                if page is not None:
                    await runner.fill_form(
                        page,
                        run.spec,
                        candidate,
                        resume_path,
                        overrides=usable,
                        cover_letter=cover_text or None,
                    )
                    run.holds = await runner.verify(page, run.spec)
                    run.needs_user = [
                        item
                        for item in run.needs_user
                        if not any(item.startswith(label) for label, _ in open_questions)
                    ]
                    if shot := await runner.screenshot(page, f"answered-{run.ats}"):
                        result.screenshots.append(shot)
            for answer in generated:
                if answer.usable and answer.source == "generated":
                    await repo.remember_answer(
                        session,
                        application.candidate_id,
                        answer.canonical_field,
                        qa.normalize_question(answer.question),
                        answer.text,
                    )

        result.filled = [
            {
                "field": s.canonical_field,
                "label": s.label,
                "ok": s.ok,
                "detail": s.detail,
                "value": s.value,
            }
            for s in run.filled
        ]
        result.needs_user = list(run.needs_user)
        result.never_filled = list(run.never_filled)
        result.holds = [str(h) for h in run.holds]

        for item in result.never_filled:
            await _emit(
                EventType.FORM_FIELD_DETECTED,
                f"Left for you: {item}",
                application_id,
                job_id=job.id,
                field=item,
            )

        # ─── Where does it stop? ────────────────────────────────────────────
        application.preview = {
            "url": url,
            "ats": run.ats,
            "confidence": run.confidence,
            "filled": result.filled,
            "needs_user": result.needs_user,
            "never_filled": result.never_filled,
            "holds": result.holds,
            "answers": result.answers,
            "screenshots": result.screenshots,
            "validation": result.validation,
            "prepared_at": datetime.now(UTC).isoformat(),
        }

        if application.status not in {S.READY, S.AWAITING_APPROVAL}:
            await _move(session, application, S.READY)

        if result.needs_user or result.holds:
            target = S.HUMAN_REQUIRED if result.needs_user else S.READY
            if target is not application.status:
                await _move(
                    session,
                    application,
                    target,
                    reason=(
                        f"{len(result.needs_user)} field(s) need you"
                        if result.needs_user
                        else f"{len(result.holds)} pre-submit hold(s)"
                    ),
                )
            if result.holds:
                await _emit(
                    EventType.PRESUBMIT_HELD,
                    f"Held before submit: {result.holds[0]}",
                    application_id,
                    job_id=job.id,
                    holds=result.holds,
                    level=Level.WARN,
                )
        else:
            await _move(session, application, S.AWAITING_APPROVAL)
            await _emit(
                EventType.HUMAN_APPROVAL_REQUIRED,
                f"Ready for your approval: {job.title} at {job.company}",
                application_id,
                job_id=job.id,
                ats=run.ats,
                filled=len(result.filled),
            )

        keep = runner.held_page is not None
        if keep:
            await get_registry().put(
                LiveSession(
                    application_id=application_id,
                    runner=runner,
                    spec=run.spec,
                    url=url,
                    screenshots=result.screenshots,
                    headed=not headless,
                )
            )
        result.session_held = keep
        result.status = application.status
        await _record_llm(session)
        return result

    finally:
        if not keep:
            await runner.__aexit__(None, None, None)


# ─── Approve and submit ─────────────────────────────────────────────────────


async def approve(
    session: AsyncSession,
    application_id: str,
    candidate: Candidate,
    *,
    field_overrides: dict[str, str] | None = None,
    autonomy_level: int | None = None,
) -> PrepareResult:
    """Submit an application the user has approved.

    An approval authorises submitting *a form that passes the pre-submit verdict*.
    It does not authorise submitting regardless of it: the page is re-read and
    re-judged, and a hold still stops the click. That is the difference between a
    human in the loop and a human rubber-stamping a form they cannot see.
    """
    settings = get_settings()
    level = settings.autonomy_level if autonomy_level is None else autonomy_level

    application = await repo.get_application(session, application_id)
    if application is None:
        raise ApplicationError(f"application {application_id} not found")
    if application.status in {S.APPLIED, S.SUBMISSION_UNVERIFIED}:
        raise ApplicationError(f"already {application.status}; not submitting again")
    if level < 4:
        raise ApplicationError(
            f"autonomy level {level} does not permit submitting; level 4 submits on your approval"
        )

    submitted_today = await repo.applications_submitted_today(session, application.candidate_id)
    if submitted_today >= settings.daily_application_cap:
        raise ApplicationError(
            f"daily cap reached ({submitted_today}/{settings.daily_application_cap}). "
            "Raise CAREERAGENT_DAILY_APPLICATION_CAP or try tomorrow."
        )

    job = await session.get(Job, application.job_id)
    result = PrepareResult(
        application_id=application_id,
        status=application.status,
        url=application.application_url or "",
    )

    registry = get_registry()
    live = await registry.get(application_id)

    if live is None:
        # The reviewed session is gone. Re-preparing is correct but must be
        # visible: the form being submitted is not literally the one reviewed.
        await _emit(
            EventType.APPLICATION_STARTED,
            "The reviewed browser session expired — re-opening the form before submitting",
            application_id,
            job_id=application.job_id,
            level=Level.WARN,
        )
        prepared = await prepare(
            session, application_id, candidate, use_llm=False, autonomy_level=level
        )
        if prepared.status != S.AWAITING_APPROVAL:
            prepared.error = prepared.error or (
                "re-opened form is not in a submittable state; review it again"
            )
            return prepared
        live = await registry.get(application_id)
        if live is None:
            prepared.error = "could not hold a browser session to submit through"
            return prepared

    # ─── Does the form contradict the resume being attached to it? ──────────
    # The pre-submit verdict asks "is every required field ready?". This asks the
    # different question the spec's §67 cares about: "does the application disagree
    # with itself?" — a form claiming two years against a resume showing one.
    filled_values = {
        item.get("field", ""): item.get("value", "")
        for item in (application.preview or {}).get("filled", [])
        if item.get("field")
    }
    filled_values.update(field_overrides or {})

    if filled_values:
        tailored = None
        if application.resume_render_id:
            render = await repo.get_resume_render(session, application.resume_render_id)
            if render and render.content:
                from ..resume.documents import TailoredResume

                tailored = TailoredResume.from_dict(render.content)

        verification = verify_application(filled_values, candidate, tailored)
        result.verification = verification.to_dict()
        await audit.record(
            session,
            audit.Decision(
                agent="application_verification",
                task="does the form contradict the resume?",
                subject_type="application",
                subject_id=application_id,
                decision="blocked" if verification.blocked else "consistent",
                confidence=1.0,
                evidence={
                    "fields_checked": verification.checked,
                    "conflicts": [c.to_dict() for c in verification.conflicts],
                },
                tools_used=["fact_bank", "tailored_resume"],
                outcome=blocks_submission(verification) or "no contradictions found",
            ),
            candidate_id=application.candidate_id,
        )
        if verification.blocked:
            reason = blocks_submission(verification)
            await _move(session, application, S.AWAITING_APPROVAL, reason=reason)
            await _emit(
                EventType.PRESUBMIT_HELD,
                f"Not submitted — {reason}",
                application_id,
                job_id=application.job_id,
                level=Level.WARN,
                conflicts=[c.to_dict() for c in verification.blocking_conflicts],
            )
            result.status = application.status
            result.error = reason
            result.holds = [str(c) for c in verification.blocking_conflicts]
            return result

    await _move(session, application, S.APPLYING, reason="approved by user")
    await session.flush()

    outcome, evidence, holds = await live.runner.submit_held(
        live.spec, overrides=field_overrides or None
    )
    result.ats = live.spec.ats
    result.url = live.url
    result.holds = [str(h) for h in holds]
    result.screenshots = list(live.screenshots)

    page = live.runner.held_page
    if page is not None and (
        shot := await live.runner.screenshot(page, f"after-submit-{live.spec.ats}")
    ):
        result.screenshots.append(shot)

    await audit.record(
        session,
        audit.Decision(
            agent="submission_verification",
            task="did the application actually submit?",
            subject_type="application",
            subject_id=application_id,
            decision=outcome.value,
            confidence=1.0 if outcome is Outcome.SUBMITTED else 0.5,
            evidence=dict(evidence),
            tools_used=["browser"],
            outcome=(
                "confirmed by the page"
                if outcome is Outcome.SUBMITTED
                else str(evidence.get("reason", ""))
            ),
        ),
        candidate_id=application.candidate_id,
    )

    if outcome is Outcome.SUBMITTED:
        application.submitted_at = datetime.now(UTC)
        application.follow_up_date = date.today() + timedelta(days=FOLLOW_UP_DAYS)
        await _move(session, application, S.APPLIED, evidence=evidence)
        await _emit(
            EventType.APPLICATION_SUBMITTED,
            f"Submitted: {job.title if job else ''} at {job.company if job else ''}",
            application_id,
            job_id=application.job_id,
            evidence=evidence,
        )
    elif outcome is Outcome.UNVERIFIED:
        application.submitted_at = datetime.now(UTC)
        await _move(session, application, S.SUBMISSION_UNVERIFIED, evidence=evidence)
        await _emit(
            EventType.APPLICATION_UNVERIFIED,
            "Clicked submit but saw no confirmation — please check your email and confirm",
            application_id,
            job_id=application.job_id,
            level=Level.WARN,
            evidence=evidence,
        )
    elif outcome is Outcome.HUMAN_REQUIRED:
        await _move(session, application, S.HUMAN_REQUIRED, reason=str(evidence.get("blocker", "")))
        await _emit(
            EventType.HUMAN_APPROVAL_REQUIRED,
            f"{evidence.get('blocker', 'A challenge')} appeared at submit — over to you",
            application_id,
            job_id=application.job_id,
        )
    elif outcome is Outcome.HELD:
        await _move(session, application, S.AWAITING_APPROVAL, reason="pre-submit verdict held it")
        await _emit(
            EventType.PRESUBMIT_HELD,
            f"Not submitted — {result.holds[0] if result.holds else 'a field is not ready'}",
            application_id,
            job_id=application.job_id,
            level=Level.WARN,
        )
    else:
        application.attempts += 1
        application.last_error = str(evidence.get("reason", "submit failed"))
        prescription = prescribe(application.last_error, attempt=application.attempts)
        result.recovery = prescription.to_dict()
        await audit.record(
            session,
            audit.Decision(
                agent="recovery",
                task="what should happen after this submit failure?",
                subject_type="application",
                subject_id=application_id,
                decision=prescription.action.value,
                confidence=0.9 if prescription.kind.value != "unknown" else 0.4,
                evidence={"kind": prescription.kind.value, "attempt": prescription.attempt},
                inputs={"error": application.last_error[:300]},
                outcome=prescription.reason,
            ),
            candidate_id=application.candidate_id,
        )
        # From APPLYING the only legal failure state is FAILED, and that is deliberate:
        # a failure during the submit click is the one case where the application may
        # actually have landed, so it must stay reachable by the verification path
        # rather than being written off terminally here.
        await _move(session, application, S.FAILED, reason=prescription.reason)
        if prescription.terminal:
            await _move(session, application, S.FAILED_TERMINAL, reason=prescription.reason)
        await _emit(
            EventType.APPLICATION_FAILED,
            f"Submit failed ({prescription.kind.value}): {prescription.user_message}",
            application_id,
            job_id=application.job_id,
            level=Level.ERROR,
            action=prescription.action.value,
        )

    application.preview = {
        **(application.preview or {}),
        "submitted_at": datetime.now(UTC).isoformat(),
        "submit_outcome": outcome.value,
        "screenshots": result.screenshots,
    }
    await session.flush()
    await registry.drop(application_id)

    result.status = application.status
    result.evidence = evidence
    return result


async def reject(session: AsyncSession, application_id: str, reason: str = "") -> Application:
    """The user decided against this one. Feeds the learning engine."""
    application = await repo.get_application(session, application_id)
    if application is None:
        raise ApplicationError(f"application {application_id} not found")
    await _move(session, application, S.REJECTED, reason=reason or "rejected by user")
    await get_registry().drop(application_id)
    await _emit(
        EventType.JOB_REJECTED, reason or "Rejected", application_id, job_id=application.job_id
    )
    return application


async def takeover(
    session: AsyncSession, application_id: str, candidate: Candidate
) -> PrepareResult:
    """Open a *visible* browser on the form and hand it to the user.

    The one thing that must never be automated around: CAPTCHA, MFA, a login wall,
    an identity check, or a question only the candidate can answer. The agent fills
    what it can prove and then gets out of the way.
    """
    application = await repo.get_application(session, application_id)
    if application is None:
        raise ApplicationError(f"application {application_id} not found")

    await get_registry().drop(application_id)
    result = await prepare(session, application_id, candidate, use_llm=False, headless=False)
    await _emit(
        EventType.HUMAN_APPROVAL_REQUIRED,
        "A browser window is open on the form — finish it and then set the status yourself",
        application_id,
        job_id=application.job_id,
    )
    return result


async def set_status(
    session: AsyncSession,
    application_id: str,
    status: str,
    *,
    note: str = "",
    evidence: dict[str, Any] | None = None,
) -> Application:
    """A manual status change: interview, rejection, offer, or confirming a
    submission the agent could not verify."""
    application = await repo.get_application(session, application_id)
    if application is None:
        raise ApplicationError(f"application {application_id} not found")

    try:
        target = S(status)
    except ValueError as exc:
        raise ApplicationError(f"unknown status {status!r}") from exc

    try:
        await _move(
            session,
            application,
            target,
            evidence=evidence or ({"confirmed_by": "user"} if target is S.APPLIED else None),
            reason=note or "set by user",
        )
    except IllegalTransition as exc:
        raise ApplicationError(str(exc)) from exc

    if note:
        application.notes = f"{application.notes}\n{note}".strip()
    if target is S.APPLIED and application.submitted_at is None:
        application.submitted_at = datetime.now(UTC)
        application.follow_up_date = date.today() + timedelta(days=FOLLOW_UP_DAYS)
    if target in {S.INTERVIEW, S.OFFER}:
        await _emit(
            EventType.INTERVIEW_RECORDED if target is S.INTERVIEW else EventType.OFFER_RECORDED,
            f"{target.value.title()} recorded",
            application_id,
            job_id=application.job_id,
        )
    await session.flush()
    return application


# ─── Batch ──────────────────────────────────────────────────────────────────


async def prepare_batch(
    session: AsyncSession,
    job_ids: list[str],
    candidate: Candidate,
    candidate_id: str,
    *,
    use_llm: bool = True,
    limit: int = 5,
) -> list[PrepareResult]:
    """Shortlist and prepare several jobs, respecting the daily cap.

    Sequential on purpose: several Chromium instances filling forms at once is how
    an unattended run produces results nobody can review, and the cost of a
    mistake here is an application the candidate has to explain.
    """
    settings = get_settings()
    out: list[PrepareResult] = []
    for job_id in job_ids[:limit]:
        submitted = await repo.applications_submitted_today(session, candidate_id)
        if submitted >= settings.daily_application_cap:
            log.info("prepare_batch_capped", submitted=submitted)
            break
        try:
            application = await shortlist(session, job_id, candidate_id)
            out.append(await prepare(session, application.id, candidate, use_llm=use_llm))
        except ApplicationError as exc:
            out.append(PrepareResult(application_id="", status="error", error=str(exc)))
    return out
