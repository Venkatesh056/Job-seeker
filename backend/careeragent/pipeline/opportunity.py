"""Final opportunity ranking and the application decision.

Combines the scoring stages into one number, then decides what to do about it.
Two rules shape the design:

  * Weights are configurable and versioned. A score is meaningless to the
    learning engine without knowing which weights produced it.
  * A component with no evidence is excluded and its weight redistributed, not
    scored zero. Company fit is unknown until research runs, and treating
    "not yet researched" as "bad company" would bury every job on the first
    pass through a fresh database.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.job import EmploymentType, NormalizedJob
from ..models.scoring import (
    DeterministicScore,
    JobScore,
    LLMScore,
    Priority,
    PriorityThresholds,
    ScoreWeights,
    SemanticScore,
)

# How much of job fit comes from each stage when all are present. Deterministic
# keeps the largest share: it is the only stage that can be inspected line by
# line when a user asks why something ranked where it did.
STAGE_MIX = {"deterministic": 0.45, "semantic": 0.30, "llm": 0.25}

# Application feasibility by channel. A direct ATS application is both easier to
# automate reliably and converts better than a portal repost.
FEASIBILITY = {
    "greenhouse": 1.0,
    "lever": 1.0,
    "ashby": 1.0,
    "workable": 0.9,
    "recruitee": 0.9,
    "smartrecruiters": 0.85,
    "breezy": 0.85,
    "teamtailor": 0.85,
    "personio": 0.85,
    "bamboohr": 0.8,
    "rippling": 0.8,
    # Workday requires creating an account per tenant, with email verification.
    # That is a human step by design, so it is feasible but not cheap.
    "workday": 0.5,
    "linkedin": 0.4,
    "indeed": 0.4,
    "naukri": 0.4,
}

FRESHNESS_FULL_DAYS = 3  # posted within this many days scores full marks
FRESHNESS_ZERO_DAYS = 45  # older than this contributes nothing


@dataclass(frozen=True)
class Decision:
    """What to do about one opportunity, and why."""

    action: str  # apply | review | outreach | reject
    strategy: str  # ats_direct | careers_page | portal | approval | outreach | ignore
    reason: str
    requires_approval: bool = True


def freshness_score(age_days: int | None) -> float | None:
    """None when the posting date is unknown — which is common and must not be
    confused with 'old'."""
    if age_days is None:
        return None
    if age_days <= FRESHNESS_FULL_DAYS:
        return 1.0
    if age_days >= FRESHNESS_ZERO_DAYS:
        return 0.0
    span = FRESHNESS_ZERO_DAYS - FRESHNESS_FULL_DAYS
    return round(1.0 - (age_days - FRESHNESS_FULL_DAYS) / span, 4)


def feasibility_score(job: NormalizedJob) -> float:
    for listing in job.listings:
        if listing.ats and listing.ats in FEASIBILITY:
            return FEASIBILITY[listing.ats]
    return 0.5  # an unrecognised application system; assume it needs a human


def career_potential_score(job: NormalizedJob, deterministic: DeterministicScore) -> float:
    """A rough, honest proxy: a role that stretches slightly beyond the current
    skill set is worth more than one that uses none of it or all of it.

    Deliberately crude. Real career-potential reasoning needs company trajectory
    and team context, which is the LLM's job in stage 3 — this only prevents the
    component from being a constant.
    """
    matched = len(deterministic.matched_skills)
    missing = len(deterministic.missing_skills)
    if matched == 0:
        return 0.2
    stretch = missing / (matched + missing) if (matched + missing) else 0.0
    # Peak around a quarter of the requirements being new.
    return round(max(0.3, 1.0 - abs(stretch - 0.25) * 1.6), 4)


def job_fit(
    deterministic: DeterministicScore,
    semantic: SemanticScore | None,
    llm: LLMScore | None,
) -> float:
    """Blend the stages that actually ran, renormalising over those present."""
    parts: dict[str, tuple[float, float]] = {
        "deterministic": (deterministic.total, STAGE_MIX["deterministic"])
    }
    if semantic is not None:
        parts["semantic"] = (semantic.total, STAGE_MIX["semantic"])
    if llm is not None:
        parts["llm"] = (llm.score, STAGE_MIX["llm"])

    total_weight = sum(w for _, w in parts.values()) or 1.0
    return round(sum(v * w for v, w in parts.values()) / total_weight, 4)


def score_opportunity(
    job: NormalizedJob,
    deterministic: DeterministicScore,
    semantic: SemanticScore | None = None,
    llm: LLMScore | None = None,
    company_fit: float | None = None,
    weights: ScoreWeights | None = None,
) -> JobScore:
    weights = weights or ScoreWeights()
    normalized = weights.normalized()

    score = JobScore(
        job_id=job.job_id,
        deterministic=deterministic,
        semantic=semantic,
        llm=llm,
        weights_version=weights.version,
    )

    if deterministic.hard_fail:
        score.priority = Priority.REJECT
        score.final_score = 0.0
        return score

    score.job_fit = job_fit(deterministic, semantic, llm)
    score.company_fit = company_fit if company_fit is not None else 0.0

    # Only components with real evidence take part; the rest give up their
    # weight rather than dragging the score down with a zero.
    components: dict[str, float] = {
        "job_fit": score.job_fit,
        "candidate_match": deterministic.skills,
        "role_relevance": deterministic.role,
        "location": deterministic.location,
        "application_feasibility": feasibility_score(job),
        "career_potential": career_potential_score(job, deterministic),
    }
    if company_fit is not None:
        components["company_fit"] = company_fit
    if (fresh := freshness_score(job.age_days)) is not None:
        components["freshness"] = fresh

    total_weight = sum(normalized[k] for k in components) or 1.0
    score.final_score = round(
        sum(value * normalized[key] for key, value in components.items()) / total_weight, 4
    )
    score.priority = Priority.from_score(score.final_score)
    return score


def rank(scores: list[JobScore]) -> list[JobScore]:
    return sorted(scores, key=lambda s: s.final_score, reverse=True)


# ─── Decision engine ────────────────────────────────────────────────────────


def decide(
    job: NormalizedJob,
    score: JobScore,
    *,
    autonomy_level: int = 4,
    auto_submit_threshold: float = 0.92,
    already_applied: bool = False,
    thresholds: PriorityThresholds | None = None,
) -> Decision:
    """Decide what to do with one scored opportunity.

    Never returns `apply` on a keyword match alone: a job that matches no target
    role has `role_relevance` at zero, which the scoring already suppresses, and
    the explicit check below makes the rule visible rather than emergent.
    """
    thresholds = thresholds or PriorityThresholds()

    if already_applied:
        return Decision("reject", "ignore", "already applied to this job", False)

    if score.deterministic.hard_fail:
        return Decision("reject", "ignore", score.deterministic.hard_fail, False)

    if score.deterministic.role == 0.0:
        # Still worth surfacing for outreach if the company is a good fit, but
        # never worth an application.
        if score.company_fit >= 0.8:
            return Decision(
                "outreach", "outreach", "strong company but the role does not match your targets"
            )
        return Decision("reject", "ignore", "title does not match any target role", False)

    if score.final_score < thresholds.low:
        return Decision("reject", "ignore", f"score {score.final_score:.2f} below threshold", False)

    if score.final_score < thresholds.review:
        return Decision("review", "approval", f"low priority ({score.final_score:.2f})")

    strategy = _preferred_strategy(job)

    if autonomy_level <= 2:
        return Decision("review", "approval", "autonomy level does not permit applying")

    if (
        autonomy_level >= 5
        and score.final_score >= auto_submit_threshold
        and strategy == "ats_direct"
        and not _needs_human(job)
    ):
        return Decision(
            "apply", strategy, f"high confidence ({score.final_score:.2f}) on a known ATS", False
        )

    return Decision("apply", strategy, f"good match ({score.final_score:.2f}); awaiting approval")


def _preferred_strategy(job: NormalizedJob) -> str:
    """Where to apply when a job appears in more than one place.

    A direct ATS application beats a portal repost: better conversion for the
    candidate, and it avoids automating a platform whose terms prohibit it.
    """
    for listing in job.listings:
        if (
            listing.ats
            and listing.ats in FEASIBILITY
            and listing.ats
            not in {
                "linkedin",
                "indeed",
                "naukri",
            }
        ):
            return "ats_direct"
    if any(listing.source == "careers_page" for listing in job.listings):
        return "careers_page"
    return "approval"


def _needs_human(job: NormalizedJob) -> bool:
    """Signals that this application cannot be safely automated end to end."""
    text = f"{job.title} {job.description[:3000]}".lower()
    markers = (
        "cover letter",
        "portfolio required",
        "security clearance",
        "assessment",
        "take-home",
        "video interview",
    )
    if any(m in text for m in markers):
        return True
    # Workday needs an account per tenant, with email verification.
    return any(listing.ats == "workday" for listing in job.listings)


def summarize(decisions: list[Decision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.action] = counts.get(decision.action, 0) + 1
    return counts


def missing_skill_gaps(scores: list[JobScore], min_occurrences: int = 2) -> list[tuple[str, int]]:
    """Skills that repeatedly cost applications, most frequent first.

    The single most actionable output the system produces: what to learn next,
    ranked by how often its absence shows up in jobs you otherwise fit.
    """
    counts: dict[str, int] = {}
    for score in scores:
        for skill in score.deterministic.missing_skills:
            counts[skill] = counts.get(skill, 0) + 1
    return sorted(
        ((s, c) for s, c in counts.items() if c >= min_occurrences),
        key=lambda pair: pair[1],
        reverse=True,
    )


def is_internship(job: NormalizedJob) -> bool:
    return job.employment_type is EmploymentType.INTERNSHIP
