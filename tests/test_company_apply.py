"""Applying to a company rather than to a job.

The interesting tests here are the ones that prove this is *not* a mass-application
button: that the daily cap binds, that a hard-failed opening is refused with its reason
rather than quietly applied to, that a stale page cannot prepare something that has since
been filled, and that nothing claims to have been submitted.
"""

from __future__ import annotations

from datetime import date

import pytest

from careeragent.applications import company_apply
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ─── Building a world to plan against ───────────────────────────────────────


async def _world(
    *,
    jobs: list[dict],
    cap: int = 5,
    submitted_today: int = 0,
) -> tuple[str, str, Candidate]:
    """One company, some scored jobs, and a candidate. Returns (company_id, cand_id)."""
    from careeragent.storage import init_db, session_scope
    from careeragent.storage.models import Job, JobScore

    await init_db(drop=True)
    candidate = Candidate(
        name="Test Candidate",
        email="t@example.com",
        facts=[
            Fact(
                kind=FactKind.SKILL,
                label="python",
                evidence_source=EvidenceSource.MASTER_RESUME,
                evidence_locator="cv.pdf#page=1",
            )
        ],
    )

    from careeragent.storage import repository as repo

    async with session_scope() as session:
        row = await repo.save_candidate_profile(session, candidate)
        await repo.replace_facts(session, row.id, candidate.facts)
        candidate_id = row.id

        company = await repo.upsert_company(session, "Acme Analytics", "acme.example")
        await session.flush()
        company_id = company.id

        for spec in jobs:
            job = Job(
                id=spec["id"],
                company_id=company_id if spec.get("linked", True) else None,
                company=spec.get("company", "Acme Analytics"),
                title=spec["title"],
                normalized_title=spec["title"].lower(),
                location=spec.get("location", "Bangalore"),
                description="python data work",
                status=spec.get("status", "open"),
                posted_date=date.today(),
            )
            session.add(job)
            session.add(
                JobScore(
                    job_id=job.id,
                    candidate_id=candidate_id,
                    final_score=spec.get("score", 0.7),
                    priority=spec.get("priority", "medium"),
                    deterministic=spec.get("deterministic", {}),
                )
            )

        for n in range(submitted_today):
            # The job has to exist. These applications used to reference "submitted-0"
            # and friends, which were never inserted — the foreign key was in the schema
            # and SQLite was not enforcing it, so the rows went in orphaned. Turning
            # `PRAGMA foreign_keys=ON` surfaced seven tests doing this.
            session.add(
                Job(
                    id=f"submitted-{n}",
                    company_id=company_id,
                    company="Acme Analytics",
                    title=f"Already applied {n}",
                    normalized_title=f"already applied {n}",
                    description="python data work",
                    status="open",
                    posted_date=date.today(),
                )
            )
            await session.flush()
            await repo.create_application(session, f"submitted-{n}", candidate_id)
            application = await repo.application_for_job(session, f"submitted-{n}", candidate_id)
            if application is not None:
                application.status = "applied"
                application.submitted_at = __import__("datetime").datetime.now(
                    __import__("datetime").UTC
                )

    return company_id, candidate_id, candidate


async def _plan(company_id, candidate_id, candidate, **kwargs):
    from careeragent.storage import session_scope

    async with session_scope() as session:
        return await company_apply.plan(
            session, company_id, candidate, candidate_id, refresh=False, **kwargs
        )


# ─── What the plan says ─────────────────────────────────────────────────────


async def test_every_opening_at_the_company_is_found(isolated):
    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": "j1", "title": "Data Analyst", "score": 0.8},
            {"id": "j2", "title": "ML Engineer", "score": 0.7},
            {"id": "j3", "title": "Data Engineer", "score": 0.6},
        ]
    )
    result = await _plan(company_id, candidate_id, candidate)

    assert len(result.openings) == 3
    assert len(result.applyable) == 3


async def test_a_job_carrying_only_the_company_name_is_still_found(isolated):
    """Directory sources give a name and no id. Asking by id alone would miss them."""
    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": "j1", "title": "Data Analyst", "score": 0.8},
            {"id": "j2", "title": "Analyst II", "score": 0.7, "linked": False},
        ]
    )
    result = await _plan(company_id, candidate_id, candidate)
    assert {o.job_id for o in result.openings} == {"j1", "j2"}


