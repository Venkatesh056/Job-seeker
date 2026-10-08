"""Domain models.

These Pydantic models *are* the normalized schema described in
docs/01-architecture.md §5 — not a validation layer over something else. Every
source adapter's job is to produce a `RawJob`; every downstream agent consumes a
`NormalizedJob`. Nothing else crosses module boundaries.
"""

from .candidate import Candidate, Fact, FactKind, SearchConfig
from .job import (
    EmploymentType,
    JobListing,
    JobStatus,
    NormalizedJob,
    RawJob,
    Seniority,
    WorkMode,
)
from .scoring import DeterministicScore, JobScore, Priority

__all__ = [
    "Candidate",
    "DeterministicScore",
    "EmploymentType",
    "Fact",
    "FactKind",
    "JobListing",
    "JobScore",
    "JobStatus",
    "NormalizedJob",
    "Priority",
    "RawJob",
    "SearchConfig",
    "Seniority",
    "WorkMode",
]
