"""Career monitoring — addendum §13.

The thing that makes a monitor useful and the thing that makes it a nuisance are the same
mechanism: deciding what counts as new. Get it wrong in one direction and every sweep
re-reports the whole board, training you to ignore it. Get it wrong in the other and a
genuinely new role is missed.

So the tests here are mostly about that boundary, plus the pacing — because a monitor that
reads other people's servers unattended has to back off when a board is quiet and when it
is broken, and neither of those is something to get wrong in production and find out later.

Measured on the real database while building this: a first sweep over 8 boards found 260
new openings, and an immediate second sweep over the same 8 found zero.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from careeragent import monitoring
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
from careeragent.models.job import RawJob
from careeragent.monitoring.monitor import _reschedule, next_interval


def _settings(low: int = 6, high: int = 168) -> SimpleNamespace:
    return SimpleNamespace(
        monitor_min_interval_hours=low,
        monitor_max_interval_hours=high,
        monitor_boards_per_sweep=25,
    )


# ─── Pacing ─────────────────────────────────────────────────────────────────


def test_a_board_that_produced_something_is_checked_sooner():
    """Reacting to a new posting should be fast — companies post in batches, and a fresh
    role is worth applying to today."""
    assert next_interval(24, found_new=True, settings=_settings()) == 8


def test_a_quiet_board_is_checked_less_often():
    """And backing off is gradual, because a company quiet this week may not be next week."""
    assert next_interval(24, found_new=False, settings=_settings()) == 36


def test_the_interval_never_drops_below_the_floor():
    """Otherwise a busy board is polled into a rate limit."""
    assert next_interval(6, found_new=True, settings=_settings(low=6)) == 6
    assert next_interval(12, found_new=True, settings=_settings(low=6)) == 6


def test_the_interval_never_grows_past_the_ceiling():
    """A company that has been silent for months is still worth a weekly look."""
    assert next_interval(168, found_new=False, settings=_settings(high=168)) == 168
    assert next_interval(500, found_new=False, settings=_settings(high=168)) == 168


def test_reacting_is_faster_than_backing_off():
    """The asymmetry is the design, and it has to be a real one: a symmetric pair like
    halve-and-×1.5 moves the same distance both ways, which is not "react fast, back off
    gently". This test is why the policy divides by three rather than two."""
    settings = _settings()
    assert next_interval(48, found_new=True, settings=settings) < 48
    assert next_interval(48, found_new=False, settings=settings) > 48
    assert 48 - next_interval(48, found_new=True, settings=settings) > (
        next_interval(48, found_new=False, settings=settings) - 48
    )


# ─── Rescheduling ───────────────────────────────────────────────────────────


def _board(**kwargs):
    return SimpleNamespace(
        provider="greenhouse",
        slug="acme",
        crawl_interval_hours=kwargs.pop("crawl_interval_hours", 24),
        consecutive_failures=kwargs.pop("consecutive_failures", 0),
        last_ok_at=None,
        last_job_count=0,
        last_new_job_at=None,
        total_new_jobs_seen=0,
        next_crawl_at=None,
        **kwargs,
    )


def test_a_successful_read_clears_the_failure_count():
    board = _board(consecutive_failures=3)
    outcome = monitoring.BoardResult("greenhouse", "acme", checked=True, total_seen=12)
    _reschedule(board, outcome, _settings())

    assert board.consecutive_failures == 0
    assert board.last_ok_at is not None
    assert board.last_job_count == 12
    assert board.next_crawl_at > datetime.now(UTC)


def test_a_failed_read_counts_and_backs_off():
    """Retrying a board that just failed is how an agent gets blocked."""
    board = _board(crawl_interval_hours=12)
    outcome = monitoring.BoardResult("greenhouse", "acme", checked=False, error="timeout")
    _reschedule(board, outcome, _settings())

    assert board.consecutive_failures == 1
    assert board.crawl_interval_hours == 24
    assert board.last_ok_at is None


