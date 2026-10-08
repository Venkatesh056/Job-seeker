"""Application Verification Agent — spec §17, §67.

The gap this closes, stated as the spec states it:

    Resume:      1 year experience
    Application: 2 years experience
    →            BLOCK

Three validators already existed and none of them caught that:

  * `resume/validate.py`   checks **resume vs Fact Bank**
  * `answers.check_grounding` checks **answer vs Fact Bank**
  * `browser/verdict.py`   checks **field completeness** — is it filled, is it a
                           placeholder, did the combobox commit

Nothing compared the **filled form against the document being attached to it**. A form
saying two years and a resume saying one is not an incomplete field and not an ungrounded
claim; it is a contradiction, and it is the one a recruiter notices.

Pure, like the other validators, and for the same reason: this decides whether an
irreversible action proceeds, so it must not be able to fail for an unrelated reason, and
it must be exhaustively testable without a browser.

It runs *after* `presubmit_verdict` and *before* the submit click. A verdict blocks
because a field is not ready; this blocks because the application would be inconsistent
with itself.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any

from ..models.candidate import Candidate, FactKind
from ..resume.documents import TailoredResume
from ..resume.validate import _supported_terms, _technology_tokens

# Tolerance on a stated duration, in years. A candidate with 1.8 years writing "2" is
# rounding; writing "5" is not. Half a year is the widest gap that reads as rounding.
DURATION_TOLERANCE = 0.5


class Severity(enum.StrEnum):
    BLOCK = "block"
    WARN = "warn"


@dataclass
class Conflict:
    """One inconsistency between what is on the form and what is on the record."""

    field: str
    severity: Severity
    kind: str
    form_value: str
    record_value: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.BLOCK

    def __str__(self) -> str:
        return f"{self.field}: {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "severity": self.severity.value,
            "kind": self.kind,
            "form_value": self.form_value,
            "record_value": self.record_value,
            "detail": self.detail,
        }


@dataclass
class VerificationResult:
    conflicts: list[Conflict] = field(default_factory=list)
    checked: int = 0

    @property
    def blocked(self) -> bool:
        return any(c.blocking for c in self.conflicts)

    @property
    def blocking_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.blocking]

    @property
    def warnings(self) -> list[Conflict]:
        return [c for c in self.conflicts if not c.blocking]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "checked": self.checked,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "blocking": [str(c) for c in self.blocking_conflicts],
            "warnings": [str(c) for c in self.warnings],
        }


# ─── Value readers ──────────────────────────────────────────────────────────

_DURATION = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)?", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
_DIGITS = re.compile(r"\d")


def _as_years(value: str) -> float | None:
    """A year count from a form value. None when the value is not a number.

    Handles the ranges dropdowns use — "2-3 years" is read as its *lower* bound, because
    selecting "2-3" claims at least two and the lower bound is what has to be supported.
    """
    text = (value or "").strip()
    if not text:
        return None
    if range_match := re.match(r"(\d+(?:\.\d+)?)\s*[-–—to]+\s*(\d+(?:\.\d+)?)", text):
        return float(range_match.group(1))
    if match := _DURATION.search(text):
        return float(match.group(1))
    return None


def _digits(value: str) -> str:
    return "".join(_DIGITS.findall(value or ""))


def _norm(value: str) -> str:
    return " ".join((value or "").lower().split())


# ─── The checks ─────────────────────────────────────────────────────────────


def _check_duration(field_id: str, form_value: str, candidate: Candidate) -> Conflict | None:
    """The headline check: a year count the Fact Bank cannot support."""
    claimed = _as_years(form_value)
    if claimed is None:
        return None

    actual = candidate.total_years_experience()
    if actual is None:
        return Conflict(
            field=field_id,
            severity=Severity.BLOCK,
            kind="unverifiable_duration",
            form_value=form_value,
            record_value="no dated experience",
            detail=(
                f"the form claims {claimed:g} years, and your Fact Bank has no dated "
                f"experience to support any number"
            ),
        )
    if claimed > actual + DURATION_TOLERANCE:
        return Conflict(
            field=field_id,
            severity=Severity.BLOCK,
            kind="overstated_duration",
            form_value=form_value,
            record_value=f"{actual:g} years",
            detail=(f"the form claims {claimed:g} years; your resume supports {actual:g}"),
        )
    if claimed + 1.0 < actual:
        # Understating is not a lie and is sometimes deliberate, but it costs the
        # candidate a screening filter, so it is worth surfacing.
        return Conflict(
            field=field_id,
            severity=Severity.WARN,
            kind="understated_duration",
            form_value=form_value,
            record_value=f"{actual:g} years",
            detail=f"the form claims {claimed:g} years; you have {actual:g}",
        )
    return None


def _check_identity(field_id: str, form_value: str, candidate: Candidate) -> Conflict | None:
    """Name, email and phone must match the profile the resume was built from.

    A mismatch here is usually a stale autofill from the browser profile rather than an
    agent error, and it is exactly the kind of thing nobody notices until a recruiter
    emails the wrong address.
    """
    value = _norm(form_value)
    if not value:
        return None

    if field_id == "contact.email":
        expected = _norm(str(candidate.email or ""))
        if expected and value != expected:
            return Conflict(
                field=field_id,
                severity=Severity.BLOCK,
                kind="identity_mismatch",
                form_value=form_value,
                record_value=str(candidate.email or ""),
                detail=f"the form has {form_value!r}; your profile has {candidate.email!r}",
            )

    if field_id == "contact.phone":
        expected_digits = _digits(candidate.phone or "")
        form_digits = _digits(form_value)
        # Compare the last ten digits: country codes and separators differ constantly
        # between a resume and a form, and that difference is not a contradiction.
        if expected_digits and form_digits and expected_digits[-10:] != form_digits[-10:]:
            return Conflict(
                field=field_id,
                severity=Severity.BLOCK,
                kind="identity_mismatch",
                form_value=form_value,
                record_value=candidate.phone or "",
                detail=f"the form has {form_value!r}; your profile has {candidate.phone!r}",
            )

    if field_id in {
        "contact.full_name",
        "contact.first_name",
        "contact.last_name",
        "contact.preferred_name",
    }:
        # Token-subset, not substring. A single-letter surname is normal — "Venkatesh D"
        # is a complete name — and a length filter on the parts blocked every
        # application for anyone with one. Found in the first end-to-end run.
        name_tokens = set(_norm(candidate.name).split())
        value_tokens = set(value.split())
        if name_tokens and value_tokens and not value_tokens <= name_tokens:
            return Conflict(
                field=field_id,
                severity=Severity.BLOCK,
                kind="identity_mismatch",
                form_value=form_value,
                record_value=candidate.name,
                detail=f"the form has {form_value!r}; your profile name is {candidate.name!r}",
            )
    return None


def _check_employer(field_id: str, form_value: str, candidate: Candidate) -> Conflict | None:
    """A current employer the Fact Bank does not contain."""
    if field_id != "employment.current_company" or not form_value.strip():
        return None

    value = _norm(form_value)
    employers = [_norm(f.label) for f in candidate.facts if f.kind is FactKind.EXPERIENCE]
    if not employers:
        return Conflict(
            field=field_id,
            severity=Severity.WARN,
            kind="unsupported_employer",
            form_value=form_value,
            record_value="no experience on record",
            detail=f"the form names {form_value!r} and your Fact Bank has no experience",
        )
    if not any(value in employer or employer in value for employer in employers):
        # Word-level overlap, because "Acme" on a form and "ML Intern, Acme Labs | 2024"
        # in the Fact Bank are the same employer.
        words = {w for w in value.split() if len(w) > 2}
        if not any(words & {w for w in employer.split() if len(w) > 2} for employer in employers):
            return Conflict(
                field=field_id,
                severity=Severity.BLOCK,
                kind="unsupported_employer",
                form_value=form_value,
                record_value="; ".join(employers[:2]),
                detail=f"the form names {form_value!r}, which is not in your experience",
            )
    return None


def _check_free_text(
    field_id: str, form_value: str, candidate: Candidate, supported: set[str]
) -> Conflict | None:
    """A long answer that introduces a technology the Fact Bank cannot support.

    Applies the same rule as the resume validator, one layer later: this catches a
    value that reached the form through an override or a human edit rather than through
    the answer agent, which the grounding check never sees.
    """
    if len(form_value or "") < 40:
        return None
    introduced = _technology_tokens(form_value) - supported
    if introduced:
        return Conflict(
            field=field_id,
            severity=Severity.BLOCK,
            kind="fabricated_technology",
            form_value=form_value[:120],
            record_value="not in your Fact Bank",
            detail=(
                f"the answer mentions {', '.join(sorted(introduced)[:4])}, which your "
                f"resume does not evidence"
            ),
        )
    return None


def _check_against_resume(
    field_id: str, form_value: str, resume: TailoredResume
) -> Conflict | None:
    """The form against the exact document being attached to it.

    The Fact Bank is the source of truth, but the *attached resume* is what the recruiter
    reads. A form naming a skill that was deliberately left off this resume variant is
    not a lie — so this is a warning, and only where the two will be read side by side.
    """
    if field_id != "experience.years_total" or not resume.summary:
        return None

    claimed = _as_years(form_value)
    if claimed is None:
        return None
    if match := re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*years", resume.summary, re.IGNORECASE):
        on_resume = float(match.group(1))
        if abs(on_resume - claimed) > DURATION_TOLERANCE:
            return Conflict(
                field=field_id,
                severity=Severity.BLOCK,
                kind="resume_form_mismatch",
                form_value=form_value,
                record_value=f"{on_resume:g} years on the attached resume",
                detail=(
                    f"the form says {claimed:g} years and the resume you are attaching "
                    f"says {on_resume:g} — a recruiter reads both"
                ),
            )
    return None


# ─── Entry point ────────────────────────────────────────────────────────────


def verify(
    filled: dict[str, str],
    candidate: Candidate,
    resume: TailoredResume | None = None,
) -> VerificationResult:
    """Check a filled form against the candidate record and the attached resume.

    `filled` maps canonical field id → the value that will be submitted.

    Returns every conflict found, not the first: a user fixing one problem should see
    all of them rather than discovering the next on the following attempt.
    """
    result = VerificationResult()
    supported = _supported_terms(candidate)

    for field_id, form_value in (filled or {}).items():
        if not str(form_value or "").strip():
            continue
        result.checked += 1

        checks = [
            _check_duration(field_id, form_value, candidate)
            if field_id == "experience.years_total"
            else None,
            _check_identity(field_id, form_value, candidate),
            _check_employer(field_id, form_value, candidate),
            _check_free_text(field_id, form_value, candidate, supported),
        ]
        if resume is not None:
            checks.append(_check_against_resume(field_id, form_value, resume))

        result.conflicts.extend(c for c in checks if c is not None)

    return result


def blocks_submission(result: VerificationResult) -> str:
    """A one-line reason to show the user, or "" when the form is consistent."""
    if not result.blocked:
        return ""
    first = result.blocking_conflicts[0]
    extra = len(result.blocking_conflicts) - 1
    suffix = f" (and {extra} more)" if extra else ""
    return f"{first.detail}{suffix}"
