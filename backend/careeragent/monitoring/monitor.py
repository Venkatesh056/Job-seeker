"""Career monitoring — keeping the job data from going stale. Addendum §13.

Discovery was a one-shot. It found 1,169 jobs, scored them, and then nothing read those
boards again — so within a month 212 of them had quietly expired while still sitting at
the top of the ranking. A career agent that only looks once is a snapshot, not an agent.

This is the loop that re-reads the boards already resolved, notices what is new, notices
what has gone, and scores the new arrivals. It is the difference between a system you run
and a system that runs.

**Adaptive cadence, not a cron.** Every board carries its own interval. A board that keeps
producing new roles earns a shorter one; a board static for a fortnight earns a longer one.
A fixed schedule gets both halves wrong at once — hammering companies that post yearly while
missing the ones that post weekly. The band is 6 hours to a week, and the arithmetic is in
`next_interval()`, which is a pure function so the policy can be read and tested without a
network.

**A sweep is bounded.** A fixed number of boards per wake-up, with the most overdue first,
so a database that has accumulated four hundred companies still produces a predictable
amount of work per tick rather than a thundering herd.

**No LLM by default.** New jobs are scored deterministically. Stage 2 and stage 3 cost money
and a monitor runs unattended; paying for reasoning on every new posting a hundred companies
produce overnight is how an unattended loop becomes an unattended bill. The strong stages run
when you look at a job, not when the monitor finds it.

**It never applies to anything.** The monitor finds, scores and tells you. Applying stays
where it is — behind your click.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..events import EventType, Level, get_bus
from ..logging import get_logger
from ..storage.models import ATSBoard, Job

log = get_logger("monitoring")

# A board that has failed this many times in a row is not read again until something
# re-resolves it. Five consecutive failures is a board that moved, not a board having a
# bad morning.
MAX_FAILURES = 5


def next_interval(
    current_hours: int,
    *,
    found_new: bool,
    settings: Any = None,
) -> int:
    """How long to wait before reading this board again.

    A third of the gap on a find, half again on a quiet read. Pure, so the pacing policy is
    readable and testable without touching a network or a clock.

    The asymmetry is the point, and it has to be a real asymmetry: halving on a find while
    multiplying by 1.5 on a miss moves the same distance in both directions, which is not
    what "react fast, back off gently" means. A test caught that.

    Dividing by three is right because companies post in batches — one new role usually
    means more this week — and a fresh posting is worth applying to today. Backing off stays
    gentle because a company quiet this week may not be next week.
    """
    settings = settings or get_settings()
    low = max(1, settings.monitor_min_interval_hours)
    high = max(low, settings.monitor_max_interval_hours)
    nxt = current_hours // 3 if found_new else int(current_hours * 1.5)
    return max(low, min(nxt, high))


@dataclass
class BoardResult:
    """What one board produced this sweep."""

    provider: str
    slug: str
    company: str = ""
    checked: bool = False
    new_jobs: list[dict[str, Any]] = field(default_factory=list)
    closed_jobs: int = 0
    # A posting that was closed and is on the board again — pulled and reposted, or missed
    # by one paginated read. Reported separately so it is not mistaken for a new opening.
    reopened_jobs: int = 0
    total_seen: int = 0
    error: str = ""
    next_in_hours: int = 24

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "slug": self.slug,
            "company": self.company,
            "checked": self.checked,
            "new": len(self.new_jobs),
            "new_jobs": self.new_jobs[:10],
            "closed": self.closed_jobs,
            "reopened": self.reopened_jobs,
            "total_seen": self.total_seen,
            "error": self.error or None,
            "next_in_hours": self.next_in_hours,
        }


@dataclass
class SweepReport:
    boards_due: int = 0
    boards_checked: int = 0
    new_jobs: int = 0
    closed_jobs: int = 0
    reopened_jobs: int = 0
    failures: int = 0
    results: list[BoardResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def best_new(self) -> list[dict[str, Any]]:
        """New openings across all boards, best score first — what you actually want."""
        everything = [job for result in self.results for job in result.new_jobs]
        everything.sort(key=lambda job: job.get("score") or 0.0, reverse=True)
        return everything

    def to_dict(self) -> dict[str, Any]:
        return {
            "boards_due": self.boards_due,
            "boards_checked": self.boards_checked,
            "new_jobs": self.new_jobs,
            "closed_jobs": self.closed_jobs,
            "reopened_jobs": self.reopened_jobs,
            "failures": self.failures,
            "dry_run": self.dry_run,
            "best_new": self.best_new[:20],
            "boards": [r.to_dict() for r in self.results],
        }

    def summary(self) -> str:
        if not self.boards_due:
            return "No boards are due for a check."
        if not self.boards_checked:
            return f"{self.boards_due} board(s) due, none could be read."
        parts = [f"Checked {self.boards_checked} of {self.boards_due} board(s) due."]
        parts.append(f"{self.new_jobs} new opening(s)." if self.new_jobs else "Nothing new.")
        if self.closed_jobs:
            parts.append(f"{self.closed_jobs} posting(s) have closed.")
        if self.reopened_jobs:
            parts.append(f"{self.reopened_jobs} are back on a board.")
        if self.failures:
            parts.append(f"{self.failures} board(s) could not be read.")
        return " ".join(parts)


# ─── Choosing what to read ──────────────────────────────────────────────────


async def due_boards(session: AsyncSession, limit: int) -> list[ATSBoard]:
    """Boards worth reading now, most overdue first.

    A board that has never been monitored has `next_crawl_at` of NULL and is treated as due
    immediately — which is what makes the first sweep after enabling this pick up everything
    that was discovered before monitoring existed.
    """
    now = datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(ATSBoard)
                .where(
                    ATSBoard.consecutive_failures < MAX_FAILURES,
                    (ATSBoard.next_crawl_at.is_(None)) | (ATSBoard.next_crawl_at <= now),
                )
                .order_by(ATSBoard.next_crawl_at.asc().nullsfirst())
                .limit(max(1, limit))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ─── Reading one board ──────────────────────────────────────────────────────


async def check_board(
    session: AsyncSession,
    board: ATSBoard,
    candidate: Any,
    candidate_id: str,
    *,
    fetcher: Any,
    dry_run: bool = False,
) -> BoardResult:
    """Read one board, find what changed, score the new arrivals.

    Never raises. A monitor that dies on one unreachable company stops monitoring the other
    three hundred, so every failure is caught, counted and scheduled for a later retry.
    """
    from .. import preferences
    from ..pipeline.normalize import normalize_all
    from ..pipeline.opportunity import score_opportunity
    from ..pipeline.score import ScoreContext, score_job
    from ..sources.ats.providers import PROVIDERS_BY_ID
    from ..storage import repository as repo

    result = BoardResult(provider=board.provider, slug=board.slug)

    provider = PROVIDERS_BY_ID.get(board.provider)
    if provider is None:
        result.error = f"unknown provider {board.provider!r}"
        return result

    try:
        fetched = await fetcher.fetch_board(provider, board.slug)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"[:160]
        return result
    if fetched.error:
        result.error = str(fetched.error)[:160]
        return result

    result.checked = True
    result.total_seen = len(fetched.jobs)

    normalized = normalize_all(fetched.jobs)
    live_ids = {job.job_id for job in normalized if job.job_id}

    # The employer name comes from the normalized row, not the raw one. Most providers —
    # Greenhouse among them — do not put a company name on a posting at all; it is derived
    # from the board during normalization. Reading it from the raw row left this empty on
    # every board, which silently disabled the closing pass below.
    if normalized and normalized[0].company:
        result.company = normalized[0].company

    # Which of these we have seen before, asked by primary key rather than by guessing at a
    # company name. `job_id` is a content fingerprint, so the same posting read twice is the
    # same id and a genuinely new posting is a genuinely new one.
    seen_before = {
        row[0]
        for row in (await session.execute(select(Job.id).where(Job.id.in_(live_ids or {""})))).all()
    }
    fresh = [job for job in normalized if job.job_id not in seen_before]

    # Postings that were on this board before and are not in the feed now.
    #
    # Marked closed, never deleted: an application already sent against one of them is
    # still a real thing that happened, and the tracker has to keep it. Closing is what
    # stops a year-old posting being offered as a live opening and then refused for its
    # age — the state the whole corpus was in before this worked.
    #
    # Scoped to the employers this fetch actually returned rather than to one name, because
    # a board slug is not always one company: a generic slug like `rippling:careers` carries
    # several, and closing all of the first one's jobs because the others are missing would
    # be worse than not closing at all.
    companies = {job.company for job in normalized if job.company}
    if companies and live_ids:
        held = list(
            (
                await session.execute(
                    select(Job).where(Job.ats == board.provider, Job.company.in_(companies))
                )
            )
            .scalars()
            .all()
        )
        vanished = [row for row in held if row.id not in live_ids and row.status == "open"]
        returned = [row for row in held if row.id in live_ids and row.status != "open"]
        result.closed_jobs = len(vanished)
        result.reopened_jobs = len(returned)

        if not dry_run:
            for row in vanished:
                row.status = "closed"
            # A posting pulled and reposted, or missing from one page of a paginated feed
            # last time. It is on the board now, so it is open now.
            for row in returned:
                row.status = "open"

    if dry_run:
        result.new_jobs = [
            {"title": job.title, "company": job.company, "url": job.primary_url} for job in fresh
        ]
        return result

    if fresh:
        await repo.upsert_jobs(session, fresh)
        # The stored preference, not an empty one. Without this the monitor announces
        # senior architect roles on the other side of the world to someone looking for a
        # local internship, which is how a notification feed becomes noise.
        context = ScoreContext(
            candidate=candidate, config=await preferences.load(session, candidate_id)
        )
        for job in fresh:
            deterministic = score_job(job, context)
            scored = score_opportunity(job, deterministic)
            await repo.save_score(session, candidate_id, scored)
            result.new_jobs.append(
                {
                    "job_id": job.job_id,
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "url": job.primary_url,
                    "score": round(scored.final_score, 4),
                    "priority": scored.priority.value,
                    "matched": list(deterministic.matched_skills)[:6],
                }
            )

    return result


# ─── One sweep ──────────────────────────────────────────────────────────────


async def sweep(
    session: AsyncSession,
    candidate: Any,
    candidate_id: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    concurrency: int = 4,
) -> SweepReport:
    """Read every board that is due, and report what changed.

    Concurrency is modest on purpose. These are other people's servers, being read on a
    schedule with nobody watching, and a monitor that behaves like a crawler is a monitor
    that gets blocked.
    """
    from ..sources.ats import ATSFetcher

    settings = get_settings()
    report = SweepReport(dry_run=dry_run)
    boards = await due_boards(session, limit or settings.monitor_boards_per_sweep)
    report.boards_due = len(boards)
    if not boards:
        return report

    log.info("monitor_sweep_started", due=len(boards), dry_run=dry_run)
    gate = asyncio.Semaphore(max(1, concurrency))

    async with ATSFetcher() as fetcher:

        async def one(board: ATSBoard) -> BoardResult:
            async with gate:
                return await check_board(
                    session,
                    board,
                    candidate,
                    candidate_id,
                    fetcher=fetcher,
                    dry_run=dry_run,
                )

        # Sequential inside the semaphore rather than a gather over one session: SQLAlchemy
        # async sessions are not safe to share across concurrent tasks, and the fetch is the
        # slow part anyway.
        for board in boards:
            outcome = await one(board)
            report.results.append(outcome)

            if outcome.checked:
                report.boards_checked += 1
                report.new_jobs += len(outcome.new_jobs)
                report.closed_jobs += outcome.closed_jobs
                report.reopened_jobs += outcome.reopened_jobs
            else:
                report.failures += 1

            if not dry_run:
                _reschedule(board, outcome, settings)

    if not dry_run:
        await session.flush()
        await _announce(report)

    log.info(
        "monitor_sweep_finished",
        checked=report.boards_checked,
        new=report.new_jobs,
        closed=report.closed_jobs,
        failures=report.failures,
    )
    return report


def _reschedule(board: ATSBoard, outcome: BoardResult, settings: Any) -> None:
    """Update the board's cadence from what this read found."""
    now = datetime.now(UTC)
    if not outcome.checked:
        board.consecutive_failures += 1
        # Backed off rather than retried immediately: a board that just failed is usually
        # still failing a minute later, and retrying hard is how an agent gets blocked.
        hours = min(
            settings.monitor_max_interval_hours,
            max(settings.monitor_min_interval_hours, board.crawl_interval_hours * 2),
        )
    else:
        board.consecutive_failures = 0
        board.last_ok_at = now
        board.last_job_count = outcome.total_seen
        hours = next_interval(
            board.crawl_interval_hours or 24,
            found_new=bool(outcome.new_jobs),
            settings=settings,
        )
        if outcome.new_jobs:
            board.last_new_job_at = now
            board.total_new_jobs_seen = (board.total_new_jobs_seen or 0) + len(outcome.new_jobs)

    board.crawl_interval_hours = hours
    board.next_crawl_at = now + timedelta(hours=hours)
    outcome.next_in_hours = hours


