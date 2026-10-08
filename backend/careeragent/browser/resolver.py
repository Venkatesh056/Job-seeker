"""Canonical field → candidate value.

Pure and evidence-bound. Every value returned either comes from the Fact Bank /
candidate profile, or the field is escalated to a human. Nothing is inferred to
fill a blank, because a blank on a form is recoverable and a wrong answer is not.

The three outcomes are deliberately distinct:

    FILL              a value we can stand behind
    NEEDS_USER        we do not know; ask the person
    NEVER_FILL        legally or ethically not ours to answer, at any autonomy level
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from ..models.candidate import Candidate, FactKind
from .catalog import BY_ID, Sensitivity, is_freeform


class Resolution(enum.StrEnum):
    FILL = "fill"
    NEEDS_USER = "needs_user"
    NEVER_FILL = "never_fill"


@dataclass(frozen=True)
class ResolvedField:
    field_id: str
    resolution: Resolution
    value: str = ""
    reason: str = ""
    fact_ids: tuple[str, ...] = ()

    @property
    def fillable(self) -> bool:
        return self.resolution is Resolution.FILL and bool(self.value)


def _split_name(full: str) -> tuple[str, str]:
    parts = [p for p in full.split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def resolve(
    field_id: str, candidate: Candidate, *, resume_path: str | None = None
) -> ResolvedField:
    """Resolve one canonical field against the candidate."""
    canonical = BY_ID.get(field_id)

    if canonical is not None and canonical.sensitivity is Sensitivity.NEVER_FILL:
        return ResolvedField(
            field_id,
            Resolution.NEVER_FILL,
            reason=(
                "legal declaration or demographic question — answered by you, never by the agent"
            ),
        )

    if is_freeform(field_id):
        return ResolvedField(
            field_id,
            Resolution.NEEDS_USER,
            reason="open question; needs an evidence-grounded answer",
        )

    first, last = _split_name(candidate.name)

    simple: dict[str, str] = {
        "contact.full_name": candidate.name,
        "contact.first_name": first,
        "contact.last_name": last,
        "contact.preferred_name": first,
        "contact.email": str(candidate.email or ""),
        "contact.phone": candidate.phone or "",
        "contact.location": candidate.location,
        "contact.city": candidate.location.split(",")[0].strip() if candidate.location else "",
        "profile.linkedin": candidate.links.get("linkedin", ""),
        "profile.github": candidate.links.get("github", ""),
        "profile.portfolio": candidate.links.get("portfolio", ""),
        "profile.website": candidate.links.get("portfolio", ""),
    }

    if field_id in simple:
        value = simple[field_id].strip()
        if value:
            return ResolvedField(field_id, Resolution.FILL, value)
        return ResolvedField(
            field_id,
            Resolution.NEEDS_USER,
            reason=f"{canonical.label if canonical else field_id} is not in your profile",
        )

    if field_id == "resume.file":
        if resume_path:
            return ResolvedField(field_id, Resolution.FILL, resume_path)
        return ResolvedField(field_id, Resolution.NEEDS_USER, reason="no resume selected")

    if field_id == "experience.years_total":
        years = candidate.total_years_experience()
        if years is None:
            # Dates are missing from the Fact Bank, so any number would be
            # invented. This is exactly the claim that must never be guessed.
            return ResolvedField(
                field_id,
                Resolution.NEEDS_USER,
                reason="no dated experience in the Fact Bank; a number here would be invented",
            )
        fact_ids = tuple(
            str(f.id) for f in candidate.facts if f.kind is FactKind.EXPERIENCE and f.first_used_at
        )
        return ResolvedField(field_id, Resolution.FILL, str(years), fact_ids=fact_ids)

    if field_id == "employment.current_company":
        current = [
            f
            for f in candidate.facts
            if f.kind is FactKind.EXPERIENCE and f.first_used_at and f.last_used_at is None
        ]
        if current:
            return ResolvedField(
                field_id, Resolution.FILL, current[0].label, fact_ids=(str(current[0].id),)
            )
        return ResolvedField(
            field_id, Resolution.NEEDS_USER, reason="no current role in the Fact Bank"
        )

    if field_id in {"education.school", "education.degree", "education.discipline"}:
        education = [f for f in candidate.facts if f.kind is FactKind.EDUCATION]
        if education:
            return ResolvedField(
                field_id,
                Resolution.NEEDS_USER,
                value=education[0].label,
                reason="education fields are dropdowns; confirm the exact option",
                fact_ids=(str(education[0].id),),
            )
        return ResolvedField(field_id, Resolution.NEEDS_USER, reason="no education on file")

    if field_id == "availability.start_date":
        if candidate.availability:
            return ResolvedField(field_id, Resolution.FILL, candidate.availability)
        return ResolvedField(field_id, Resolution.NEEDS_USER, reason="availability not set")

    if field_id == "compensation.expected":
        prefs = candidate.salary_preferences
        if prefs.target:
            return ResolvedField(field_id, Resolution.FILL, f"{prefs.currency} {prefs.target:,.0f}")
        return ResolvedField(field_id, Resolution.NEEDS_USER, reason="no salary expectation set")

    if field_id == "preference.relocate":
        return ResolvedField(
            field_id,
            Resolution.NEEDS_USER,
            reason="relocation is a commitment; confirm per application",
        )

    return ResolvedField(
        field_id, Resolution.NEEDS_USER, reason="no rule maps this field to your profile"
    )


def resolve_all(
    field_ids: list[str], candidate: Candidate, *, resume_path: str | None = None
) -> dict[str, ResolvedField]:
    return {fid: resolve(fid, candidate, resume_path=resume_path) for fid in field_ids}


def summarize(resolved: dict[str, ResolvedField]) -> dict[str, int]:
    counts = {r.value: 0 for r in Resolution}
    for field in resolved.values():
        counts[field.resolution.value] += 1
    return counts