def test_finding_something_records_it_and_speeds_up():
    board = _board(crawl_interval_hours=24)
    outcome = monitoring.BoardResult(
        "greenhouse", "acme", checked=True, new_jobs=[{"title": "x"}, {"title": "y"}]
    )
    _reschedule(board, outcome, _settings())

    assert board.crawl_interval_hours == 8
    assert board.total_new_jobs_seen == 2
    assert board.last_new_job_at is not None


def test_a_board_gives_up_after_repeated_failure():
    """Five consecutive failures is a board that moved, not one having a bad morning."""
    assert monitoring.MAX_FAILURES == 5


# ─── Reporting ──────────────────────────────────────────────────────────────


def test_nothing_due_says_so_rather_than_claiming_success():
    assert monitoring.SweepReport().summary() == "No boards are due for a check."


def test_a_sweep_that_found_nothing_says_nothing_new():
    report = monitoring.SweepReport(boards_due=4, boards_checked=4)
    assert "Nothing new" in report.summary()


def test_failures_are_reported_not_hidden():
    report = monitoring.SweepReport(boards_due=5, boards_checked=3, failures=2)
    assert "2 board(s) could not be read" in report.summary()


def test_closed_postings_are_reported():
    report = monitoring.SweepReport(boards_due=2, boards_checked=2, closed_jobs=7)
    assert "7 posting(s) have closed" in report.summary()


def test_the_best_new_openings_come_first():
    """What you actually want out of a sweep is the top of the list, not a board-by-board
    walk."""
    report = monitoring.SweepReport(
        results=[
            monitoring.BoardResult("a", "a", new_jobs=[{"title": "low", "score": 0.2}]),
            monitoring.BoardResult(
                "b", "b", new_jobs=[{"title": "high", "score": 0.9}, {"title": "mid", "score": 0.5}]
            ),
        ]
    )
    assert [j["title"] for j in report.best_new] == ["high", "mid", "low"]


# ─── Against a real database ────────────────────────────────────────────────


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
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


