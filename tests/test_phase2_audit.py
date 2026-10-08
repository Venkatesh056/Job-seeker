"""The decision trail and the render-reuse rule — spec §50, §77, §45.

Two things Phase 2 added that could each fail silently:

  * **The audit trail.** A decision table nothing writes to, or one whose write can
    abort an application, is worse than not having it. Both halves are tested: that
    decisions reach the database from the real code paths, and that a broken write is
    swallowed.
  * **Render reuse.** The graph tailors, then `prepare` tailored again — two strong-tier
    LLM calls and two PDFs for one document. The reuse rule fixes it, and reuse that is
    *too* eager is the worse bug: reusing a document that failed evidence validation
    would attach an unevidenced resume to a real application.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind


def fact(kind: FactKind, label: str, **kwargs) -> Fact:
    return Fact(
        kind=kind,
        label=label,
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#page=1,line=1",
        **kwargs,
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("CAREERAGENT_CACHE_DIR", str(tmp_path / "cache"))

    from careeragent import config as config_module
    from careeragent import storage

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    yield tmp_path
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]


async def seed():
    """A candidate and one job, enough to shortlist and tailor against."""
    from careeragent.models.job import RawJob
    from careeragent.pipeline.normalize import normalize_all
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo

    await init_db(drop=True)
    model = Candidate(
        name="Asha Raman",
        email="asha@example.com",
        phone="+91 90000 00000",
        location="Bangalore, India",
        facts=[
            fact(FactKind.SKILL, "python"),
            fact(FactKind.SKILL, "pytorch"),
            fact(FactKind.PROJECT, "Resume Ranker", body="Built a ranking pipeline in Python."),
        ],
    )
    jobs = normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id="1",
                title="ML Engineer",
                url="https://boards.greenhouse.io/acme/jobs/1",
                apply_url="https://boards.greenhouse.io/acme/jobs/1",
                company_name="Acme",
                location="Bangalore, India",
                description="Build models with PyTorch and Python.",
                ats="greenhouse",
            )
        ]
    )
    async with session_scope() as session:
        row = await repo.save_candidate_profile(session, model)
        await repo.replace_facts(session, row.id, model.facts)
        await repo.upsert_jobs(session, jobs)
        candidate_id = row.id
    return candidate_id, jobs[0].job_id, model


# ─── The Decision value object ──────────────────────────────────────────────


def test_a_decision_is_a_justification_not_a_document():
    """Long prose is truncated. Evidence rows must stay scannable and cheap to store."""
    from careeragent.agents import audit

    decision = audit.Decision(
        agent="recovery", decision="give_up", outcome="x" * (audit.MAX_OUTCOME_CHARS + 500)
    )
    assert len(decision.to_dict()["outcome"]) == audit.MAX_OUTCOME_CHARS


def test_confidence_appears_in_the_one_line_summary():
    from careeragent.agents import audit

    assert "60%" in audit.Decision(agent="a", decision="apply", confidence=0.6).summary()
    # Zero confidence is "not recorded", not "0% sure" — showing 0% would read as a
    # finding when it is an absence.
    assert "%" not in audit.Decision(agent="a", decision="apply").summary()


# ─── Persistence ────────────────────────────────────────────────────────────


async def test_decisions_round_trip_and_group_by_agent(isolated):
    from careeragent.agents import audit
    from careeragent.storage import init_db, session_scope

    await init_db(drop=True)
    async with session_scope() as session:
        assert await audit.record(
            session,
            audit.Decision(
                agent="freshness",
                decision="live",
                task="is it open?",
                subject_type="job",
                subject_id="job-1",
                confidence=1.0,
                evidence={"status_code": 200},
                tools_used=["http_get"],
            ),
        )
        await audit.record(
            session,
            audit.Decision(
                agent="freshness",
                decision="expired",
                subject_type="job",
                subject_id="job-2",
                confidence=0.5,
            ),
        )
        await audit.record(
            session, audit.Decision(agent="recovery", decision="retry", confidence=0.9)
        )

    async with session_scope() as session:
        trail = await audit.for_subject(session, "job", "job-1")
        assert [row["decision"] for row in trail] == ["live"]
        assert trail[0]["evidence"] == {"status_code": 200}
        assert trail[0]["tools_used"] == ["http_get"]

        summary = await audit.summarize(session)
        assert summary["total"] == 3
        by_agent = {row["agent"]: row for row in summary["agents"]}
        assert by_agent["freshness"]["decisions"] == 2
        assert by_agent["freshness"]["avg_confidence"] == 0.75

        # Newest first, and filterable — "what has recovery been doing?"
        assert [r["agent"] for r in await audit.recent(session, agent="recovery")] == ["recovery"]


async def test_an_unwritable_decision_never_raises(isolated):
    """The audit trail must not be able to cost the user an application.

    A session that rejects the write stands in for the real failures — a locked
    database, a schema not yet migrated — because the caller has already done the work
    the row describes and losing the record is strictly better than losing that work.
    """
    from careeragent.agents import audit

    class Broken:
        def add(self, _row):
            raise RuntimeError("database is locked")

    assert await audit.record(Broken(), audit.Decision(agent="a", decision="b")) is False


async def test_a_trail_flushes_once_for_many_decisions(isolated):
    """The fan-out case: a campaign decides about hundreds of jobs across branches."""
    from careeragent.agents import audit
    from careeragent.storage import init_db, session_scope

    await init_db(drop=True)
    trail = audit.Trail(run_id="run-1")
    for i in range(5):
        trail.note("opportunity_scoring", f"apply #{i}", subject_type="job", subject_id=f"j{i}")
    assert len(trail.to_dicts()) == 5

    async with session_scope() as session:
        assert await trail.flush(session) == 5
    # Flushed decisions are cleared, so a second flush cannot double-write them.
    assert trail.decisions == []

    async with session_scope() as session:
        assert (await audit.summarize(session))["total"] == 5


# ─── The real code paths write to it ────────────────────────────────────────


async def test_tailoring_records_why_the_document_looks_like_that(isolated):
    """§69: a decision table is only real if the production path writes to it."""
    from careeragent.agents import audit
    from careeragent.applications import service
    from careeragent.storage import session_scope

    candidate_id, job_id, candidate = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        await service.tailor_for(session, application, candidate, use_llm=False, render_pdf=False)

    async with session_scope() as session:
        trail = await audit.for_subject(session, "job", job_id)
        tailoring = [row for row in trail if row["agent"] == "resume_optimization"]
        assert len(tailoring) == 1
        row = tailoring[0]
        # The deterministic path is recorded as deterministic. A trail that said "llm"
        # for a document no model touched would be worse than no trail.
        assert "deterministic" in row["decision"]
        assert "llm" not in row["tools_used"]
        assert row["evidence"]["fact_ids"], "a tailored resume traces to specific facts"
        assert row["outcome"] == "accepted"


async def test_the_decisions_api_reads_the_trail(isolated):
    from careeragent.agents import audit
    from careeragent.api import app
    from careeragent.storage import init_db, session_scope

    await init_db(drop=True)
    async with session_scope() as session:
        await audit.record(
            session,
            audit.Decision(
                agent="verification",
                decision="blocked",
                subject_type="application",
                subject_id="app-1",
                confidence=1.0,
                outcome="the form claims 7 years; your resume supports 0.2",
            ),
        )

    with TestClient(app) as client:
        listing = client.get("/api/v1/decisions").json()
        assert listing["count"] == 1
        assert listing["decisions"][0]["agent"] == "verification"

        # `/summary` must not be swallowed by `/{subject_type}/{subject_id}`.
        summary = client.get("/api/v1/decisions/summary").json()
        assert summary["total"] == 1

        one = client.get("/api/v1/decisions/application/app-1").json()
        assert "7 years" in one["decisions"][0]["outcome"]

        # An unknown subject is an empty trail, not a 404.
        empty = client.get("/api/v1/decisions/job/does-not-exist")
        assert empty.status_code == 200
        assert empty.json()["count"] == 0


async def test_status_reports_whether_durability_is_real(isolated):
    """A process that fell back to in-memory checkpoints must say so, not imply durability."""
    from careeragent.api import app

    with TestClient(app) as client:
        status = client.get("/api/v1/status").json()
        assert status["checkpoints"]["backend"] in {"sqlite", "postgres", "memory"}
        assert status["checkpoints"]["durable"] is (
            status["checkpoints"]["backend"] in {"sqlite", "postgres"}
        )
        assert "decisions_recorded" in status


# ─── Render reuse ───────────────────────────────────────────────────────────


# These two render real PDFs, because the reuse rule requires the PDF to exist on disk —
# checking for a file that is never written would test nothing. Chromium launch dominates
# the runtime, hence the raised timeout.
@pytest.mark.timeout(300)
async def test_tailoring_twice_reuses_the_render(isolated):
    """The bug this fixes: the graph tailored, then `prepare` tailored the same document
    again — two LLM rewrites and two PDFs per application."""
    from sqlalchemy import func, select

    from careeragent.applications import service
    from careeragent.storage import session_scope
    from careeragent.storage.models import ResumeRender

    candidate_id, job_id, candidate = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        first, _ = await service.tailor_for(session, application, candidate, use_llm=False)
        second, _ = await service.tailor_for(session, application, candidate, use_llm=False)

    assert first == second
    async with session_scope() as session:
        count = (await session.execute(select(func.count()).select_from(ResumeRender))).scalar_one()
        assert count == 1


@pytest.mark.timeout(300)
async def test_force_tailors_again(isolated):
    """An explicit request from the user must produce a fresh document."""
    from sqlalchemy import func, select

    from careeragent.applications import service
    from careeragent.storage import session_scope
    from careeragent.storage.models import ResumeRender

    candidate_id, job_id, candidate = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        first, _ = await service.tailor_for(session, application, candidate, use_llm=False)
        second, _ = await service.tailor_for(
            session, application, candidate, use_llm=False, force=True
        )

    assert first != second
    async with session_scope() as session:
        count = (await session.execute(select(func.count()).select_from(ResumeRender))).scalar_one()
        assert count == 2


async def test_a_render_that_failed_validation_is_never_reused(isolated):
    """The dangerous direction. Reuse exists to save money; reusing an unevidenced
    document would attach a resume the validator refused to a real application."""
    from careeragent.applications import service
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        await repo.save_resume_render(
            session,
            candidate_id,
            job_id,
            "ml_engineer",
            {"summary": "fabricated"},
            [],
            {"violations": [{"claim": "5 years of Kubernetes", "kind": "unevidenced"}]},
            "whatever.pdf",
        )
        assert await service._reusable_render(session, application) is None


async def test_a_render_whose_pdf_is_gone_is_never_reused(isolated):
    """The row outlives the file whenever `data/` is cleared, and a render with no PDF
    cannot be attached to anything."""
    from careeragent.applications import service
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        await repo.save_resume_render(
            session,
            candidate_id,
            job_id,
            "ml_engineer",
            {"summary": "fine"},
            ["fact-1"],
            {"violations": []},
            str(isolated / "deleted" / "resume.pdf"),
        )
        assert await service._reusable_render(session, application) is None


@pytest.mark.timeout(300)
async def test_a_different_archetype_is_a_different_document(isolated):
    from careeragent.applications import service
    from careeragent.storage import session_scope

    candidate_id, job_id, candidate = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        await service.tailor_for(
            session, application, candidate, archetype="ml_engineer", use_llm=False
        )
        assert (
            await service._reusable_render(session, application, archetype="data_scientist")
        ) is None


# ─── The event bus outlives the app ─────────────────────────────────────────


def test_the_bus_refuses_a_duplicate_sink():
    """Found by a hanging test, but the real symptom was silent duplication.

    The bus is process-global; the application lifespan is not. Every app startup
    appended another `DatabaseSink`, so after a `uvicorn --reload` each event was
    written to `agent_event` twice, then three times. Nothing raised — the activity
    feed just grew duplicates of everything, and every event became N database writes.
    """
    from careeragent.events import DatabaseSink, EventBus, LogSink

    bus = EventBus()
    assert bus.sink_names() == ["LogSink"]
    assert bus.add_sink(DatabaseSink()) is True
    assert bus.add_sink(DatabaseSink()) is False, "a second sink of the same type stacks writes"
    assert bus.sink_names() == ["LogSink", "DatabaseSink"]

    assert bus.remove_sink(DatabaseSink()) is True
    assert bus.sink_names() == ["LogSink"]
    assert bus.remove_sink(DatabaseSink()) is False

    # Different sink types still compose — the rule is per type, not a limit of one.
    assert bus.add_sink(DatabaseSink()) is True
    assert bus.add_sink(LogSink()) is False  # LogSink is already the default
    assert bus.sink_names() == ["LogSink", "DatabaseSink"]


def test_repeated_app_startups_leave_one_sink(isolated):
    """The actual scenario: `uvicorn --reload`, or a test suite that starts the app often."""
    from careeragent.api import app
    from careeragent.events import get_bus

    before = get_bus().sink_names()
    for _ in range(3):
        with TestClient(app):
            pass
    assert get_bus().sink_names() == before