async def _announce(report: SweepReport) -> None:
    """Put the interesting findings on the event feed.

    Only the openings worth looking at. A monitor that emits an event for every posting a
    hundred companies produce overnight trains you to ignore the feed, which costs you the
    one notification that mattered.
    """
    bus = get_bus()
    notable = [job for job in report.best_new if (job.get("score") or 0) >= 0.5][:10]
    for job in notable:
        await bus.emit_one(
            EventType.JOB_FOUND,
            f"New: {job['title']} at {job['company']} ({job['score']:.0%} match)",
            job_id=job.get("job_id"),
            level=Level.INFO,
            score=job.get("score"),
            monitored=True,
        )
    if report.closed_jobs:
        await bus.emit_one(
            EventType.JOB_REJECTED,
            f"{report.closed_jobs} posting(s) have closed since the last check",
            level=Level.INFO,
        )


# ─── The background loop ────────────────────────────────────────────────────


_task: asyncio.Task | None = None


async def _loop() -> None:
    """Wake on a tick, sweep whatever is due, sleep again.

    Every failure mode here has to be survivable: this runs for days unattended, and a loop
    that dies on one bad sweep leaves the user with stale data and no error they will ever
    see.
    """
    from ..storage import repository as repo
    from ..storage import session_scope

    settings = get_settings()
    tick = max(60, settings.monitor_tick_minutes * 60)
    log.info("monitor_started", tick_minutes=settings.monitor_tick_minutes)

    while True:
        try:
            await asyncio.sleep(tick)
            async with session_scope() as session:
                candidate = await repo.load_candidate(session)
                if candidate is None:
                    continue
                row = await repo.get_or_create_candidate(session)
                await sweep(session, candidate, row.id)
        except asyncio.CancelledError:
            log.info("monitor_stopped")
            raise
        except Exception as exc:
            log.warning("monitor_sweep_failed", error=f"{type(exc).__name__}: {exc}"[:160])


