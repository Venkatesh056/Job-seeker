"""The learning engine: proposals, not optimization.

The brief's §29 asks the agent to learn from outcomes. The honest version of that,
at the data volumes one job search produces, is a decision aid — and saying so is
part of the feature.

A single candidate produces perhaps 50-200 applications and a handful of
interviews. Fitting eight ranking weights to that is overfitting to noise, and the
resulting system would confidently steer someone away from a role type on the
evidence of two rejections. So:

  * Every proposal states its sample size and a confidence band derived from it.
  * Nothing is applied automatically. `apply_proposal` exists and requires an
    explicit call; there is no scheduler that calls it.
  * Weights are versioned, so an applied proposal is diffable and revertible, and
    an old score remains explainable by the weights that produced it.
  * Below `MIN_OBSERVATIONS`, the engine says it does not know yet. That is the
    correct output for most of the first month, and reporting it is more useful
    than a confident weight change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .analytics import by_dimension, funnel, resume_performance
from .logging import get_logger
from .models.scoring import ScoreWeights
from .storage import repository as repo
from .storage.models import LearningProposal

log = get_logger("learning")

# Below this many submissions, no proposal is made at all.
MIN_OBSERVATIONS = 12
# The largest single change to any weight. Small on purpose: a proposal should
# nudge the ranking, not redefine it, because the evidence cannot support more.
MAX_WEIGHT_DELTA = 0.05
# How much better one group must do than another before it is worth acting on.
MEANINGFUL_GAP = 0.10


@dataclass
class Proposal:
    kind: str
    observation: str
    sample_size: int
    confidence: str  # low | moderate | none
    current: dict[str, Any] = field(default_factory=dict)
    proposed: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    @property
    def actionable(self) -> bool:
        return bool(self.proposed) and self.proposed != self.current

    def diff(self) -> list[dict[str, Any]]:
        keys = sorted(set(self.current) | set(self.proposed))
        return [
            {
                "key": key,
                "from": self.current.get(key),
                "to": self.proposed.get(key),
                "delta": round(
                    float(self.proposed.get(key, 0)) - float(self.current.get(key, 0)), 4
                ),
            }
            for key in keys
            if self.current.get(key) != self.proposed.get(key)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "observation": self.observation,
            "sample_size": self.sample_size,
            "confidence": self.confidence,
            "current": dict(self.current),
            "proposed": dict(self.proposed),
            "diff": self.diff(),
            "rationale": self.rationale,
            "actionable": self.actionable,
        }


def confidence_for(sample: int) -> str:
    """Deliberately tops out at "moderate".

    There is no sample size reachable by one person's job search that would justify
    calling a weight change high-confidence, and a label that said so would be the
    engine overstating itself — the exact failure the whole module is designed around.
    """
    if sample < MIN_OBSERVATIONS:
        return "none"
    if sample < 30:
        return "low"
    return "moderate"


# ─── Observations ───────────────────────────────────────────────────────────


async def observations(session: AsyncSession, candidate_id: str) -> list[dict[str, Any]]:
    """Descriptive patterns in the outcomes. No recommendations, just the numbers.

    Useful on its own — this is the screen that answers "what is actually working"
    — and it is the input every proposal is derived from.
    """
    out: list[dict[str, Any]] = []
    counts = await funnel(session, candidate_id)

    if counts.submitted < MIN_OBSERVATIONS:
        out.append(
            {
                "kind": "insufficient_data",
                "text": (
                    f"{counts.submitted} submitted application(s). Below {MIN_OBSERVATIONS} "
                    "there is nothing here worth learning from, and a pattern found now "
                    "would be noise."
                ),
                "sample_size": counts.submitted,
            }
        )
        return out

    for dimension in ("role", "location", "ats", "source"):
        groups = (await by_dimension(session, candidate_id, dimension))["groups"]
        reportable = [g for g in groups if g["response"]["reportable"]]
        if len(reportable) < 2:
            continue
        best, worst = reportable[0], reportable[-1]
        for group in reportable:
            if (group["response"]["rate"] or 0) > (best["response"]["rate"] or 0):
                best = group
            if (group["response"]["rate"] or 1) < (worst["response"]["rate"] or 1):
                worst = group
        gap = (best["response"]["rate"] or 0) - (worst["response"]["rate"] or 0)
        if gap >= MEANINGFUL_GAP:
            out.append(
                {
                    "kind": f"{dimension}_gap",
                    "text": (
                        f"{dimension}: {best['key']} responds at "
                        f"{best['response']['rate']:.0%} (n={best['submitted']}) versus "
                        f"{worst['key']} at {worst['response']['rate']:.0%} "
                        f"(n={worst['submitted']})"
                    ),
                    "sample_size": best["submitted"] + worst["submitted"],
                    "best": best["key"],
                    "worst": worst["key"],
                    "gap": round(gap, 4),
                }
            )

    resumes = (await resume_performance(session, candidate_id))["archetypes"]
    reportable_resumes = [r for r in resumes if r.get("response") and r["response"]["reportable"]]
    if len(reportable_resumes) >= 2:
        ranked = sorted(reportable_resumes, key=lambda r: r["response"]["rate"] or 0, reverse=True)
        out.append(
            {
                "kind": "resume_performance",
                "text": (
                    f"{ranked[0]['archetype']} resume responds at "
                    f"{ranked[0]['response']['rate']:.0%} (n={ranked[0]['submitted']}); "
                    f"{ranked[-1]['archetype']} at {ranked[-1]['response']['rate']:.0%} "
                    f"(n={ranked[-1]['submitted']})"
                ),
                "sample_size": sum(r["submitted"] for r in ranked),
                "best": ranked[0]["archetype"],
            }
        )

    unverified = await _unverified_count(session, candidate_id)
    if unverified:
        out.append(
            {
                "kind": "unverified_submissions",
                "text": (
                    f"{unverified} application(s) were clicked through but never confirmed. "
                    "Until you verify them, every rate above is computed on a denominator "
                    "that may be wrong."
                ),
                "sample_size": unverified,
            }
        )

    if not out:
        out.append(
            {
                "kind": "no_pattern",
                "text": (
                    f"No difference large enough to act on across {counts.submitted} "
                    "submissions. That is a normal result, not a missing feature."
                ),
                "sample_size": counts.submitted,
            }
        )
    return out


async def _unverified_count(session: AsyncSession, candidate_id: str) -> int:
    from sqlalchemy import func

    from .storage.models import Application
    from .storage.models import ApplicationStatus as S

    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Application)
                .where(
                    Application.candidate_id == candidate_id,
                    Application.status == S.SUBMISSION_UNVERIFIED.value,
                )
            )
        ).scalar()
        or 0
    )


# ─── Proposals ──────────────────────────────────────────────────────────────


async def propose_weights(session: AsyncSession, candidate_id: str) -> Proposal:
    """Suggest a ranking-weight change from the outcomes so far.

    The mapping from observation to weight is intentionally narrow — only two
    components can be moved, and only by `MAX_WEIGHT_DELTA`:

      * A location gap moves `location`.
      * A role gap moves `role_relevance`.

    Response rates cannot tell us anything about whether `company_fit` or
    `career_potential` were weighted correctly, so the engine does not pretend to
    and leaves them alone.
    """
    counts = await funnel(session, candidate_id)
    active = await repo.active_weights(session, candidate_id)
    current = dict(active.weights) if active else ScoreWeights().model_dump(exclude={"version"})

    if counts.submitted < MIN_OBSERVATIONS:
        return Proposal(
            kind="ranking_weights",
            observation=(
                f"{counts.submitted} submitted application(s); {MIN_OBSERVATIONS} needed "
                "before a weight change is anything but guesswork"
            ),
            sample_size=counts.submitted,
            confidence="none",
            current=current,
        )

    notes: list[str] = []
    proposed = dict(current)

    for dimension, key in (("location", "location"), ("role", "role_relevance")):
        groups = (await by_dimension(session, candidate_id, dimension))["groups"]
        reportable = [g for g in groups if g["response"]["reportable"]]
        if len(reportable) < 2:
            continue
        rates = [g["response"]["rate"] or 0.0 for g in reportable]
        gap = max(rates) - min(rates)
        if gap < MEANINGFUL_GAP:
            continue
        # A large spread means this dimension separates outcomes, so it deserves
        # more of the ranking. A flat spread is not evidence it deserves less.
        delta = min(MAX_WEIGHT_DELTA, round(gap / 4, 4))
        proposed[key] = round(min(0.5, current.get(key, 0.0) + delta), 4)
        notes.append(
            f"{dimension} separates outcomes by {gap:.0%} across "
            f"{sum(g['submitted'] for g in reportable)} submissions, so {key} is "
            f"weighted +{delta:.3f}"
        )

    if not notes:
        return Proposal(
            kind="ranking_weights",
            observation=(
                f"No dimension separates outcomes by more than {MEANINGFUL_GAP:.0%} over "
                f"{counts.submitted} submissions; the current weights are not contradicted"
            ),
            sample_size=counts.submitted,
            confidence=confidence_for(counts.submitted),
            current=current,
        )

    # Renormalise so the weights still sum to what they did.
    total_before = sum(current.values()) or 1.0
    total_after = sum(proposed.values()) or 1.0
    proposed = {k: round(v * total_before / total_after, 4) for k, v in proposed.items()}

    return Proposal(
        kind="ranking_weights",
        observation="; ".join(notes),
        sample_size=counts.submitted,
        confidence=confidence_for(counts.submitted),
        current=current,
        proposed=proposed,
        rationale=(
            "Derived from response rates only. It cannot distinguish 'this job type "
            "suits you' from 'these companies were hiring'. Review before applying."
        ),
    )


async def save_proposal(
    session: AsyncSession, candidate_id: str, proposal: Proposal
) -> LearningProposal:
    row = LearningProposal(
        candidate_id=candidate_id,
        kind=proposal.kind,
        observation=proposal.observation,
        sample_size=proposal.sample_size,
        confidence=proposal.confidence,
        current=proposal.current,
        proposed=proposal.proposed,
        status="pending" if proposal.actionable else "informational",
    )
    session.add(row)
    await session.flush()
    return row


async def list_proposals(
    session: AsyncSession, candidate_id: str, limit: int = 20
) -> list[LearningProposal]:
    return list(
        (
            await session.execute(
                select(LearningProposal)
                .where(LearningProposal.candidate_id == candidate_id)
                .order_by(LearningProposal.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def apply_proposal(
    session: AsyncSession, candidate_id: str, proposal_id: str
) -> dict[str, Any]:
    """Apply an approved proposal as a new weight version.

    Called only from an explicit user action. There is no scheduler, no threshold
    and no confidence level at which this runs by itself — see the module docstring.
    """
    from datetime import UTC, datetime

    row = await session.get(LearningProposal, proposal_id)
    if row is None or row.candidate_id != candidate_id:
        raise ValueError("proposal not found")
    if row.status == "applied":
        raise ValueError("this proposal has already been applied")
    if not row.proposed:
        raise ValueError("this proposal has nothing to apply")

    weights = await repo.save_weights(
        session,
        candidate_id,
        row.proposed,
        rationale=f"{row.observation} (n={row.sample_size}, {row.confidence} confidence)",
    )
    row.status = "applied"
    row.applied_at = datetime.now(UTC)
    await session.flush()

    log.info(
        "weights_applied",
        version=weights.version,
        sample_size=row.sample_size,
        confidence=row.confidence,
    )
    return {
        "applied": True,
        "weights_version": weights.version,
        "weights": weights.weights,
        "note": (
            "Scores computed before this change still record the version that produced "
            "them. Re-run scoring to rank existing jobs under the new weights."
        ),
    }


async def revert_weights(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    """Go back to the previous weight version. The reason versions exist."""
    from .storage.models import RankingWeights

    rows = list(
        (
            await session.execute(
                select(RankingWeights)
                .where(RankingWeights.candidate_id == candidate_id)
                .order_by(RankingWeights.version.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) < 2:
        raise ValueError("there is no previous weight version to go back to")

    rows[0].active = False
    rows[1].active = True
    await session.flush()
    return {"reverted_to_version": rows[1].version, "weights": rows[1].weights}


async def active_weight_model(session: AsyncSession, candidate_id: str) -> ScoreWeights:
    """The weights the scorer should use right now."""
    row = await repo.active_weights(session, candidate_id)
    if row is None:
        return ScoreWeights()
    return ScoreWeights(version=row.version, **row.weights)
