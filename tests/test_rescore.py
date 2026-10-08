"""Recomputing stored scores, and reporting honestly about what moved.

Two things get tested here. The mechanical one — does it recompute and preserve what it
should — and the one that actually went wrong twice: **does the report attribute each change
to its real cause?**

The first version told the user that a Data Analyst role "no longer matches excel, power bi,
py, python", for 548 rows. It had aged past the freshness limit, which hard-fails the score
to zero *before* skills are examined, so `matched_skills` came back empty and the diff read
as a catastrophic loss of matching. The second version reported alias renames — "py" becoming
"python" — as skills being lost, beside scores that had gone *up*.

Both were false, both looked plausible, and both would have led someone to the wrong
conclusion about their own job search. Hence this file.
"""

from __future__ import annotations

import pytest

from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
from careeragent.models.job import RawJob
from careeragent.pipeline.normalize import normalize_all
from careeragent.pipeline.rescore import Change, Report, rescore


def _skill(label: str, *aliases: str) -> Fact:
    return Fact(
        kind=FactKind.SKILL,
        label=label,
        aliases=list(aliases),
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#page=1",
    )


# ─── Attribution ────────────────────────────────────────────────────────────


def test_an_aged_out_job_is_not_blamed_on_its_skills():
    """The bug, exactly. A hard failure zeroes the score before skills are looked at, so the
    empty `matched_skills` is an artefact of the early return — not a matching change."""
    change = Change(
        job_id="j1",
        title="Data Analyst (Finance)",
        company="Paytm",
        before=0.736,
        after=0.0,
        skills_before=["excel", "power bi", "py", "python"],
        skills_after=[],
        hard_fail="posted 33 days ago (limit 30)",
    )
    assert change.reason == "posted 33 days ago (limit 30)"
    assert change.kind == "aged_out"
    assert "no longer matches" not in change.reason


def test_a_disqualification_that_is_not_about_age_is_labelled_separately():
    change = Change(
        job_id="j2",
        title="Staff Engineer",
        company="Acme",
        before=0.5,
        after=0.0,
        hard_fail="requires 10 years; you have 0.2",
    )
    assert change.kind == "disqualified"


def test_an_alias_rename_is_not_reported_as_a_loss():
    """ "py" becoming "python" is the alias fix landing, not a skill disappearing. Reporting
    it as a loss beside a score that went up is how a report loses a reader's trust."""
    change = Change(
        job_id="j3",
        title="Platform Engineer",
        company="Quantiphi",
        before=0.554,
        after=0.773,
        skills_before=["py", "python", "tf", "tensorflow"],
        skills_after=["python", "tensorflow"],
    )
    assert change.kind == "aliases"
    assert "no longer matches" not in change.reason
    assert "counted twice" in change.reason


def test_a_genuine_loss_is_still_reported_as_one():
    """The fix must not hide real changes behind the alias explanation."""
    change = Change(
        job_id="j4",
        title="Brand Designer",
        company="Apna",
        before=0.461,
        after=0.316,
        skills_before=["python", "excel", "git"],
        skills_after=["python"],
    )
    assert change.kind == "skills"
    assert change.reason.startswith("no longer matches")
    assert "excel" in change.reason and "git" in change.reason


def test_a_genuine_gain_is_reported():
    change = Change(
        job_id="j5",
        title="Data Engineer",
        company="Acme",
        before=0.4,
        after=0.6,
        skills_before=["python"],
        skills_after=["python", "sql"],
    )
    assert change.kind == "skills"
    assert change.reason == "now matches sql"


def test_a_move_with_no_matching_change_is_attributed_to_weighting():
    change = Change(
        job_id="j6",
        title="Analyst",
        company="Acme",
        before=0.4,
        after=0.45,
        skills_before=["python"],
        skills_after=["python"],
    )
    assert change.kind == "other"
    assert change.reason == "weighting changed"


def test_the_summary_counts_each_cause_separately():
    """One combined number invites the reader to attribute all of it to whichever cause they
    happened to be thinking about. A posting ageing out and a match being corrected are not
    the same event."""
    report = Report(
        scored=1169,
        changed=548,
        rose=226,
        fell=322,
        by_kind={"aged_out": 212, "skills": 180, "other": 156},
    )
    text = report.summary()
    assert "212 because the posting is now past the freshness limit" in text
    assert "180 because the skill match itself changed" in text
    assert "156 from weighting alone" in text


def test_an_empty_run_says_so_rather_than_claiming_success():
    assert Report().summary() == "No stored scores to recompute."


