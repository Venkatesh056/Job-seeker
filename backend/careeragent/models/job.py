"""Job models: the shape every source normalizes into."""

from __future__ import annotations

import enum
import hashlib
import re
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class WorkMode(enum.StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(enum.StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


class Seniority(enum.StrEnum):
    INTERN = "intern"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    UNKNOWN = "unknown"


class JobStatus(enum.StrEnum):
    OPEN = "open"
    EXPIRED = "expired"
    FILLED = "filled"
    UNKNOWN = "unknown"


class Salary(BaseModel):
    min: float | None = None
    max: float | None = None
    currency: str | None = None
    period: str | None = None  # year | month | hour
    raw: str | None = None


class RawJob(BaseModel):
    """A posting exactly as one source returned it.

    Deliberately permissive: sources differ wildly and a strict model here would
    mean discarding real jobs over a malformed date. Cleaning happens in the
    normalizer, where it is testable.
    """

    source: str  # 'greenhouse' | 'lever' | 'careers_page' | …
    source_job_id: str
    title: str
    url: str
    company_name: str | None = None
    location: str = ""
    department: str = ""
    team: str = ""
    employment_type: str = ""
    remote: bool | None = None
    published_at: str | None = None
    updated_at: str | None = None
    posted_label: str | None = None  # Workday gives "Posted Yesterday", not a date
    description: str = ""
    salary: str = ""
    board_slug: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JobListing(BaseModel):
    """One place a logical job was seen. A job on LinkedIn + Indeed + the company
    ATS is three listings and one `NormalizedJob`."""

    source: str
    source_job_id: str
    url: str
    application_url: str | None = None
    ats: str | None = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NormalizedJob(BaseModel):
    job_id: str = ""
    company: str = ""
    company_domain: str | None = None
    title: str = ""
    normalized_title: str = ""
    location: str = ""
    work_mode: WorkMode = WorkMode.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    seniority: Seniority = Seniority.UNKNOWN
    salary: Salary | None = None
    description: str = ""

    requirements: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    education_requirements: list[str] = Field(default_factory=list)
    min_years_experience: float | None = None
    max_years_experience: float | None = None

    posted_date: date | None = None
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: JobStatus = JobStatus.OPEN

    listings: list[JobListing] = Field(default_factory=list)
    ats: str | None = None
    fingerprint: str = ""

    @field_validator("title", "company", "location")
    @classmethod
    def _collapse_space(cls, v: str) -> str:
        return " ".join(v.split())

    @property
    def primary_url(self) -> str | None:
        """Where a human should be sent. Prefers a direct ATS application URL —
        direct applications convert better than portal reposts."""
        for listing in self.listings:
            if listing.ats and listing.application_url:
                return listing.application_url
        for listing in self.listings:
            if listing.application_url:
                return listing.application_url
        return self.listings[0].url if self.listings else None

    @property
    def age_days(self) -> int | None:
        if self.posted_date is None:
            return None
        return (datetime.now(UTC).date() - self.posted_date).days

    def compute_fingerprint(self) -> str:
        """Stable identity for deduplication across sources.

        Location is reduced to its first component ("Bangalore, India" →
        "bangalore") because the same opening is routinely listed with different
        location granularity on different boards.
        """
        city = _first_location_part(self.location)
        basis = f"{_norm(self.company)}|{self.normalized_title}|{city}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


# ─── Normalization helpers (pure; unit-tested) ──────────────────────────────

_SENIORITY_NOISE = re.compile(
    r"\b(?:senior|sr\.?|junior|jr\.?|staff|principal|lead|head|chief|associate|"
    r"entry[- ]level|mid[- ]level|i{1,3}|1|2|3|iv|v)\b",
    re.IGNORECASE,
)
_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_PUNCT = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")

# Expanded before noise-stripping so "sr. ml eng" and "Senior Machine Learning
# Engineer" converge on the same normalized title.
_TITLE_SYNONYMS = {
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "nlp": "natural language processing",
    "swe": "software engineer",
    "sde": "software engineer",
    "eng": "engineer",
    "engr": "engineer",
    "dev": "developer",
    "sci": "scientist",
    "genai": "generative artificial intelligence",
    "llm": "large language model",
}


def _norm(text: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def _first_location_part(location: str) -> str:
    if not location:
        return ""
    return _norm(location.split(",")[0])


def normalize_title(title: str) -> str:
    """Reduce a job title to a comparable form.

    'Senior ML Engineer (Remote) - II' → 'machine learning engineer'
    """
    text = _BRACKETED.sub(" ", title)
    text = _norm(text)
    words = [_TITLE_SYNONYMS.get(w, w) for w in text.split()]
    text = " ".join(words)
    text = _SENIORITY_NOISE.sub(" ", text)
    return _WS.sub(" ", text).strip()


def detect_work_mode(*texts: str) -> WorkMode:
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return WorkMode.UNKNOWN
    if "hybrid" in blob:
        return WorkMode.HYBRID
    if re.search(r"\b(remote|work from home|wfh|distributed)\b", blob):
        # "remote" inside a negation is common: "this role is not remote"
        if re.search(r"\b(not|no|non)[- ]remote\b", blob):
            return WorkMode.ONSITE
        return WorkMode.REMOTE
    if re.search(r"\b(on[- ]?site|in[- ]office|in person)\b", blob):
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN


def detect_employment_type(*texts: str) -> EmploymentType:
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return EmploymentType.UNKNOWN
    if re.search(r"\b(intern|internship|trainee|apprentice)\b", blob):
        return EmploymentType.INTERNSHIP
    if re.search(r"\b(contract|contractor|freelance|c2h|consultant)\b", blob):
        return EmploymentType.CONTRACT
    if re.search(r"\bpart[- ]time\b", blob):
        return EmploymentType.PART_TIME
    if re.search(r"\btemporary\b|\btemp\b", blob):
        return EmploymentType.TEMPORARY
    if re.search(r"\bfull[- ]time\b|\bpermanent\b", blob):
        return EmploymentType.FULL_TIME
    return EmploymentType.UNKNOWN


def detect_seniority(title: str, description: str = "") -> Seniority:
    blob = f"{title} {description[:600]}".lower()
    lowered = title.lower()
    if re.search(r"\b(intern|internship|trainee|apprentice)\b", blob):
        return Seniority.INTERN
    # "Architect" belongs here. Missing it let "Technical Architect - ML" top the
    # list for a student searching for an internship, because the title matched
    # the target role perfectly and nothing marked it as out of reach.
    # "Associate Technical Architect" is still an architect role.
    if re.search(
        r"\b(principal|distinguished|staff|architect|fellow|vp|vice president|chief|head)\b",
        lowered,
    ):
        return Seniority.LEAD
    if re.search(r"\b(lead|head of|manager|director)\b", lowered):
        return Seniority.LEAD
    if re.search(r"\b(senior|sr\.?|iii)\b", lowered):
        return Seniority.SENIOR
    if re.search(r"\b(junior|jr\.?|entry[- ]level|graduate|fresher|new grad)\b", blob):
        return Seniority.ENTRY
    return Seniority.UNKNOWN


# The word boundaries are load-bearing: without them "2024 years" matches as the
# range 20–24, turning a calendar year in a company blurb into a requirement.
_YEARS = re.compile(
    r"\b(\d{1,2})\b\s*(?:\+|plus)?\s*(?:[-–]|to)?\s*(?:\b(\d{1,2})\b)?\s*\+?\s*years?",
    re.IGNORECASE,
)


def extract_years_experience(text: str) -> tuple[float | None, float | None]:
    """Pull a years-of-experience range out of a description.

    Only looks at sentences mentioning experience, so a line like '3 years of
    runway' in a company blurb does not become a requirement.
    """
    if not text:
        return None, None
    best: tuple[float | None, float | None] | None = None
    for sentence in re.split(r"[.\n;]", text[:4000]):
        if "experience" not in sentence.lower():
            continue
        if m := _YEARS.search(sentence):
            lo = float(m.group(1))
            hi = float(m.group(2)) if m.group(2) else None
            if lo > 25:  # '2024 years' — a parsed year, not a duration
                continue
            if best is None or lo < (best[0] or 99):
                best = (lo, hi)
    return best if best else (None, None)
