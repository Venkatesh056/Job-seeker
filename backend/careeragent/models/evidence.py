"""Three states, not two: known, unknown, and not applicable.

This module exists because the same bug was found three separate times in this project,
each time wearing different clothes and each time costing real ranking quality:

1. **Company fit.** A company whose website is a JavaScript shell yielded no readable text,
   so its fit scored **0.0** — indistinguishable from a company that was read and found to
   be a poor match. Swiggy sat at 0.00 until Apollo filled the gap, then at 0.87.

2. **Technology overlap.** 42 of 63 researched companies overlapped on *nothing* with a
   candidate holding 50 skills, because a marketing page says "we deliver value", not "we
   use pandas". The heaviest component scored 0.13 on average and crushed every company.

3. **Job descriptions.** Apollo's feed carries no body text, so 211 real openings matched
   zero skills and all scored `reject` — including a one-day-old internship in Bangalore.

The shape is identical every time. A measurement that was never taken gets written down as
a measurement of zero, and nothing downstream can tell the difference. A zero is a claim
about the world; an absence is a claim about our knowledge of it, and conflating them makes
a confident-looking ranking out of nothing.

    KNOWN(0.0)       "we looked, and it is bad"
    UNKNOWN          "we did not look, or could not see"
    NOT_APPLICABLE   "this question does not apply here"

An `UNKNOWN` component takes no part in a weighted score — it surrenders its weight to the
components that do have evidence, rather than dragging the average down. `NOT_APPLICABLE`
does the same but means something different to a reader, and the distinction matters when
explaining a score: "we could not find their tech stack" and "this role has no tech stack"
deserve different sentences.

What comes out the other side is not a bare number but a number with its provenance
attached — how much of the possible evidence it rests on, and what was missing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Known(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Observation:
    """One measured component, or the honest absence of one.

    Construct through `measured`, `unknown` or `not_applicable` rather than directly, so
    that an `UNKNOWN` can never be carrying a value that something later reads.
    """

    name: str
    state: Known
    value: float | None = None
    # Why it is unknown, in the words a user should see. Empty for a real measurement.
    reason: str = ""
    detail: str = ""

    @property
    def counts(self) -> bool:
        """Does this take part in a weighted score?"""
        return self.state is Known.KNOWN and self.value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "value": self.value,
            "reason": self.reason or None,
            "detail": self.detail or None,
        }


def measured(name: str, value: float, *, detail: str = "") -> Observation:
    """A real measurement. `value` is clamped, because a component is a fraction."""
    return Observation(
        name=name, state=Known.KNOWN, value=max(0.0, min(1.0, float(value))), detail=detail
    )


def unknown(name: str, reason: str) -> Observation:
    """Not measured. The reason is required — an unexplained gap is as opaque as a zero."""
    return Observation(name=name, state=Known.UNKNOWN, value=None, reason=reason)


def not_applicable(name: str, reason: str) -> Observation:
    """The question does not apply. Distinct from unknown when explaining a score."""
    return Observation(name=name, state=Known.NOT_APPLICABLE, value=None, reason=reason)


@dataclass
class Scored:
    """A score with its provenance attached.

    A bare float invites exactly the mistake this module exists to prevent: two scores
    computed from wholly different amounts of evidence look comparable, and nothing warns
    the reader. `confidence` and `evidence_coverage` travel with the number so that a
    sparse score cannot silently pass as a confident one.
    """

    score: float
    confidence: float
    evidence_coverage: float
    observations: list[Observation] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def unknown_fields(self) -> list[str]:
        return [o.name for o in self.observations if o.state is Known.UNKNOWN]

    @property
    def not_applicable_fields(self) -> list[str]:
        return [o.name for o in self.observations if o.state is Known.NOT_APPLICABLE]

    @property
    def provisional(self) -> bool:
        """Is this resting on so little evidence that it should not be read as a verdict?"""
        return self.evidence_coverage < 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 3),
            "evidence_coverage": round(self.evidence_coverage, 3),
            "unknown_fields": self.unknown_fields,
            "not_applicable_fields": self.not_applicable_fields,
            "provisional": self.provisional,
            "observations": [o.to_dict() for o in self.observations],
            "reasons": self.reasons,
        }


def combine(
    observations: Iterable[Observation],
    weights: Mapping[str, float],
    *,
    reasons: list[str] | None = None,
) -> Scored:
    """Weight the components that have evidence, and account for the ones that do not.

    Unknown components surrender their weight rather than contributing a zero — that single
    rule is the whole point. What they do contribute is a lower `evidence_coverage`, so the
    caller can see that a 0.9 built on one component is not the same animal as a 0.9 built
    on six.

    `confidence` is coverage tempered by how much weight the *unknown* components carried.
    Missing a 5%-weight field barely dents it; missing the 35% one should.
    """
    observations = list(observations)
    total_possible = sum(weights.get(o.name, 0.0) for o in observations) or sum(weights.values())

    counted = [o for o in observations if o.counts]
    counted_weight = sum(weights.get(o.name, 0.0) for o in counted)

    if not counted or counted_weight <= 0:
        # Nothing could be measured. A zero score with zero confidence, which any caller
        # that reads confidence will treat as "not assessed" rather than "assessed badly".
        return Scored(
            score=0.0,
            confidence=0.0,
            evidence_coverage=0.0,
            observations=observations,
            reasons=list(reasons or []),
        )

    score = sum((o.value or 0.0) * weights.get(o.name, 0.0) for o in counted) / counted_weight

    # Not-applicable weight is excluded from the denominator: a question that does not
    # apply is not evidence we are missing, so it should not depress coverage.
    applicable = total_possible - sum(
        weights.get(o.name, 0.0) for o in observations if o.state is Known.NOT_APPLICABLE
    )
    coverage = counted_weight / applicable if applicable > 0 else 0.0

    unknown_weight = sum(weights.get(o.name, 0.0) for o in observations if o.state is Known.UNKNOWN)
    confidence = coverage * (1.0 - min(0.5, unknown_weight))

    return Scored(
        score=round(score, 4),
        confidence=round(max(0.0, min(1.0, confidence)), 3),
        evidence_coverage=round(max(0.0, min(1.0, coverage)), 3),
        observations=observations,
        reasons=list(reasons or []),
    )