# ─── The recompute itself ───────────────────────────────────────────────────


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
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


async def _seed_with_a_wrong_score() -> tuple[str, str, Candidate]:
    """A job whose stored score was computed the old, substring-matching way."""
    from datetime import date

    from careeragent.models.scoring import DeterministicScore
    from careeragent.models.scoring import JobScore as ScoreModel
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo

    await init_db(drop=True)
    candidate = Candidate(
        name="T", email="t@example.com", facts=[_skill("excel"), _skill("python")]
    )
    jobs = normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id="1",
                title="Communications Lead",
                url="https://boards.greenhouse.io/acme/jobs/1",
                company_name="Acme",
                location="Bangalore, India",
                # Mentions neither skill. "excellent" used to match "excel".
                description="We want excellent communication and a digital mindset. " * 6,
                published_at=str(date.today()),
            )
        ]
    )
    async with session_scope() as session:
        row = await repo.save_candidate_profile(session, candidate)
        await repo.replace_facts(session, row.id, candidate.facts)
        await repo.upsert_jobs(session, jobs)
        # The wrong score, as the old matcher would have stored it.
        await repo.save_score(
            session,
            row.id,
            ScoreModel(
                job_id=jobs[0].job_id,
                deterministic=DeterministicScore(
                    skills=1.0, total=0.9, matched_skills=["excel", "python"]
                ),
                final_score=0.9,
                job_fit=0.9,
            ),
        )
        return row.id, jobs[0].job_id, candidate


async def test_a_dry_run_changes_nothing(isolated):
    from sqlalchemy import select

    from careeragent.storage import session_scope
    from careeragent.storage.models import JobScore

    candidate_id, job_id, candidate = await _seed_with_a_wrong_score()
    async with session_scope() as session:
        report = await rescore(session, candidate, candidate_id, dry_run=True)
        assert report.changed == 1
        assert report.dry_run is True

    async with session_scope() as session:
        stored = (
            await session.execute(select(JobScore).where(JobScore.job_id == job_id))
        ).scalar_one()
        assert stored.final_score == pytest.approx(0.9), "a dry run wrote to the database"


async def test_applying_corrects_the_stored_score(isolated):
    from sqlalchemy import select

    from careeragent.storage import session_scope
    from careeragent.storage.models import JobScore

    candidate_id, job_id, candidate = await _seed_with_a_wrong_score()
    async with session_scope() as session:
        report = await rescore(session, candidate, candidate_id, dry_run=False)
        assert report.changed == 1
        assert report.fell == 1

    async with session_scope() as session:
        stored = (
            await session.execute(select(JobScore).where(JobScore.job_id == job_id))
        ).scalar_one()
        assert stored.final_score < 0.9
        # The skills the old matcher invented are gone from the stored breakdown too.
        assert stored.deterministic["matched_skills"] == []


async def test_paid_for_llm_scores_are_preserved(isolated):
    """Stage 3 cost money and never depended on skill matching. Discarding it would turn a
    free correction into one that has to be paid for again."""
    from sqlalchemy import select

    from careeragent.models.scoring import LLMScore
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope
    from careeragent.storage.models import JobScore

    candidate_id, job_id, candidate = await _seed_with_a_wrong_score()
    async with session_scope() as session:
        stored = (
            await session.execute(select(JobScore).where(JobScore.job_id == job_id))
        ).scalar_one()
        stored.llm = LLMScore(score=0.8, strategy="apply early").model_dump(mode="json")
        stored.semantic = {"resume_to_jd": 0.7}
        assert await repo.get_or_create_candidate(session)

    async with session_scope() as session:
        await rescore(session, candidate, candidate_id, dry_run=False)

    async with session_scope() as session:
        stored = (
            await session.execute(select(JobScore).where(JobScore.job_id == job_id))
        ).scalar_one()
        assert stored.llm["score"] == 0.8
        assert stored.llm["strategy"] == "apply early"
        assert stored.semantic["resume_to_jd"] == 0.7


async def test_nothing_is_fetched(isolated, monkeypatch):
    """The whole point of rescoring in place: no network, no quota, no LLM spend."""
    import httpx

    def explode(*_args, **_kwargs):
        raise AssertionError("rescoring made a network call")

    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    monkeypatch.setattr(httpx.AsyncClient, "request", explode)

    from careeragent.storage import session_scope

    candidate_id, _job_id, candidate = await _seed_with_a_wrong_score()
    async with session_scope() as session:
        report = await rescore(session, candidate, candidate_id, dry_run=False)
    assert report.scored == 1


