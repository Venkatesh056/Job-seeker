"""Applying to a company rather than to a job.

"I want to work at Quantiphi" is a different request from "I want this Quantiphi job",
and until now the product could only answer the second one. This answers the first:
find every opening the company has, drop the ones that are not worth applying to, rank
what is left, and prepare an application — with its own tailored resume — for each.

Three constraints shape the design, and all three exist to stop this becoming a
mass-application button.

**It plans before it acts.** `plan()` does the discovery, filtering and ranking and
returns what *would* happen, changing nothing. Nothing is prepared until `apply()` is
called with the openings the user actually chose. A fan-out that starts working the
moment it is clicked is one the user cannot review, and the review is the point.

**The daily cap still binds.** `settings.daily_application_cap` counts submissions per
day across the whole system, and a company fan-out is not an exception to it. The plan
says up front how many of the openings the cap allows today, so "14 openings, 5 today"
is visible before the click rather than discovered afterwards.

**Every opening is tailored separately.** A single resume sent to fourteen openings is
the thing that makes bulk applications worthless. Each opening gets its own application
row, its own tailoring run against that job's description, and its own approval.

Nothing here submits anything. Each prepared application lands in the same approval
queue a single application does, and a human still decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..logging import get_logger
from ..models.candidate import Candidate
from ..sources.directory.base import normalize_company
from ..storage import repository as repo
from ..storage.models import ATSBoard, Company, Job, JobScore
from .service import PrepareResult, prepare, shortlist

log = get_logger("applications.company_apply")

# How many openings at one company we will ever put in front of the user at once.
# Not a cap on the company's size — a cap on a list a person can actually read.
MAX_OPENINGS = 25

# How many of the company's boards a refresh will read. A company with more boards than
# this is an aggregator, and reading all of them is a crawl.
MAX_BOARDS = 5


@dataclass
class Opening:
    """One of the company's jobs, and whether we would apply to it."""

    job_id: str
    title: str
    location: str
    score: float
    priority: str
    # Empty when we would apply. Otherwise the reason, in the user's words.
    skipped: str = ""
    application_id: str = ""

    @property
    def applyable(self) -> bool:
        return not self.skipped

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "location": self.location,
            "score": round(self.score, 3),
            "priority": self.priority,
            "skipped": self.skipped,
            "applyable": self.applyable,
            "application_id": self.application_id,
        }


@dataclass
class CompanyPlan:
    """What applying to this company would mean, before anything is done."""

    company_id: str
    company: str
    openings: list[Opening] = field(default_factory=list)
    # How many the company actually has. `openings` is capped at MAX_OPENINGS for a list
    # a person can read, and a note that counts one thing while the list shows another is
    # how a status page stops being believed.
    total_openings: int = 0
    boards_checked: int = 0
    newly_discovered: int = 0
    # How many more applications today's cap allows, across the whole system.
    remaining_today: int = 0
    daily_cap: int = 0
    note: str = ""

    @property
    def applyable(self) -> list[Opening]:
        return [o for o in self.openings if o.applyable]

    @property
    def would_prepare_now(self) -> list[Opening]:
        """The ones the daily cap leaves room for today."""
        return self.applyable[: max(0, self.remaining_today)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "company": self.company,
            "openings": [o.to_dict() for o in self.openings],
            "total_openings": self.total_openings or len(self.openings),
            "showing": len(self.openings),
            "applyable": len(self.applyable),
            "skipped": len(self.openings) - len(self.applyable),
            "would_prepare_now": [o.job_id for o in self.would_prepare_now],
            "boards_checked": self.boards_checked,
            "newly_discovered": self.newly_discovered,
            "remaining_today": self.remaining_today,
            "daily_cap": self.daily_cap,
            "note": self.note,
        }


# ─── Finding the company's openings ─────────────────────────────────────────


async def _company_jobs(session: AsyncSession, company: Company) -> list[Job]:
    """Every open job we hold for this company.

    Matched on `company_id` first, then on the normalised name, because jobs arriving
    from a directory source carry a name and no id while jobs arriving from an ATS board
    carry the id. Asking only by id would silently miss half the openings at exactly the
    companies the user cares most about.
    """
    by_id = list(
        (
            await session.execute(
                select(Job).where(Job.company_id == company.id, Job.status == "open")
            )
        )
        .scalars()
        .all()
    )

    key = normalize_company(company.name)
    by_name = (
        list(
            (
                await session.execute(
                    select(Job).where(Job.company.ilike(f"%{company.name}%"), Job.status == "open")
                )
            )
            .scalars()
            .all()
        )
        if key
        else []
    )

    seen: dict[str, Job] = {job.id: job for job in by_id}
    for job in by_name:
        # ilike is a prefilter, not the test: "Acme" would otherwise match "Acme Staffing".
        if job.id not in seen and normalize_company(job.company) == key:
            seen[job.id] = job
    return list(seen.values())