async def test_a_similarly_named_company_is_not_swept_in(isolated):
    """ "Acme Analytics" must not pull in "Acme Analytics Staffing Partners"."""
    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": "j1", "title": "Data Analyst", "score": 0.8},
            {
                "id": "j2",
                "title": "Analyst",
                "score": 0.9,
                "linked": False,
                "company": "Acme Analytics Staffing Partners",
            },
        ]
    )
    result = await _plan(company_id, candidate_id, candidate)
    assert {o.job_id for o in result.openings} == {"j1"}


async def test_openings_are_ranked_best_first(isolated):
    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": "low", "title": "C", "score": 0.4},
            {"id": "high", "title": "A", "score": 0.9},
            {"id": "mid", "title": "B", "score": 0.6},
        ]
    )
    result = await _plan(company_id, candidate_id, candidate)
    assert [o.job_id for o in result.openings] == ["high", "mid", "low"]


async def test_a_closed_posting_is_not_offered(isolated):
    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": "open", "title": "Open", "score": 0.8},
            {"id": "gone", "title": "Filled", "score": 0.9, "status": "closed"},
        ]
    )
    result = await _plan(company_id, candidate_id, candidate)
    assert [o.job_id for o in result.openings] == ["open"]


# ─── What it refuses, and why ───────────────────────────────────────────────


async def test_a_hard_failed_opening_is_shown_with_its_real_reason(isolated):
    """Not "low score" — the reason the scorer actually gave."""
    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": "ok", "title": "Data Analyst", "score": 0.8},
            {
                "id": "senior",
                "title": "Principal Data Scientist",
                "score": 0.0,
                "priority": "reject",
                "deterministic": {
                    "hard_fail": "senior-level role, and you are targeting entry level"
                },
            },
        ]
    )
    result = await _plan(company_id, candidate_id, candidate)

    senior = next(o for o in result.openings if o.job_id == "senior")
    assert not senior.applyable
    assert senior.skipped == "senior-level role, and you are targeting entry level"
    assert [o.job_id for o in result.applyable] == ["ok"]


async def test_an_opening_already_applied_to_is_not_offered_again(isolated):
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": "j1", "title": "Data Analyst", "score": 0.8},
            {"id": "j2", "title": "ML Engineer", "score": 0.7},
        ]
    )
    async with session_scope() as session:
        await repo.create_application(session, "j1", candidate_id)

    result = await _plan(company_id, candidate_id, candidate)
    j1 = next(o for o in result.openings if o.job_id == "j1")
    assert j1.skipped == "already applied"
    assert [o.job_id for o in result.applyable] == ["j2"]


async def test_an_unscored_opening_says_so_rather_than_scoring_zero(isolated):
    from careeragent.storage import session_scope
    from careeragent.storage.models import Job

    company_id, candidate_id, candidate = await _world(
        jobs=[{"id": "j1", "title": "Data Analyst", "score": 0.8}]
    )
    async with session_scope() as session:
        session.add(
            Job(
                id="unscored",
                company_id=company_id,
                company="Acme Analytics",
                title="New Role",
                normalized_title="new role",
                status="open",
            )
        )

    result = await _plan(company_id, candidate_id, candidate)
    unscored = next(o for o in result.openings if o.job_id == "unscored")
    assert not unscored.applyable
    assert "not scored yet" in unscored.skipped


async def test_a_score_floor_is_applied_when_asked_for(isolated):
    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": "good", "title": "A", "score": 0.8},
            {"id": "weak", "title": "B", "score": 0.3},
        ]
    )
    result = await _plan(company_id, candidate_id, candidate, min_score=0.5)
    assert [o.job_id for o in result.applyable] == ["good"]
    weak = next(o for o in result.openings if o.job_id == "weak")
    assert "below your 0.50 floor" in weak.skipped


async def test_a_weak_match_says_it_scored_low_not_that_a_rule_refused_it(isolated):
    """Two different causes of rejection, two different sentences.

    Found against a live Greenhouse board: three roles were being reported as "does not
    match what you want" when what actually happened was a 0.15 score.
    """
    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": "ok", "title": "Data Analyst", "score": 0.8},
            {"id": "weak", "title": "Account Executive", "score": 0.15, "priority": "reject"},
            {
                "id": "stale",
                "title": "Old Role",
                "score": 0.0,
                "priority": "reject",
                "deterministic": {"hard_fail": "posted 49 days ago (limit 30)"},
            },
        ]
    )
    result = await _plan(company_id, candidate_id, candidate)

    weak = next(o for o in result.openings if o.job_id == "weak")
    stale = next(o for o in result.openings if o.job_id == "stale")
    assert weak.skipped == "scores 0.15 — too weak a match to be worth applying to"
    assert stale.skipped == "posted 49 days ago (limit 30)"


