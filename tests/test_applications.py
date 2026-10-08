"""The application service and the new API surface, against a real database.

Integration rather than unit: the guards that matter here — the duplicate constraint,
the daily cap, the autonomy gate, the evidence requirement — only exist end to end, and
a mocked session would test the mock.

No browser and no network: `prepare` is only exercised through paths that stop before
opening one, and the browser half has its own suite in `test_browser_runner.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    """A fresh database and storage directory per test."""
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


async def seed(candidate_name: str = "Asha Raman"):
    """A candidate with a Fact Bank and one stored job, ready to apply to."""
    from careeragent.models.job import RawJob
    from careeragent.pipeline.normalize import normalize_all
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo

    await init_db(drop=True)

    model = Candidate(
        name=candidate_name,
        email="asha@example.com",
        phone="+91 90000 00000",
        location="Bangalore, India",
        facts=[
            fact(FactKind.SKILL, "python"),
            fact(FactKind.SKILL, "pytorch"),
            fact(FactKind.PROJECT, "Resume Ranker", body="Built an NLP pipeline in Python."),
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


# ─── Duplicate guard ────────────────────────────────────────────────────────


async def test_shortlisting_twice_returns_the_same_application(isolated):
    from careeragent.applications import service
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        first = await service.shortlist(session, job_id, candidate_id)
        second = await service.shortlist(session, job_id, candidate_id)
        assert first.id == second.id


async def test_the_database_refuses_a_second_application(isolated):
    """The real enforcement is the unique constraint, not the convenience check."""
    from sqlalchemy.exc import IntegrityError

    from careeragent.storage import session_scope
    from careeragent.storage.models import Application

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        session.add(Application(job_id=job_id, candidate_id=candidate_id))
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(Application(job_id=job_id, candidate_id=candidate_id))


async def test_shortlist_copies_the_score_and_the_application_url(isolated):
    from careeragent.applications import service
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        assert application.application_url
        assert application.ats == "greenhouse"


# ─── Autonomy and caps ──────────────────────────────────────────────────────


async def test_low_autonomy_refuses_to_open_a_browser(isolated):
    from careeragent.applications import service
    from careeragent.applications.service import ApplicationError
    from careeragent.storage import session_scope

    candidate_id, job_id, model = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        with pytest.raises(ApplicationError, match="autonomy level 2"):
            await service.prepare(session, application.id, model, autonomy_level=2)


async def test_approving_below_level_four_is_refused(isolated):
    from careeragent.applications import service
    from careeragent.applications.service import ApplicationError
    from careeragent.storage import session_scope

    candidate_id, job_id, model = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        with pytest.raises(ApplicationError, match="does not permit submitting"):
            await service.approve(session, application.id, model, autonomy_level=3)


async def test_the_daily_cap_stops_submission(isolated, monkeypatch):
    """The cap counts unverified submissions too: a runaway loop must be stopped."""
    from careeragent.applications import service
    from careeragent.applications.service import ApplicationError
    from careeragent.config import get_settings
    from careeragent.storage import session_scope
    from careeragent.storage.models import Application, Job

    candidate_id, job_id, model = await seed()
    get_settings().daily_application_cap = 1

    async with session_scope() as session:
        # One already submitted in the last 24 hours.
        session.add(
            Job(
                id="other",
                company="Globex",
                title="Data Scientist",
                normalized_title="data scientist",
            )
        )
        await session.flush()
        session.add(
            Application(
                job_id="other",
                candidate_id=candidate_id,
                status="submission_unverified",
                submitted_at=datetime.now(UTC) - timedelta(hours=2),
            )
        )

    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        with pytest.raises(ApplicationError, match="daily cap"):
            await service.approve(session, application.id, model, autonomy_level=4)

    get_settings().daily_application_cap = 15


async def test_an_already_applied_application_is_not_prepared_again(isolated):
    from careeragent.applications import service
    from careeragent.applications.service import ApplicationError
    from careeragent.storage import session_scope

    candidate_id, job_id, model = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        application.status = "applied"
        await session.flush()
        with pytest.raises(ApplicationError, match="already"):
            await service.prepare(session, application.id, model)


# ─── Tailoring through the service ──────────────────────────────────────────


async def test_tailoring_stores_a_render_and_advances_the_status(isolated):
    from careeragent.applications import service
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    candidate_id, job_id, model = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        render_id, validation = await service.tailor_for(
            session, application, model, use_llm=False, render_pdf=False
        )
        assert render_id
        assert validation["ok"]
        assert application.status == "resume_tailored"
        assert application.resume_render_id == render_id

        render = await repo.get_resume_render(session, render_id)
        assert render.fact_ids, "a render with no evidence link is not traceable"
        assert render.archetype == "ml_engineer"


async def test_tailoring_records_an_event(isolated):
    from careeragent.applications import service
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    candidate_id, job_id, model = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        await service.tailor_for(session, application, model, use_llm=False, render_pdf=False)
        events = await repo.application_events(session, application.id)
        assert any(e.type == "status_resume_tailored" for e in events)


# ─── Status transitions through the service ─────────────────────────────────


async def test_manual_status_change_is_validated(isolated):
    from careeragent.applications import service
    from careeragent.applications.service import ApplicationError
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        with pytest.raises(ApplicationError, match="not a legal transition"):
            await service.set_status(session, application.id, "offer")


async def test_confirming_a_submission_sets_a_follow_up_date(isolated):
    from careeragent.applications import service
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        application.status = "submission_unverified"
        await session.flush()
        await service.set_status(session, application.id, "applied", note="confirmed by email")
        assert application.status == "applied"
        assert application.submitted_at is not None
        assert application.follow_up_date is not None


async def test_rejecting_records_the_reason(isolated):
    from careeragent.applications import service
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        await service.reject(session, application.id, "salary too low")
        events = await repo.application_events(session, application.id)
        assert any("salary too low" in str(e.payload) for e in events)


# ─── Tracker projections ────────────────────────────────────────────────────


async def test_board_places_the_application_in_a_column(isolated):
    from careeragent.applications import service, tracker
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        await service.shortlist(session, job_id, candidate_id)
        board = await tracker.board(session, candidate_id)

    assert board["total"] == 1
    shortlisted = next(c for c in board["columns"] if c["name"] == "Shortlisted")
    assert shortlisted["count"] == 1


async def test_summary_does_not_count_unverified_as_submitted(isolated):
    from careeragent.applications import service, tracker
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        application.status = "submission_unverified"
        application.submitted_at = datetime.now(UTC)
        await session.flush()
        summary = await tracker.summary(session, candidate_id)

    assert summary["submitted"] == 0, "unverified must not inflate the funnel"
    assert summary["submission_unverified"] == 1
    assert summary["needs_human"] == 1


async def test_queue_surfaces_what_needs_a_human(isolated):
    from careeragent.applications import service, tracker
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        application.status = "awaiting_approval"
        await session.flush()
        queue = await tracker.queue(session, candidate_id)

    assert len(queue) == 1
    assert queue[0]["needs_attention"] == "waiting for your approval"


async def test_timeline_returns_the_full_audit_trail(isolated):
    from careeragent.applications import service, tracker
    from careeragent.storage import session_scope

    candidate_id, job_id, model = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        await service.tailor_for(session, application, model, use_llm=False, render_pdf=False)
        timeline = await tracker.timeline(session, application.id)

    assert timeline["job"]["title"] == "ML Engineer"
    assert len(timeline["events"]) >= 2


# ─── Analytics over a real database ─────────────────────────────────────────


async def test_analytics_withholds_a_rate_on_a_small_sample(isolated):
    from careeragent import analytics
    from careeragent.applications import service
    from careeragent.storage import session_scope

    candidate_id, job_id, _ = await seed()
    async with session_scope() as session:
        application = await service.shortlist(session, job_id, candidate_id)
        application.status = "applied"
        application.submitted_at = datetime.now(UTC)
        await session.flush()
        rates = await analytics.rates(session, candidate_id)

    assert rates["response"]["n"] == 1
    assert rates["response"]["rate"] is None, "one application is not a response rate"


async def test_learning_says_it_does_not_know_yet(isolated):
    from careeragent import learning
    from careeragent.storage import session_scope

    candidate_id, _job_id, _ = await seed()
    async with session_scope() as session:
        observations = await learning.observations(session, candidate_id)
        proposal = await learning.propose_weights(session, candidate_id)

    assert observations[0]["kind"] == "insufficient_data"
    assert proposal.confidence == "none"
    assert not proposal.actionable


# ─── API surface ────────────────────────────────────────────────────────────


@pytest.fixture
def client(isolated):
    from careeragent.api import app

    with TestClient(app) as c:
        yield c


def test_approve_requires_an_explicit_confirm(client):
    """An accidental POST must not submit an application."""
    response = client.post("/api/v1/applications/does-not-matter/approve", json={"confirm": False})
    assert response.status_code == 400
    assert "confirm" in response.json()["detail"]


def test_sending_outreach_requires_an_explicit_confirm(client):
    assert client.post("/api/v1/outreach/x/send").status_code == 400


def test_applying_a_learning_proposal_requires_an_explicit_confirm(client):
    assert client.post("/api/v1/learning/proposals/x/apply").status_code == 400


def test_settings_never_return_an_api_key(client):
    """A key readable over HTTP is a key that leaks through a screenshot."""
    body = client.get("/api/v1/settings/llm").json()
    serialized = str(body)
    assert "sk-" not in serialized
    assert "api_key" not in serialized
    for info in body["providers"].values():
        assert set(info) <= {"configured", "note"}


def test_autonomy_reports_non_negotiable_hard_stops(client):
    body = client.get("/api/v1/settings/autonomy").json()
    joined = " ".join(body["hard_stops"]).lower()
    for stop in ("captcha", "mfa", "demographic", "sponsorship"):
        assert stop in joined
    assert "not configurable" in body["note"]


def test_autonomy_level_is_bounded(client):
    assert client.put("/api/v1/settings/autonomy", json={"level": 9}).status_code == 422
    assert client.put("/api/v1/settings/autonomy", json={"level": 4}).status_code == 200


def test_unknown_weights_are_rejected(client):
    response = client.put(
        "/api/v1/settings/weights", json={"weights": {"vibes": 0.5}, "rationale": ""}
    )
    assert response.status_code == 422
    assert "vibes" in response.json()["detail"]


def test_weights_are_versioned(client):
    first = client.put("/api/v1/settings/weights", json={"weights": {"location": 0.07}}).json()
    second = client.put("/api/v1/settings/weights", json={"weights": {"location": 0.08}}).json()
    assert second["version"] == first["version"] + 1


def test_archetypes_are_listed(client):
    body = client.get("/api/v1/resumes/variants").json()
    ids = {v["id"] for v in body}
    assert {"ai_engineer", "ml_engineer", "data_analyst", "nlp_engineer"} <= ids


def test_downloading_an_unvalidated_render_is_refused(client, isolated):
    """A document with evidence violations must not reach a recruiter."""
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    async def make_bad_render() -> str:
        async with session_scope() as session:
            row = await repo.get_or_create_candidate(session)
            render = await repo.save_resume_render(
                session,
                row.id,
                None,
                "custom",
                {"candidate_name": "X"},
                [],
                {"ok": False, "violations": [{"kind": "unsupported_skill", "detail": "x"}]},
                storage_key="/nowhere.pdf",
            )
            return render.id

    # The TestClient is synchronous, so the async setup runs on its own portal loop.
    from anyio.from_thread import start_blocking_portal

    with start_blocking_portal() as portal:
        render_id = portal.call(make_bad_render)

    response = client.get(f"/api/v1/resumes/renders/{render_id}/download")
    assert response.status_code == 409
    assert "violation" in response.json()["detail"]


def test_analytics_endpoints_respond_on_an_empty_database(client):
    for path in (
        "/api/v1/analytics/funnel",
        "/api/v1/analytics/rates",
        "/api/v1/analytics/costs",
        "/api/v1/analytics/overview",
        "/api/v1/applications",
        "/api/v1/applications/queue",
        "/api/v1/outreach",
        "/api/v1/learning/observations",
    ):
        assert client.get(path).status_code == 200, path


def test_unknown_analytics_dimension_is_rejected(client):
    assert client.get("/api/v1/analytics/by-dimension?dimension=astrology").status_code == 422


def test_contact_without_a_permitted_source_is_refused(client):
    response = client.post(
        "/api/v1/outreach/contacts",
        json={
            "company_id": "x",
            "channel": "email",
            "address": "someone@acme.com",
            "source": "guessed_from_pattern",
        },
    )
    assert response.status_code == 422
    assert "not permitted" in response.json()["detail"]
