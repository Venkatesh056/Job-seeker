"""Analytics: the funnel, the rates, and the breakdowns.

One rule runs through this module, and it is the difference between a useful
dashboard and a misleading one: **every rate carries its sample size, and a rate
computed on too few observations is reported as None rather than as a number.**

A single candidate generates tens of applications, not thousands. "Data Analyst:
0% response rate" over three applications is not a finding, it is noise — and
presented as a percentage it will change how someone spends their next month. So
`MIN_SAMPLE` gates every rate, and the denominator travels with it everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .applications.states import RESPONDED_STATES, SUBMITTED_STATES
from .storage.models import (
    Application,
    Job,
    JobScore,
    LLMCall,
    ResumeRender,
)
from .storage.models import (
    ApplicationStatus as S,
)

# Below this many submissions, a rate is not reported. Chosen because it is the
# point where one extra response stops moving the percentage by more than 10
# points — not a statistical threshold, an honesty threshold.
MIN_SAMPLE = 8


@dataclass
class Rate:
    """A proportion that knows how much evidence is behind it."""

    numerator: int
    denominator: int
    label: str = ""

    @property
    def value(self) -> float | None:
        if self.denominator < MIN_SAMPLE:
            return None
        return round(self.numerator / self.denominator, 4)

    @property
    def indicative(self) -> float | None:
        """The raw proportion, for display next to its sample size only.

        Kept separate from `value` so that a caller cannot accidentally treat a
        1-in-2 as a 50% response rate.
        """
        if self.denominator == 0:
            return None
        return round(self.numerator / self.denominator, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "n": self.denominator,
            "count": self.numerator,
            "rate": self.value,
            "indicative_rate": self.indicative,
            "reportable": self.denominator >= MIN_SAMPLE,
        }


@dataclass
class Funnel:
    """The brief's §1 funnel, measured rather than asserted."""

    discovered: int = 0
    scored: int = 0
    past_gate: int = 0
    strong_matches: int = 0
    shortlisted: int = 0
    prepared: int = 0
    submitted: int = 0
    responses: int = 0
    interviews: int = 0
    offers: int = 0

    def to_dict(self) -> dict[str, Any]:
        stages = [
            ("Discovered", self.discovered),
            ("Scored", self.scored),
            ("Past relevance gate", self.past_gate),
            ("Strong matches", self.strong_matches),
            ("Shortlisted", self.shortlisted),
            ("Prepared", self.prepared),
            ("Submitted", self.submitted),
            ("Responses", self.responses),
            ("Interviews", self.interviews),
            ("Offers", self.offers),
        ]
        out = []
        previous: int | None = None
        for name, count in stages:
            out.append(
                {
                    "stage": name,
                    "count": count,
                    "from_previous": (round(count / previous, 4) if previous else None),
                }
            )
            previous = count or previous
        return {"stages": out, **self.__dict__}


# ─── Funnel ─────────────────────────────────────────────────────────────────


async def funnel(session: AsyncSession, candidate_id: str) -> Funnel:
    result = Funnel()

    result.discovered = int(
        (await session.execute(select(func.count()).select_from(Job))).scalar() or 0
    )
    result.scored = int(
        (
            await session.execute(
                select(func.count())
                .select_from(JobScore)
                .where(JobScore.candidate_id == candidate_id)
            )
        ).scalar()
        or 0
    )
    result.past_gate = int(
        (
            await session.execute(
                select(func.count())
                .select_from(JobScore)
                .where(JobScore.candidate_id == candidate_id, JobScore.priority != "reject")
            )
        ).scalar()
        or 0
    )
    result.strong_matches = int(
        (
            await session.execute(
                select(func.count())
                .select_from(JobScore)
                .where(
                    JobScore.candidate_id == candidate_id,
                    JobScore.priority.in_(["very_high", "high"]),
                )
            )
        ).scalar()
        or 0
    )

    applications = (
        (await session.execute(select(Application).where(Application.candidate_id == candidate_id)))
        .scalars()
        .all()
    )
    result.shortlisted = len(applications)
    result.prepared = sum(1 for a in applications if a.preview)
    result.submitted = sum(1 for a in applications if S(a.status) in SUBMITTED_STATES)
    result.responses = sum(1 for a in applications if S(a.status) in RESPONDED_STATES)
    result.interviews = sum(1 for a in applications if S(a.status) in {S.INTERVIEW, S.OFFER})
    result.offers = sum(1 for a in applications if S(a.status) is S.OFFER)
    return result