async def _refresh_boards(
    session: AsyncSession, company: Company, candidate: Candidate, candidate_id: str
) -> tuple[int, int]:
    """Re-read the company's ATS boards so the plan reflects today, not last week.

    Returns (boards checked, jobs newly discovered). Never raises: a company whose board
    is unreachable should still produce a plan from the openings already held, with the
    staleness visible, rather than an error page.
    """
    from ..monitoring.monitor import MAX_FAILURES, check_board
    from ..sources.ats import ATSFetcher

    # Read now regardless of the crawl schedule — the user asked for this company by
    # name — but still skip a board we have given up on, which is the one case where
    # reading again is known to be pointless.
    boards = list(
        (
            await session.execute(
                select(ATSBoard)
                .where(
                    ATSBoard.company_id == company.id,
                    ATSBoard.consecutive_failures < MAX_FAILURES,
                )
                .order_by(ATSBoard.last_job_count.desc())
                .limit(MAX_BOARDS)
            )
        )
        .scalars()
        .all()
    )
    if not boards:
        return 0, 0

    checked = discovered = 0
    async with ATSFetcher() as fetcher:
        for board in boards:
            outcome = await check_board(
                session, board, candidate, candidate_id, fetcher=fetcher, dry_run=False
            )
            if outcome.checked:
                checked += 1
                discovered += len(outcome.new_jobs)
            else:
                log.info(
                    "company_board_unreadable",
                    company=company.name,
                    provider=board.provider,
                    error=outcome.error,
                )
    await session.flush()
    return checked, discovered


# ─── The plan ───────────────────────────────────────────────────────────────


async def plan(
    session: AsyncSession,
    company_id: str,
    candidate: Candidate,
    candidate_id: str,
    *,
    refresh: bool = True,
    min_score: float = 0.0,
) -> CompanyPlan:
    """What applying to this company would mean. Changes nothing.

    `refresh=False` skips re-reading the boards, which makes this instant and answers
    from what is already held — the right choice when the user is re-opening a plan they
    just looked at.
    """
    settings = get_settings()

    company = await repo.get_company(session, company_id)
    if company is None:
        raise ValueError(f"company {company_id} not found")

    result = CompanyPlan(company_id=company.id, company=company.name)

    if refresh:
        result.boards_checked, result.newly_discovered = await _refresh_boards(
            session, company, candidate, candidate_id
        )

    jobs = await _company_jobs(session, company)
    if not jobs:
        result.note = f"No open postings held for {company.name}. " + (
            "Its careers board was read and had none."
            if result.boards_checked
            else "No ATS board is known for it — discover one first, and its openings "
            "will appear here."
        )
        return result

    scores = {
        row.job_id: row
        for row in (
            await session.execute(
                select(JobScore).where(
                    JobScore.candidate_id == candidate_id,
                    JobScore.job_id.in_([job.id for job in jobs]),
                )
            )
        )
        .scalars()
        .all()
    }

    openings: list[Opening] = []
    for job in jobs:
        score = scores.get(job.id)
        opening = Opening(
            job_id=job.id,
            title=job.title,
            location=job.location or "",
            score=float(score.final_score) if score else 0.0,
            priority=score.priority if score else "unscored",
        )

        if score is None:
            opening.skipped = "not scored yet — run a scoring pass and it will appear here"
        elif score.priority == "reject":
            # A rejection has two quite different causes and they deserve different words.
            # A hard fail is a rule the posting broke — "posted 49 days ago (limit 30)" —
            # and the scorer already phrased it. Everything else is simply a weak match,
            # and saying "does not match what you want" about a 0.15 hides the fact that
            # the number, not a rule, is what refused it.
            hard_fail = (score.deterministic or {}).get("hard_fail")
            opening.skipped = hard_fail or (
                f"scores {score.final_score:.2f} — too weak a match to be worth applying to"
            )
        elif await repo.already_applied(session, job.id, candidate_id):
            opening.skipped = "already applied"
        elif score.final_score < min_score:
            opening.skipped = f"scores {score.final_score:.2f}, below your {min_score:.2f} floor"

        openings.append(opening)

    # Applyable first and best-first within that, so the openings the user is being asked
    # to decide about are at the top and the skipped ones remain visible underneath.
    openings.sort(key=lambda o: (o.applyable is False, -o.score))
    result.total_openings = len(openings)
    result.openings = openings[:MAX_OPENINGS]

    submitted = await repo.applications_submitted_today(session, candidate_id)
    result.daily_cap = settings.daily_application_cap
    result.remaining_today = max(0, settings.daily_application_cap - submitted)

    applyable = len(result.applyable)
    trimmed = (
        f" Showing the top {len(result.openings)} of {result.total_openings}."
        if result.total_openings > len(result.openings)
        else ""
    )
    if applyable == 0:
        result.note = (
            f"{result.total_openings} opening(s) at {company.name}, none of them worth "
            f"applying to right now — the reason is on each row.{trimmed}"
        )
    elif result.remaining_today == 0:
        result.note = (
            f"{applyable} opening(s) worth applying to, but today's cap of "
            f"{result.daily_cap} is already used. They will still be here tomorrow."
        )
    elif applyable > result.remaining_today:
        result.note = (
            f"{applyable} opening(s) worth applying to. Today's cap of {result.daily_cap} "
            f"leaves room for {result.remaining_today}; the rest stay in the plan."
        )
    else:
        result.note = f"{applyable} opening(s) worth applying to, all within today's cap.{trimmed}"

    return result


