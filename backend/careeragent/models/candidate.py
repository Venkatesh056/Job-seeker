"""Candidate knowledge base and the Fact Bank.

The Fact Bank is the anti-fabrication mechanism. Every claim the system is
allowed to make about the candidate exists as a `Fact` with a resolvable
`evidence_locator`. Generation proposes; the validator checks each claim against
this bank; anything unsupported is a gap, not a bullet point.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel, EmailStr, Field


class FactKind(enum.StrEnum):
    SKILL = "skill"
    PROJECT = "project"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    CERTIFICATION = "certification"
    ACHIEVEMENT = "achievement"
    LINK = "link"
    PREFERENCE = "preference"


class EvidenceSource(enum.StrEnum):
    MASTER_RESUME = "master_resume"
    USER_ENTERED = "user_entered"
    PROJECT_README = "project_readme"
    CERTIFICATE = "certificate"


class Fact(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    kind: FactKind
    label: str  # 'PyTorch', 'Whisper fine-tuning', 'ML Intern @ Acme'
    body: str = ""
    evidence_source: EvidenceSource
    evidence_locator: str  # 'master_resume.pdf#page=1,line=14' — must resolve back
    first_used_at: date | None = None
    last_used_at: date | None = None
    strength: int = Field(default=3, ge=1, le=5)  # user-set only; never LLM-set
    verified: bool = False  # flipped by human review, not by the parser
    aliases: list[str] = Field(default_factory=list)

    @property
    def years_used(self) -> float | None:
        """Truthful experience duration, derived from dates rather than claimed.

        Returns None when the dates are unknown — the caller must then say
        nothing about duration rather than guess.
        """
        if not self.first_used_at:
            return None
        end = self.last_used_at or date.today()
        return round((end - self.first_used_at).days / 365.25, 1)

    def matches(self, term: str) -> bool:
        term = term.strip().lower()
        if not term:
            return False
        return term == self.label.lower() or term in {a.lower() for a in self.aliases}


class WorkAuthorization(BaseModel):
    citizenship: str | None = None
    authorized_countries: list[str] = Field(default_factory=list)
    requires_sponsorship: bool | None = None
    visa_status: str | None = None


class SalaryPreference(BaseModel):
    currency: str = "INR"
    minimum: float | None = None
    target: float | None = None
    period: str = "year"
    negotiable: bool = True


class Candidate(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str
    email: EmailStr | None = None
    phone: str | None = None
    location: str = ""
    preferred_locations: list[str] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)  # github, linkedin, portfolio

    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    salary_preferences: SalaryPreference = Field(default_factory=SalaryPreference)
    notice_period: str = ""
    availability: str = ""

    facts: list[Fact] = Field(default_factory=list)

    def skills(self) -> list[Fact]:
        return [f for f in self.facts if f.kind is FactKind.SKILL]

    def skill_labels(self) -> set[str]:
        out: set[str] = set()
        for fact in self.skills():
            out.add(fact.label.lower())
            out.update(a.lower() for a in fact.aliases)
        return out

    def supports(self, term: str) -> Fact | None:
        """The guard every generated claim passes through.

        Returns the backing fact, or None — in which case the claim must not be
        made. Callers treat None as a gap to record, never as a prompt to invent.
        """
        for fact in self.facts:
            if fact.matches(term):
                return fact
        return None

    def total_years_experience(self) -> float | None:
        spans = [
            (f.first_used_at, f.last_used_at or date.today())
            for f in self.facts
            if f.kind is FactKind.EXPERIENCE and f.first_used_at
        ]
        if not spans:
            return None
        # Merge overlapping spans — concurrent roles must not double-count.
        spans.sort()
        merged: list[tuple[date, date]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        days = sum((e - s).days for s, e in merged)
        return round(days / 365.25, 1)


class SearchConfig(BaseModel):
    """The structured form of a search. The natural-language command parser
    produces one of these for the user to review — it never triggers an action
    directly."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str = "default"
    roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    work_modes: list[str] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    experience_band: str | None = None
    salary_min: float | None = None
    keywords_include: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)
    companies_preferred: list[str] = Field(default_factory=list)
    companies_blocked: list[str] = Field(default_factory=list)
    company_domains: list[str] = Field(default_factory=list)
    max_job_age_days: int | None = 30
    sources: list[str] = Field(default_factory=list)
    limit: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