# ─── Rates ──────────────────────────────────────────────────────────────────


async def rates(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    counts = await funnel(session, candidate_id)
    return {
        "response": Rate(counts.responses, counts.submitted, "response rate").to_dict(),
        "interview": Rate(counts.interviews, counts.submitted, "interview rate").to_dict(),
        "offer": Rate(counts.offers, counts.submitted, "offer rate").to_dict(),
        "min_sample": MIN_SAMPLE,
        "note": (
            f"Rates are withheld below {MIN_SAMPLE} submissions. An indicative "
            "proportion is shown with its sample size so it is read as what it is."
        ),
    }


# ─── Breakdowns ─────────────────────────────────────────────────────────────

DIMENSIONS = (
    "role",
    "company",
    "location",
    "source",
    "ats",
    "resume_archetype",
    "work_mode",
    "seniority",
    "strategy",
)


async def by_dimension(
    session: AsyncSession, candidate_id: str, dimension: str = "role"
) -> dict[str, Any]:
    """Outcomes grouped by one dimension, every group carrying its sample size.

    The brief's §28 example ("AI Engineer 11%, Data Analyst 3%") is exactly the
    output — with the caveat that at realistic volumes most groups will have too few
    observations to report a rate, and this says so rather than printing one.
    """
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}; choose from {', '.join(DIMENSIONS)}")

    rows = (
        await session.execute(
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(Application.candidate_id == candidate_id)
        )
    ).all()

    archetypes: dict[str, str] = {}
    if dimension == "resume_archetype":
        renders = (
            (
                await session.execute(
                    select(ResumeRender).where(ResumeRender.candidate_id == candidate_id)
                )
            )
            .scalars()
            .all()
        )
        archetypes = {r.id: r.archetype for r in renders}

    groups: dict[str, dict[str, int]] = {}
    for application, job in rows:
        key = _dimension_key(dimension, application, job, archetypes)
        bucket = groups.setdefault(
            key, {"applications": 0, "submitted": 0, "responses": 0, "interviews": 0, "offers": 0}
        )
        status = S(application.status)
        bucket["applications"] += 1
        if status in SUBMITTED_STATES:
            bucket["submitted"] += 1
        if status in RESPONDED_STATES:
            bucket["responses"] += 1
        if status in {S.INTERVIEW, S.OFFER}:
            bucket["interviews"] += 1
        if status is S.OFFER:
            bucket["offers"] += 1

    out = []
    for key, bucket in groups.items():
        out.append(
            {
                "key": key,
                **bucket,
                "response": Rate(bucket["responses"], bucket["submitted"]).to_dict(),
                "interview": Rate(bucket["interviews"], bucket["submitted"]).to_dict(),
            }
        )
    out.sort(key=lambda g: (g["submitted"], g["applications"]), reverse=True)
    return {"dimension": dimension, "groups": out}


def _dimension_key(
    dimension: str, application: Application, job: Job, archetypes: dict[str, str]
) -> str:
    if dimension == "role":
        return job.normalized_title or job.title or "unknown"
    if dimension == "company":
        return job.company or "unknown"
    if dimension == "location":
        return (job.location or "unknown").split(",")[0].strip() or "unknown"
    if dimension == "source":
        return (job.listings[0].source if job.listings else None) or "unknown"
    if dimension == "ats":
        return application.ats or job.ats or "unknown"
    if dimension == "resume_archetype":
        return archetypes.get(application.resume_render_id or "", "none")
    if dimension == "work_mode":
        return job.work_mode or "unknown"
    if dimension == "seniority":
        return job.seniority or "unknown"
    return application.strategy or "unknown"