# ─── Acting on it ───────────────────────────────────────────────────────────


async def apply(
    session: AsyncSession,
    company_id: str,
    candidate: Candidate,
    candidate_id: str,
    *,
    job_ids: list[str] | None = None,
    use_llm: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Prepare an application per opening. Submits nothing.

    `job_ids` names the openings to prepare; omitting it takes the plan's own choice,
    which is the applyable openings in rank order. Either way every id is re-checked
    against the current plan, so a stale page cannot prepare an opening that has since
    been filled, hard-failed, or already been applied to.

    Sequential, like `prepare_batch`, and for the same reason: several Chromium instances
    filling forms at once produces work nobody can review.
    """
    settings = get_settings()
    current = await plan(session, company_id, candidate, candidate_id, refresh=False)
    allowed = {o.job_id: o for o in current.applyable}

    # True when the cap is why some applyable opening is not being prepared — set here
    # as well as in the loop, because taking the plan's own choice trims the list to the
    # cap *before* the loop, and reporting `capped: false` in that case would mean the
    # user was never told why three of their openings were left out.
    capped = False

    if job_ids is None:
        chosen = [o.job_id for o in current.would_prepare_now]
        capped = len(chosen) < len(current.applyable)
        refused: list[dict[str, str]] = []
    else:
        chosen = [job_id for job_id in job_ids if job_id in allowed]
        refused = [
            {
                "job_id": job_id,
                "reason": next(
                    (o.skipped for o in current.openings if o.job_id == job_id),
                    "not an opening at this company",
                ),
            }
            for job_id in job_ids
            if job_id not in allowed
        ]

    if limit is not None:
        chosen = chosen[: max(0, limit)]

    prepared: list[PrepareResult] = []
    for job_id in chosen:
        submitted = await repo.applications_submitted_today(session, candidate_id)
        if submitted >= settings.daily_application_cap:
            capped = True
            log.info("company_apply_capped", company=current.company, submitted=submitted)
            break
        try:
            application = await shortlist(session, job_id, candidate_id)
            prepared.append(await prepare(session, application.id, candidate, use_llm=use_llm))
        except Exception as exc:
            # One bad form must not abandon the other openings at the same company.
            prepared.append(
                PrepareResult(application_id="", status="error", error=f"{job_id}: {exc}"[:300])
            )

    ok = [r for r in prepared if r.status != "error"]
    log.info(
        "company_apply_finished",
        company=current.company,
        chosen=len(chosen),
        prepared=len(ok),
        refused=len(refused),
        capped=capped,
    )

    return {
        "company_id": current.company_id,
        "company": current.company,
        "prepared": [
            {"application_id": r.application_id, "status": r.status, "error": r.error}
            for r in prepared
        ],
        "prepared_count": len(ok),
        "refused": refused,
        "capped": capped,
        "note": _outcome_note(current, prepared, refused, capped=capped),
    }


def _outcome_note(
    current: CompanyPlan,
    prepared: list[PrepareResult],
    refused: list[dict[str, str]],
    *,
    capped: bool,
) -> str:
    """What happened, in one sentence, without overclaiming.

    Nothing here was submitted, and the wording must never suggest it was.
    """
    ok = [r for r in prepared if r.status != "error"]
    failed = len(prepared) - len(ok)

    if not prepared:
        if refused:
            return "Nothing was prepared — none of the openings you chose is still applyable."
        return f"Nothing to prepare at {current.company} right now."

    parts = [
        f"Prepared {len(ok)} application(s) at {current.company}, each with its own "
        "tailored resume. None has been submitted — they are waiting for your approval."
    ]
    if failed:
        parts.append(f"{failed} could not be prepared; the reason is on each row.")
    if capped:
        held = len(current.applyable) - len(ok)
        parts.append(
            f"Stopped at today's cap of {current.daily_cap}"
            + (f" — {held} opening(s) are waiting for tomorrow." if held > 0 else ".")
        )
    if refused:
        parts.append(f"{len(refused)} was skipped as no longer applyable.")
    return " ".join(parts)
