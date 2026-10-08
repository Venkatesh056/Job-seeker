"""Fetching the description a job was discovered without.

Some sources give a posting's title, location and date but not its text. Apollo's feed is
the main one: it reaches companies whose ATS slug was never guessed and whose careers page
is a JavaScript shell, which is exactly its value, and it hands back 211 real openings with
no body text at all.

That is a problem, and it is the same problem this codebase keeps meeting in new clothes.
Skill matching reads the description. A job with no description matches **zero** skills, and
the scorer cannot tell "this job wants none of your skills" from "nobody has read this job
yet". Measured: all 211 Apollo postings scored `reject`, the best at 0.33, including a
one-day-old product management internship in Bangalore.

So the text is fetched — but not for all of them, because 211 HTTP requests to other
people's servers to score jobs the user will never see is not a reasonable trade. The
candidates are chosen first on what *is* known:

    posted recently  ·  right country  ·  not senior  ·  not already applied to

Those are the filters that need no description. What survives them is worth a request; what
does not is left as it is, with `needs_description` recording why it was never scored
properly, so an absence is visible rather than silently read as a poor match.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from ..config import get_settings
from ..logging import get_logger

log = get_logger("pipeline.describe")

# Below this, a page is a JavaScript shell or an error page rather than a job description.
MIN_USEFUL_CHARS = 400

# Other people's servers, read on our schedule with nobody watching.
CONCURRENCY = 4
REQUEST_TIMEOUT = 15.0


@dataclass
class DescribeResult:
    considered: int = 0
    attempted: int = 0
    described: int = 0
    unreadable: int = 0
    rescored: int = 0
    errors: list[str] = field(default_factory=list)

    def note(self) -> str:
        if not self.considered:
            return "No job is missing its description."
        if not self.attempted:
            return (
                f"{self.considered} job(s) have no description, none of them worth a "
                "request — all are stale, senior, or outside the places you want."
            )
        return (
            f"Read {self.described} of {self.attempted} job pages and rescored them. "
            f"{self.unreadable} rendered nothing usable, most likely JavaScript-only."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "attempted": self.attempted,
            "described": self.described,
            "unreadable": self.unreadable,
            "rescored": self.rescored,
            "errors": self.errors[:6],
            "note": self.note(),
        }


def worth_fetching(
    job: Any,
    config: Any,
    *,
    today: date | None = None,
) -> str:
    """Is this job worth one HTTP request? Returns "" for yes, a reason for no.

    Every test here runs on what a description-less posting already carries — its title,
    location and date — so the decision costs nothing. The point is to spend requests on
    the jobs that could plausibly survive scoring, not on all of them.
    """
    today = today or date.today()

    if not _url_of(job):
        return "no URL to read"

    max_age = getattr(config, "max_job_age_days", None)
    if max_age and job.posted_date and (today - job.posted_date).days > max_age:
        return f"posted more than {max_age} days ago"

    # Seniority is readable from the title alone, and a senior posting is unreachable for
    # a junior candidate however well its description reads.
    from .. import preferences
    from ..models.job import Seniority, detect_seniority

    band = (getattr(config, "experience_band", "") or "").lower()
    if band in preferences.JUNIOR_BANDS:
        seniority = detect_seniority(job.title or "")
        if seniority in {Seniority.SENIOR, Seniority.LEAD}:
            return f"{seniority.value}-level role"

    # Location is on the posting. A job in another country cannot become local by being
    # read, so reading it is a wasted request.
    if locations := [str(loc).lower() for loc in (getattr(config, "locations", None) or [])]:
        from ..company import _canonical_city
        from ..sources.directory.providers import country_code_for

        here = _canonical_city(job.location or "")
        wanted = {_canonical_city(loc) for loc in locations if loc}
        remote_ok = "remote" in {str(w).lower() for w in (getattr(config, "work_modes", []) or [])}
        looks_remote = "remote" in (job.location or "").lower() or not job.location

        names_a_wanted_city = bool(here) and any(here in w or w in here for w in wanted if w)

        if looks_remote:
            # "Remote — Bangalore" is both, and the city is what makes it acceptable.
            if not remote_ok and not names_a_wanted_city:
                return "remote, and you are not looking for remote work"
        elif not here:
            return "no location on the posting"
        elif not names_a_wanted_city:
            # A country is not a mismatch with a city inside it. A posting that says only
            # "India" is *less specific* than "Bangalore", not somewhere else — and 16 real
            # postings were being refused a read on exactly that confusion. Less specific
            # means unknown, and unknown is worth one request to resolve.
            same_country = country_code_for(job.location or "") in {
                country_code_for(loc) for loc in locations
            }
            if not same_country:
                return f"in {job.location}, which is not somewhere you want to work"

    return ""


def _url_of(job: Any) -> str:
    for listing in getattr(job, "listings", None) or []:
        if listing.url:
            return str(listing.url)
    return ""


async def _read(url: str, client: httpx.AsyncClient) -> str:
    """The readable text of one job page, or "" if there is none.

    Never raises: a posting that cannot be read leaves the job exactly as it was, which is
    the same state it was already in.
    """
    from ..company import _strip_html

    try:
        response = await client.get(url, headers={"accept": "text/html,*/*"})
    except httpx.HTTPError:
        return ""
    if response.status_code >= 400:
        return ""
    if "html" not in response.headers.get("content-type", ""):
        return ""
    return _strip_html(response.text)


async def describe_missing(
    session: Any,
    candidate: Any,
    candidate_id: str,
    *,
    limit: int = 40,
    source: str = "",
) -> DescribeResult:
    """Fetch and store the descriptions of jobs discovered without one, then rescore.

    Rescoring is the whole point — a description nobody scored against changes nothing —
    and it happens per job, immediately, so a run interrupted halfway leaves every job it
    touched consistent rather than half-described and unscored.
    """
    from sqlalchemy import select

    from .. import preferences
    from ..storage import repository as repo
    from ..storage.models import Job, JobListing
    from .opportunity import score_opportunity
    from .score import ScoreContext, score_job

    result = DescribeResult()
    settings = get_settings()
    config = await preferences.load(session, candidate_id)

    stmt = select(Job).where(Job.status == "open", Job.description == "")
    if source:
        stmt = stmt.where(Job.id.in_(select(JobListing.job_id).where(JobListing.source == source)))
    jobs = list((await session.execute(stmt)).scalars().all())
    result.considered = len(jobs)
    if not jobs:
        return result

    ranked: list[tuple[Any, str]] = []
    for job in jobs:
        if await repo.already_applied(session, job.id, candidate_id):
            continue
        if reason := worth_fetching(job, config):
            log.debug("description_not_worth_fetching", job=job.id, reason=reason)
            continue
        ranked.append((job, _url_of(job)))

    # Freshest first: if the limit cuts the list short, the jobs most likely to still be
    # open are the ones that got read.
    ranked.sort(key=lambda pair: pair[0].posted_date or date.min, reverse=True)
    ranked = ranked[:limit]
    result.attempted = len(ranked)
    if not ranked:
        return result

    context = ScoreContext(candidate=candidate, config=config)
    gate = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"user-agent": settings.user_agent},
    ) as client:

        async def read_one(url: str) -> str:
            async with gate:
                return await _read(url, client)

        # Fetching is concurrent; writing is not. A SQLAlchemy async session is not safe
        # to share across tasks, and the network is the slow part anyway.
        texts = await asyncio.gather(*(read_one(url) for _job, url in ranked))

    from ..applications.service import _job_to_normalized

    for (job, _url), text in zip(ranked, texts, strict=True):
        if len(text) < MIN_USEFUL_CHARS:
            result.unreadable += 1
            continue

        job.description = text[:40_000]
        result.described += 1

        normalized = _job_to_normalized(job)
        deterministic = score_job(normalized, context)
        await repo.save_score(session, candidate_id, score_opportunity(normalized, deterministic))
        result.rescored += 1

    await session.flush()
    log.info(
        "descriptions_fetched",
        considered=result.considered,
        attempted=result.attempted,
        described=result.described,
        unreadable=result.unreadable,
    )
    return result