def start() -> bool:
    """Start the background monitor. Returns whether it started.

    Off unless `monitor_enabled` is set: a process that reaches out to other people's
    servers on a timer is something to switch on knowingly, not to find in a network log.
    """
    global _task
    if not get_settings().monitor_enabled:
        return False
    if _task is not None and not _task.done():
        return True
    _task = asyncio.create_task(_loop())
    return True


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    # Both cases are expected on shutdown: the cancellation we just asked for, and whatever
    # the loop was in the middle of when it was cancelled. Neither should stop the process
    # from closing.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _task
    _task = None


def running() -> bool:
    return _task is not None and not _task.done()


async def status(session: AsyncSession) -> dict[str, Any]:
    """What the monitor is doing and what it is watching."""
    from sqlalchemy import func

    settings = get_settings()
    now = datetime.now(UTC)

    total = int(
        (await session.execute(select(func.count()).select_from(ATSBoard))).scalar_one() or 0
    )
    due = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ATSBoard)
                .where(
                    ATSBoard.consecutive_failures < MAX_FAILURES,
                    (ATSBoard.next_crawl_at.is_(None)) | (ATSBoard.next_crawl_at <= now),
                )
            )
        ).scalar_one()
        or 0
    )
    dead = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ATSBoard)
                .where(ATSBoard.consecutive_failures >= MAX_FAILURES)
            )
        ).scalar_one()
        or 0
    )
    upcoming = (
        (
            await session.execute(
                select(ATSBoard)
                .where(ATSBoard.next_crawl_at.isnot(None))
                .order_by(ATSBoard.next_crawl_at.asc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )

    return {
        "enabled": settings.monitor_enabled,
        "running": running(),
        "tick_minutes": settings.monitor_tick_minutes,
        "boards_per_sweep": settings.monitor_boards_per_sweep,
        "interval_band_hours": [
            settings.monitor_min_interval_hours,
            settings.monitor_max_interval_hours,
        ],
        "boards_watched": total,
        "boards_due_now": due,
        "boards_given_up_on": dead,
        "next_checks": [
            {
                "provider": b.provider,
                "slug": b.slug,
                "at": b.next_crawl_at.isoformat() if b.next_crawl_at else None,
                "every_hours": b.crawl_interval_hours,
                "new_jobs_seen": b.total_new_jobs_seen,
            }
            for b in upcoming
        ],
    }