async def _seed_board(next_crawl_at=None, failures: int = 0) -> tuple[str, Candidate]:
    from careeragent.storage import init_db, session_scope
    from careeragent.storage.models import ATSBoard

    await init_db(drop=True)
    candidate = Candidate(
        name="T",
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
        session.add(
            ATSBoard(
                provider="greenhouse",
                slug="acme",
                next_crawl_at=next_crawl_at,
                consecutive_failures=failures,
            )
        )
        return row.id, candidate


class FakeFetcher:
    """Returns a scripted board, and counts how many times it was asked."""

    def __init__(self, jobs):
        self.jobs = jobs
        self.calls = 0

    async def fetch_board(self, _provider, _slug, **_kwargs):
        self.calls += 1
        return SimpleNamespace(provider="greenhouse", slug="acme", jobs=self.jobs, error=None)


def _raw(source_job_id: str, title: str = "Data Analyst"):
    return RawJob(
        source="greenhouse",
        source_job_id=source_job_id,
        title=title,
        url=f"https://boards.greenhouse.io/acme/jobs/{source_job_id}",
        company_name="Acme",
        location="Bangalore, India",
        description="Python and SQL reporting work. " * 6,
    )


async def test_a_board_never_checked_is_due_immediately(isolated):
    """NULL means "never looked at", which is what makes the first sweep after enabling
    monitoring pick up every board discovered before it existed."""
    from careeragent.storage import session_scope

    await _seed_board(next_crawl_at=None)
    async with session_scope() as session:
        assert len(await monitoring.due_boards(session, 10)) == 1


async def test_a_board_checked_recently_is_not_due(isolated):
    from careeragent.storage import session_scope

    await _seed_board(next_crawl_at=datetime.now(UTC) + timedelta(hours=6))
    async with session_scope() as session:
        assert await monitoring.due_boards(session, 10) == []


async def test_a_board_given_up_on_is_left_alone(isolated):
    from careeragent.storage import session_scope

    await _seed_board(next_crawl_at=None, failures=monitoring.MAX_FAILURES)
    async with session_scope() as session:
        assert await monitoring.due_boards(session, 10) == []


async def test_a_sweep_is_bounded_by_its_limit(isolated):
    """A database with four hundred companies still has to produce a predictable amount of
    work per wake-up."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import ATSBoard

    await _seed_board()
    async with session_scope() as session:
        for i in range(10):
            session.add(ATSBoard(provider="lever", slug=f"co{i}"))
    async with session_scope() as session:
        assert len(await monitoring.due_boards(session, 4)) == 4


async def test_new_postings_are_found_and_scored(isolated):
    from careeragent.storage import session_scope
    from careeragent.storage.models import ATSBoard

    candidate_id, candidate = await _seed_board()
    fetcher = FakeFetcher([_raw("1"), _raw("2", "ML Engineer")])

    async with session_scope() as session:
        board = (await session.execute(__import__("sqlalchemy").select(ATSBoard))).scalar_one()
        result = await monitoring.check_board(
            session, board, candidate, candidate_id, fetcher=fetcher
        )

    assert result.checked
    assert len(result.new_jobs) == 2
    assert all("score" in job for job in result.new_jobs)


async def test_the_same_postings_are_not_reported_twice(isolated):
    """The test that decides whether this is useful or noise. Measured on the real database:
    a first sweep over 8 boards found 260 openings, an immediate second found zero."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import ATSBoard

    candidate_id, candidate = await _seed_board()
    fetcher = FakeFetcher([_raw("1"), _raw("2", "ML Engineer")])

    async with session_scope() as session:
        board = (await session.execute(__import__("sqlalchemy").select(ATSBoard))).scalar_one()
        first = await monitoring.check_board(
            session, board, candidate, candidate_id, fetcher=fetcher
        )
    async with session_scope() as session:
        board = (await session.execute(__import__("sqlalchemy").select(ATSBoard))).scalar_one()
        second = await monitoring.check_board(
            session, board, candidate, candidate_id, fetcher=fetcher
        )

    assert len(first.new_jobs) == 2
    assert second.new_jobs == []
    assert second.checked


async def test_a_dry_run_stores_nothing(isolated):
    from sqlalchemy import func, select

    from careeragent.storage import session_scope
    from careeragent.storage.models import ATSBoard, Job

    candidate_id, candidate = await _seed_board()
    fetcher = FakeFetcher([_raw("1")])

    async with session_scope() as session:
        board = (await session.execute(select(ATSBoard))).scalar_one()
        result = await monitoring.check_board(
            session, board, candidate, candidate_id, fetcher=fetcher, dry_run=True
        )
        assert len(result.new_jobs) == 1

    async with session_scope() as session:
        assert (await session.execute(select(func.count()).select_from(Job))).scalar_one() == 0


async def test_an_unreadable_board_is_recorded_not_raised(isolated):
    """A monitor that dies on one unreachable company stops monitoring the other three
    hundred."""
    from sqlalchemy import select

    from careeragent.storage import session_scope
    from careeragent.storage.models import ATSBoard

    candidate_id, candidate = await _seed_board()

    class Broken:
        async def fetch_board(self, *_a, **_k):
            raise TimeoutError("board is down")

    async with session_scope() as session:
        board = (await session.execute(select(ATSBoard))).scalar_one()
        result = await monitoring.check_board(
            session, board, candidate, candidate_id, fetcher=Broken()
        )

    assert not result.checked
    assert "TimeoutError" in result.error


async def test_an_unknown_provider_is_reported_rather_than_crashing(isolated):
    from sqlalchemy import select

    from careeragent.storage import session_scope
    from careeragent.storage.models import ATSBoard

    candidate_id, candidate = await _seed_board()
    async with session_scope() as session:
        board = (await session.execute(select(ATSBoard))).scalar_one()
        board.provider = "not-a-real-ats"
        result = await monitoring.check_board(
            session, board, candidate, candidate_id, fetcher=FakeFetcher([])
        )

    assert not result.checked
    assert "unknown provider" in result.error


# ─── What it must not do ────────────────────────────────────────────────────


def test_the_monitor_cannot_apply_to_anything():
    """It finds, scores and tells you. Applying stays behind a human click, so nothing in
    this module may reach the submit path."""
    import inspect

    from careeragent.monitoring import monitor

    source = inspect.getsource(monitor)
    for forbidden in ("submit", "approve", "prepare(", "BrowserRunner", "send"):
        assert forbidden not in source, f"the monitor can reach {forbidden!r}"


def test_the_background_loop_is_off_unless_switched_on(isolated):
    """A process that reaches out to other people's servers on a timer is something to turn
    on knowingly, not to find in a network log."""
    from careeragent.config import get_settings

    assert get_settings().monitor_enabled is False
    assert monitoring.start() is False
    assert monitoring.running() is False


# ─── Closing what has gone ──────────────────────────────────────────────────
#
# Found on the live database: 1528 jobs, every one of them `status="open"`, including
# postings from 342 days ago. Two faults stacked. The employer name was read from the raw
# row, which Greenhouse leaves empty, so the closing branch never ran on any board; and
# when it did run it only counted, never writing the status. The corpus could therefore
# only grow, and a year-old posting was still being offered as a live opening and then
# refused for its age.


def _raw_anonymous(source_job_id: str, title: str = ""):
    """A row shaped like the ones Greenhouse really returns: no company name on it.

    The existing fixtures all set one, which is exactly why this went unnoticed.

    The title must genuinely differ between rows: `job_id` is a fingerprint of company,
    normalized title and city, not of the source id, and `normalized_title` strips trailing
    numerals so "Data Analyst 1" and "Data Analyst 2" are one job on purpose.
    """
    title = title or {"1": "Data Analyst", "2": "ML Engineer", "3": "Backend Developer"}.get(
        source_job_id, f"Role {source_job_id}"
    )
    return RawJob(
        source="greenhouse",
        source_job_id=source_job_id,
        title=title,
        url=f"https://boards.greenhouse.io/acme/jobs/{source_job_id}",
        company_name=None,
        board_slug="acme",
        location="Bangalore, India",
        description="Python and SQL reporting work. " * 6,
    )


async def _read(candidate_id, candidate, fetcher, **kwargs):
    from sqlalchemy import select

    from careeragent.storage import session_scope
    from careeragent.storage.models import ATSBoard

    async with session_scope() as session:
        board = (await session.execute(select(ATSBoard))).scalar_one()
        return await monitoring.check_board(
            session, board, candidate, candidate_id, fetcher=fetcher, **kwargs
        )


async def _statuses():
    from sqlalchemy import select

    from careeragent.storage import session_scope
    from careeragent.storage.models import Job

    async with session_scope() as session:
        return {row[0]: row[1] for row in (await session.execute(select(Job.id, Job.status))).all()}


async def test_the_employer_is_read_from_the_normalized_row_not_the_raw_one(isolated):
    """Greenhouse puts no company name on a posting; it is derived from the board."""
    candidate_id, candidate = await _seed_board()
    result = await _read(candidate_id, candidate, FakeFetcher([_raw_anonymous("1")]))

    assert result.company, "an empty company name silently disables the closing pass"


async def test_a_posting_that_has_left_the_board_is_marked_closed(isolated):
    candidate_id, candidate = await _seed_board()

    await _read(candidate_id, candidate, FakeFetcher([_raw_anonymous("1"), _raw_anonymous("2")]))
    before = await _statuses()
    assert set(before.values()) == {"open"}

    # The second posting is gone from the feed.
    second = await _read(candidate_id, candidate, FakeFetcher([_raw_anonymous("1")]))

    after = await _statuses()
    assert second.closed_jobs == 1
    assert sorted(after.values()) == ["closed", "open"]


async def test_a_closed_posting_is_kept_not_deleted(isolated):
    """An application already sent against it is still a real thing that happened."""
    from sqlalchemy import func, select

    from careeragent.storage import session_scope
    from careeragent.storage.models import Job

    candidate_id, candidate = await _seed_board()
    await _read(candidate_id, candidate, FakeFetcher([_raw_anonymous("1"), _raw_anonymous("2")]))
    await _read(candidate_id, candidate, FakeFetcher([]))

    async with session_scope() as session:
        assert (await session.execute(select(func.count()).select_from(Job))).scalar_one() == 2


async def test_a_posting_that_comes_back_is_opened_again(isolated):
    """Pulled and reposted, or missed by one page of a paginated read."""
    candidate_id, candidate = await _seed_board()

    await _read(candidate_id, candidate, FakeFetcher([_raw_anonymous("1"), _raw_anonymous("2")]))
    await _read(candidate_id, candidate, FakeFetcher([_raw_anonymous("1")]))
    assert sorted((await _statuses()).values()) == ["closed", "open"]

    again = await _read(
        candidate_id, candidate, FakeFetcher([_raw_anonymous("1"), _raw_anonymous("2")])
    )

    assert again.reopened_jobs == 1
    assert set((await _statuses()).values()) == {"open"}
    assert again.new_jobs == [], "a returning posting is not a new one"


async def test_an_empty_feed_closes_nothing(isolated):
    """A board returning zero jobs is indistinguishable from a broken one. Closing every
    posting at that employer on the strength of it would be worse than doing nothing."""
    candidate_id, candidate = await _seed_board()
    await _read(candidate_id, candidate, FakeFetcher([_raw_anonymous("1"), _raw_anonymous("2")]))

    result = await _read(candidate_id, candidate, FakeFetcher([]))

    assert result.closed_jobs == 0
    assert set((await _statuses()).values()) == {"open"}


async def test_a_dry_run_reports_closures_without_writing_them(isolated):
    candidate_id, candidate = await _seed_board()
    await _read(candidate_id, candidate, FakeFetcher([_raw_anonymous("1"), _raw_anonymous("2")]))

    result = await _read(candidate_id, candidate, FakeFetcher([_raw_anonymous("1")]), dry_run=True)

    assert result.closed_jobs == 1
    assert set((await _statuses()).values()) == {"open"}, "a dry run must change nothing"


async def test_one_employer_on_a_shared_board_does_not_close_another(isolated):
    """A generic slug like `rippling:careers` carries several employers. Closing all of
    one company's postings because a different company's are missing would be wrong."""
    from sqlalchemy import select

    from careeragent.storage import session_scope
    from careeragent.storage.models import Job

    candidate_id, candidate = await _seed_board()

    def _for(company: str, source_job_id: str):
        return RawJob(
            source="greenhouse",
            source_job_id=source_job_id,
            title={"1": "Data Analyst", "2": "ML Engineer", "3": "Backend Developer"}[
                source_job_id
            ],
            url=f"https://boards.greenhouse.io/shared/jobs/{source_job_id}",
            company_name=company,
            board_slug="shared",
            location="Bangalore, India",
            description="Python and SQL reporting work. " * 6,
        )

    await _read(
        candidate_id,
        candidate,
        FakeFetcher([_for("Acme", "1"), _for("Beta", "2"), _for("Beta", "3")]),
    )
    # Beta drops one posting. Acme is absent from this read entirely.
    result = await _read(candidate_id, candidate, FakeFetcher([_for("Beta", "2")]))

    async with session_scope() as session:
        rows = {
            row[0]: row[1] for row in (await session.execute(select(Job.company, Job.status))).all()
        }

    assert result.closed_jobs == 1, "only Beta's missing posting"
    assert rows["Acme"] == "open", "an employer not in this read must be left alone"