async def test_the_note_counts_the_openings_that_exist_not_the_ones_shown(isolated):
    """The list is trimmed to something readable; the count must not be trimmed with it.

    Found against a live board: the note said "61 opening(s)" beside a payload of 25.
    """
    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": f"j{n}", "title": f"Role {n}", "score": 0.1, "priority": "reject"}
            for n in range(40)
        ]
    )
    result = await _plan(company_id, candidate_id, candidate)

    assert len(result.openings) == company_apply.MAX_OPENINGS
    assert result.total_openings == 40
    assert result.to_dict()["total_openings"] == 40
    assert result.to_dict()["showing"] == company_apply.MAX_OPENINGS
    assert "40 opening(s)" in result.note
    assert "Showing the top 25 of 40" in result.note


# ─── The daily cap is not waived by fanning out ─────────────────────────────


async def test_the_plan_says_how_many_the_cap_allows_today(isolated, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DAILY_APPLICATION_CAP", "2")
    from careeragent import config as config_module

    config_module.get_settings.cache_clear()

    company_id, candidate_id, candidate = await _world(
        jobs=[{"id": f"j{n}", "title": f"Role {n}", "score": 0.9 - n / 100} for n in range(6)]
    )
    result = await _plan(company_id, candidate_id, candidate)

    assert len(result.applyable) == 6
    assert result.daily_cap == 2
    assert len(result.would_prepare_now) == 2
    assert "leaves room for 2" in result.note


async def test_nothing_is_prepared_once_the_cap_is_spent(isolated, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DAILY_APPLICATION_CAP", "1")
    from careeragent import config as config_module

    config_module.get_settings.cache_clear()

    company_id, candidate_id, candidate = await _world(
        jobs=[{"id": "j1", "title": "Role", "score": 0.9}],
        submitted_today=1,
    )
    result = await _plan(company_id, candidate_id, candidate)

    assert result.remaining_today == 0
    assert result.would_prepare_now == []
    assert "already used" in result.note


async def test_apply_stops_at_the_cap_rather_than_running_through_it(isolated, monkeypatch):
    """The cap is re-read before every single opening, not computed once at the start."""
    monkeypatch.setenv("CAREERAGENT_DAILY_APPLICATION_CAP", "2")
    from careeragent import config as config_module
    from careeragent.storage import session_scope

    config_module.get_settings.cache_clear()

    company_id, candidate_id, candidate = await _world(
        jobs=[{"id": f"j{n}", "title": f"Role {n}", "score": 0.9} for n in range(5)]
    )

    prepared: list[str] = []

    async def fake_prepare(session, application_id, cand, **kwargs):
        from careeragent.applications.service import PrepareResult

        prepared.append(application_id)
        # Each prepare "submits" for the purposes of the cap, as an approved one would.
        application = await _get_application(session, application_id)
        application.status = "applied"
        application.submitted_at = __import__("datetime").datetime.now(__import__("datetime").UTC)
        await session.flush()
        return PrepareResult(application_id=application_id, status="awaiting_approval")

    monkeypatch.setattr(company_apply, "prepare", fake_prepare)

    async with session_scope() as session:
        outcome = await company_apply.apply(session, company_id, candidate, candidate_id)

    assert len(prepared) == 2, "the cap must bind"
    assert outcome["capped"] is True
    assert "Stopped at today's cap of 2" in outcome["note"]
    assert "3 opening(s) are waiting for tomorrow" in outcome["note"]


async def _get_application(session, application_id):
    from careeragent.storage.models import Application

    return await session.get(Application, application_id)


# ─── A stale page cannot act ────────────────────────────────────────────────


async def test_an_opening_that_is_no_longer_applyable_is_refused_not_prepared(
    isolated, monkeypatch
):
    """The page the user clicked may be minutes old. Every id is re-checked."""
    from careeragent.storage import session_scope

    company_id, candidate_id, candidate = await _world(
        jobs=[
            {"id": "ok", "title": "Fine", "score": 0.8},
            {
                "id": "blocked",
                "title": "Senior Role",
                "score": 0.0,
                "priority": "reject",
                "deterministic": {"hard_fail": "senior-level role"},
            },
        ]
    )

    prepared: list[str] = []

    async def fake_prepare(session, application_id, cand, **kwargs):
        from careeragent.applications.service import PrepareResult

        prepared.append(application_id)
        return PrepareResult(application_id=application_id, status="awaiting_approval")

    monkeypatch.setattr(company_apply, "prepare", fake_prepare)

    async with session_scope() as session:
        outcome = await company_apply.apply(
            session, company_id, candidate, candidate_id, job_ids=["ok", "blocked"]
        )

    assert len(prepared) == 1
    assert [r["job_id"] for r in outcome["refused"]] == ["blocked"]
    assert outcome["refused"][0]["reason"] == "senior-level role"


async def test_a_job_id_from_another_company_is_refused(isolated, monkeypatch):
    from careeragent.storage import session_scope

    company_id, candidate_id, candidate = await _world(
        jobs=[{"id": "ours", "title": "Fine", "score": 0.8}]
    )

    async def fake_prepare(session, application_id, cand, **kwargs):
        raise AssertionError("must not prepare a job from another company")

    monkeypatch.setattr(company_apply, "prepare", fake_prepare)

    async with session_scope() as session:
        outcome = await company_apply.apply(
            session, company_id, candidate, candidate_id, job_ids=["someone-elses"]
        )

    assert outcome["prepared_count"] == 0
    assert outcome["refused"][0]["reason"] == "not an opening at this company"


# ─── Failure and honesty ────────────────────────────────────────────────────


async def test_one_broken_form_does_not_abandon_the_other_openings(isolated, monkeypatch):
    from careeragent.storage import session_scope

    company_id, candidate_id, candidate = await _world(
        jobs=[{"id": f"j{n}", "title": f"Role {n}", "score": 0.9} for n in range(3)]
    )

    seen: list[str] = []

    async def flaky_prepare(session, application_id, cand, **kwargs):
        from careeragent.applications.service import PrepareResult

        seen.append(application_id)
        if len(seen) == 2:
            raise RuntimeError("the form would not load")
        return PrepareResult(application_id=application_id, status="awaiting_approval")

    monkeypatch.setattr(company_apply, "prepare", flaky_prepare)

    async with session_scope() as session:
        outcome = await company_apply.apply(session, company_id, candidate, candidate_id)

    assert len(seen) == 3, "the third opening must still be attempted"
    assert outcome["prepared_count"] == 2
    assert any(p["status"] == "error" for p in outcome["prepared"])
    assert "could not be prepared" in outcome["note"]


async def test_the_outcome_never_says_anything_was_submitted(isolated, monkeypatch):
    """The one claim this must never make. Applications wait for approval."""
    from careeragent.storage import session_scope

    company_id, candidate_id, candidate = await _world(
        jobs=[{"id": "j1", "title": "Role", "score": 0.9}]
    )

    async def fake_prepare(session, application_id, cand, **kwargs):
        from careeragent.applications.service import PrepareResult

        return PrepareResult(application_id=application_id, status="awaiting_approval")

    monkeypatch.setattr(company_apply, "prepare", fake_prepare)

    async with session_scope() as session:
        outcome = await company_apply.apply(session, company_id, candidate, candidate_id)

    note = outcome["note"].lower()
    assert "prepared" in note
    assert "none has been submitted" in note
    for forbidden in ("submitted 1", "applied to", "sent"):
        assert forbidden not in note


async def test_a_company_with_no_openings_explains_what_to_do(isolated):
    company_id, candidate_id, candidate = await _world(jobs=[])
    result = await _plan(company_id, candidate_id, candidate)

    assert result.openings == []
    assert "No ATS board is known" in result.note


async def test_an_unknown_company_is_an_error_not_an_empty_plan(isolated):
    from careeragent.storage import init_db, session_scope

    await init_db(drop=True)
    candidate = Candidate(name="T", email="t@example.com", facts=[])
    async with session_scope() as session:
        with pytest.raises(ValueError, match="not found"):
            await company_apply.plan(session, "no-such-company", candidate, "cand", refresh=False)


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
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
