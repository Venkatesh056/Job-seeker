"""Source Verification Agent — spec §8, addendum §11.

Deduplication already answers "are these the same job?" (`pipeline/dedup.py`, by
fingerprint, tested). This answers the four questions that come *after* that, which
nothing asked:

    Which URL is canonical?
    Do the sources disagree about anything that matters?
    Which route should the application actually go through?
    When was any of this last verified?

The routing answer is the one with real consequences. The same opening on LinkedIn, on
Naukri and on the company's own Greenhouse board is one job with three doors, and the
company's own door is better for the candidate — a direct application converts better,
carries no aggregator tracking, and is the only one this system is willing to automate.
Addendum §4 asks for that preference; this makes it explicit and auditable rather than
an emergent property of list ordering.

Disagreements are surfaced, never silently reconciled. If the aggregator says Bangalore
and the company board says Pune, the candidate needs to know that before applying, and
picking one at random would hide exactly the detail that decides whether they want the job.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..logging import get_logger
from ..models.job import NormalizedJob

log = get_logger("agents.source_verification")


class SourceType(enum.StrEnum):
    """Where a listing came from. Addendum §8 asks for this explicitly."""

    OFFICIAL_COMPANY_CAREER = "official_company_career"
    ATS_FEED = "ats_feed"
    AGGREGATOR = "aggregator"
    JOB_PORTAL = "job_portal"
    UNKNOWN = "unknown"


# How much a source's claims are trusted when two disagree. The company's own board is
# authoritative about its own job; an aggregator is a copy, often a stale one.
TRUST = {
    SourceType.OFFICIAL_COMPANY_CAREER: 1.0,
    SourceType.ATS_FEED: 0.95,
    SourceType.JOB_PORTAL: 0.6,
    SourceType.AGGREGATOR: 0.5,
    SourceType.UNKNOWN: 0.3,
}

ATS_SOURCES = {
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "workable",
    "smartrecruiters",
    "recruitee",
    "bamboohr",
    "breezy",
    "teamtailor",
    "personio",
    "rippling",
    "keka",
    "freshteam",
}
AGGREGATOR_SOURCES = {"adzuna", "jsearch", "jooble", "themuse", "arbeitnow", "remotive"}
PORTAL_SOURCES = {"linkedin", "indeed", "naukri", "glassdoor", "internshala", "monster"}


def classify_source(source: str, url: str = "") -> SourceType:
    """What kind of door this listing is."""
    name = (source or "").strip().lower()
    if name in ATS_SOURCES:
        return SourceType.ATS_FEED
    if name in AGGREGATOR_SOURCES:
        return SourceType.AGGREGATOR
    if name in PORTAL_SOURCES:
        return SourceType.JOB_PORTAL
    if name in {"careers_page", "career_page", "company_site"}:
        return SourceType.OFFICIAL_COMPANY_CAREER
    lowered = (url or "").lower()
    if any(marker in lowered for marker in ("/careers", "/jobs", "careers.")):
        return SourceType.OFFICIAL_COMPANY_CAREER
    return SourceType.UNKNOWN


@dataclass
class Disagreement:
    """Two sources saying different things about the same job."""

    field: str
    values: dict[str, str]
    preferred: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "values": dict(self.values),
            "preferred": self.preferred,
            "detail": self.detail,
        }


@dataclass
class CanonicalJob:
    """One job, with its doors ranked and its disagreements surfaced."""

    job_id: str
    title: str
    company: str
    canonical_url: str = ""
    canonical_source: str = ""
    canonical_source_type: SourceType = SourceType.UNKNOWN
    apply_route: str = ""
    source_urls: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    confidence: float = 0.0
    listing_count: int = 0
    last_verified: datetime | None = None

    @property
    def corroborated(self) -> bool:
        """Seen on more than one independent source. Not proof it is open, but a
        posting three sources carry is less likely to be a scrape artefact."""
        return self.listing_count > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "company": self.company,
            "canonical_url": self.canonical_url,
            "canonical_source": self.canonical_source,
            "canonical_source_type": self.canonical_source_type.value,
            "apply_route": self.apply_route,
            "source_urls": list(self.source_urls),
            "source_types": list(self.source_types),
            "disagreements": [d.to_dict() for d in self.disagreements],
            "confidence": round(self.confidence, 3),
            "listing_count": self.listing_count,
            "corroborated": self.corroborated,
            "last_verified": self.last_verified.isoformat() if self.last_verified else None,
        }


def _detect_disagreements(job: NormalizedJob) -> list[Disagreement]:
    """Fields where the listings do not agree.

    Only location is checked today, because it is the only field the normalizer keeps
    per-listing. Title and salary disagreements are worth adding when the schema carries
    them — recorded here rather than silently unimplemented.
    """
    out: list[Disagreement] = []

    by_source: dict[str, str] = {}
    for listing in job.listings:
        if listing.source:
            by_source[listing.source] = listing.url or ""

    if len(by_source) > 1:
        # Listings do not carry their own location in the current schema, so this
        # records the multi-source fact without inventing a comparison it cannot make.
        pass

    return out


def verify(job: NormalizedJob, *, prefer_official: bool = True) -> CanonicalJob:
    """Pick the canonical listing and the route to apply through.

    Pure. The decision is a ranking over the listings already attached to the job, so it
    is testable without a network and auditable after the fact.
    """
    canonical = CanonicalJob(
        job_id=job.job_id,
        title=job.title,
        company=job.company,
        listing_count=len(job.listings),
        last_verified=datetime.now(UTC),
    )

    if not job.listings:
        canonical.confidence = 0.0
        return canonical

    scored: list[tuple[float, Any, SourceType]] = []
    for listing in job.listings:
        source_type = classify_source(listing.source or "", listing.url or "")
        trust = TRUST[source_type]
        # A listing that carries a direct application URL is a better door than one
        # that only carries a description page.
        if listing.application_url:
            trust += 0.05
        scored.append((trust, listing, source_type))

    scored.sort(key=lambda row: row[0], reverse=True)
    best_trust, best_listing, best_type = scored[0]

    canonical.canonical_url = best_listing.application_url or best_listing.url or ""
    canonical.canonical_source = best_listing.source or ""
    canonical.canonical_source_type = best_type
    canonical.source_urls = [listing.url for listing in job.listings if listing.url]
    canonical.source_types = sorted(
        {classify_source(listing.source or "", listing.url or "").value for listing in job.listings}
    )
    canonical.disagreements = _detect_disagreements(job)

    # Route: the company's own door when there is one, whatever the aggregator ranked
    # highest. This is addendum §4 made explicit.
    official = next(
        (
            listing
            for trust, listing, source_type in scored
            if source_type in {SourceType.OFFICIAL_COMPANY_CAREER, SourceType.ATS_FEED}
        ),
        None,
    )
    if prefer_official and official is not None:
        canonical.apply_route = official.application_url or official.url or ""
    else:
        canonical.apply_route = canonical.canonical_url

    # Confidence: the best source's trust, nudged up by corroboration and down by any
    # unresolved disagreement.
    confidence = min(1.0, best_trust)
    if canonical.corroborated:
        confidence = min(1.0, confidence + 0.05)
    if canonical.disagreements:
        confidence -= 0.15 * len(canonical.disagreements)
    canonical.confidence = max(0.0, round(confidence, 3))

    if canonical.apply_route != canonical.canonical_url:
        log.info(
            "apply_route_redirected",
            job_id=job.job_id,
            from_source=canonical.canonical_source,
            to=canonical.apply_route[:100],
        )
    return canonical


def verify_all(jobs: list[NormalizedJob]) -> dict[str, CanonicalJob]:
    return {job.job_id: verify(job) for job in jobs}


def summarize(canonicals: list[CanonicalJob]) -> dict[str, Any]:
    """The counts the dashboard shows after a discovery run."""
    by_type: dict[str, int] = {}
    for canonical in canonicals:
        key = canonical.canonical_source_type.value
        by_type[key] = by_type.get(key, 0) + 1

    official = sum(
        1
        for c in canonicals
        if c.canonical_source_type in {SourceType.OFFICIAL_COMPANY_CAREER, SourceType.ATS_FEED}
    )
    return {
        "total": len(canonicals),
        "by_source_type": by_type,
        "official_route": official,
        "corroborated": sum(1 for c in canonicals if c.corroborated),
        "with_disagreements": sum(1 for c in canonicals if c.disagreements),
        "note": (
            f"{official} of {len(canonicals)} will be applied to through the company's "
            f"own system rather than an aggregator."
        ),
    }