async def resume_performance(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    """Which resume archetype actually produces responses, and how well it validated.

    Two different questions on one screen on purpose: an archetype can score 98% on
    ATS heuristics and get no replies, and that combination is the interesting one.
    """
    renders = (
        (
            await session.execute(
                select(ResumeRender).where(ResumeRender.candidate_id == candidate_id)
            )
        )
        .scalars()
        .all()
    )
    by_archetype: dict[str, dict[str, Any]] = {}
    for render in renders:
        bucket = by_archetype.setdefault(
            render.archetype,
            {"renders": 0, "ats_scores": [], "integrity": [], "keyword": []},
        )
        bucket["renders"] += 1
        if render.ats_score is not None:
            bucket["ats_scores"].append(render.ats_score)
        if render.evidence_integrity is not None:
            bucket["integrity"].append(render.evidence_integrity)
        if render.keyword_match is not None:
            bucket["keyword"].append(render.keyword_match)

    outcomes = await by_dimension(session, candidate_id, "resume_archetype")
    outcome_by_key = {g["key"]: g for g in outcomes["groups"]}

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    out = []
    for archetype, bucket in by_archetype.items():
        outcome = outcome_by_key.get(archetype, {})
        out.append(
            {
                "archetype": archetype,
                "renders": bucket["renders"],
                "avg_ats_score": mean(bucket["ats_scores"]),
                "avg_evidence_integrity": mean(bucket["integrity"]),
                "avg_keyword_match": mean(bucket["keyword"]),
                "submitted": outcome.get("submitted", 0),
                "responses": outcome.get("responses", 0),
                "response": outcome.get("response"),
            }
        )
    out.sort(key=lambda g: g["renders"], reverse=True)
    return {"archetypes": out}


# ─── Cost ───────────────────────────────────────────────────────────────────


async def costs(session: AsyncSession, days: int = 30) -> dict[str, Any]:
    """LLM spend, and what it bought. The brief's §54 per-application figure.

    Cost per application is the number that decides whether the funnel is tuned
    correctly, so it is computed from real recorded calls rather than estimated
    from a price list.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    calls = (await session.execute(select(LLMCall).where(LLMCall.at >= since))).scalars().all()

    by_agent: dict[str, dict[str, Any]] = {}
    total = 0.0
    tokens = 0
    cache_hits = 0
    for call in calls:
        bucket = by_agent.setdefault(call.agent, {"calls": 0, "cost_usd": 0.0, "tokens": 0})
        bucket["calls"] += 1
        bucket["cost_usd"] = round(bucket["cost_usd"] + call.cost_usd, 6)
        bucket["tokens"] += call.prompt_tokens + call.completion_tokens
        total += call.cost_usd
        tokens += call.prompt_tokens + call.completion_tokens
        cache_hits += 1 if call.cache_hit else 0

    submitted = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Application)
                .where(Application.submitted_at.is_not(None), Application.submitted_at >= since)
            )
        ).scalar()
        or 0
    )

    return {
        "window_days": days,
        "calls": len(calls),
        "cache_hits": cache_hits,
        "cache_hit_rate": round(cache_hits / len(calls), 4) if calls else None,
        "tokens": tokens,
        "total_usd": round(total, 6),
        "by_agent": by_agent,
        "applications_submitted": submitted,
        "cost_per_application_usd": round(total / submitted, 6) if submitted else None,
    }


# ─── Everything at once ─────────────────────────────────────────────────────


async def overview(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    from .applications import tracker

    return {
        "funnel": (await funnel(session, candidate_id)).to_dict(),
        "rates": await rates(session, candidate_id),
        "applications": await tracker.summary(session, candidate_id),
        "costs": await costs(session),
        "by_role": (await by_dimension(session, candidate_id, "role"))["groups"][:10],
        "by_location": (await by_dimension(session, candidate_id, "location"))["groups"][:10],
        "resume_performance": await resume_performance(session, candidate_id),
    }