# ─── Company fit must be read fresh and written back ────────────────────────
#
# Two faults found together on the live database. Rescoring read the *stored* company fit,
# so researching sixty-three companies moved nothing — every job kept the 0.0 it was first
# scored with. And the write path never persisted `company_fit` at all, so once the read
# was fixed the Jobs page showed "co=0.00" beside a score that company fit had already
# moved: a number contradicting itself on screen.


async def test_a_company_researched_since_the_last_run_reaches_the_ranking(isolated):
    """This function exists to recompute against the *current* state, and a company
    researched since the last run is part of that state."""
    from careeragent.pipeline import rescore as rescore_mod
    from careeragent.storage import session_scope
    from careeragent.storage.models import JobScore

    candidate, candidate_id, job_id = await _seed_one_job()

    # Nothing known about the employer yet.
    async with session_scope() as session:
        first = await rescore_mod.rescore(session, candidate, candidate_id)
    assert first.scored >= 1

    async with session_scope() as session:
        score = await _score_for(session, job_id, candidate_id)
        assert score.company_fit == 0.0

    # The employer is researched and scores well.
    await _set_company_fit("Acme", 0.9)

    async with session_scope() as session:
        await rescore_mod.rescore(session, candidate, candidate_id)

    async with session_scope() as session:
        score = await _score_for(session, job_id, candidate_id)
        assert score.company_fit == pytest.approx(0.9), (
            "a freshly researched company must reach the ranking"
        )
        assert isinstance(score, JobScore)


async def test_an_unresearched_company_is_left_unknown_not_scored_zero(isolated):
    """Passing 0.0 would score an unresearched company as a bad one, which is the mistake
    the whole 'exclude the unknown' rule exists to prevent."""
    from careeragent.pipeline import rescore as rescore_mod
    from careeragent.storage import session_scope
    from careeragent.storage.models import Job

    _candidate, candidate_id, job_id = await _seed_one_job()

    async with session_scope() as session:
        job = await session.get(Job, job_id)
        stored = await _score_for(session, job_id, candidate_id)
        assert rescore_mod._current_company_fit(job, {}, stored) is None, "unknown, not zero"


async def test_a_stored_fit_is_kept_when_the_company_is_not_in_the_map(isolated):
    """A company dropped from the map — renamed, merged — should not silently lose the
    fit that was already measured for it."""
    from careeragent.pipeline import rescore as rescore_mod
    from careeragent.storage import session_scope
    from careeragent.storage.models import Job

    _candidate, candidate_id, job_id = await _seed_one_job()

    async with session_scope() as session:
        job = await session.get(Job, job_id)
        stored = await _score_for(session, job_id, candidate_id)
        stored.company_fit = 0.6
        assert rescore_mod._current_company_fit(job, {}, stored) == 0.6


async def _score_for(session, job_id: str, candidate_id: str):
    from sqlalchemy import select

    from careeragent.storage.models import JobScore

    return (
        await session.execute(
            select(JobScore).where(JobScore.job_id == job_id, JobScore.candidate_id == candidate_id)
        )
    ).scalar_one()


async def _set_company_fit(name: str, fit: float) -> None:
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    async with session_scope() as session:
        company = await repo.upsert_company(session, name, f"{name.lower()}.com")
        company.fit_score = fit
        await session.flush()


async def _seed_one_job():
    """One open, scorable job at Acme. Returns (candidate, candidate_id, job_id)."""
    from datetime import date

    from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo
    from careeragent.storage.models import Job, JobScore

    await init_db(drop=True)
    candidate = Candidate(
        name="T",
        email="t@example.com",
        location="Bangalore",
        preferred_locations=["Bangalore"],
        facts=[
            Fact(
                kind=FactKind.SKILL,
                label="python",
                evidence_source=EvidenceSource.MASTER_RESUME,
                evidence_locator="cv.pdf#page=1",
            )
        ],
    )
    async with session_scope() as session:
        row = await repo.save_candidate_profile(session, candidate)
        await repo.replace_facts(session, row.id, candidate.facts)
        job = Job(
            id="job-1",
            company="Acme",
            title="Data Analyst",
            normalized_title="data analyst",
            location="Bangalore",
            description="python sql reporting " * 20,
            status="open",
            posted_date=date.today(),
        )
        session.add(job)
        session.add(JobScore(job_id=job.id, candidate_id=row.id, final_score=0.5, priority="low"))
        await session.flush()
        return candidate, row.id, job.id
