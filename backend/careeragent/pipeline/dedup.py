"""Duplicate detection.

The same opening routinely appears on a portal, an aggregator and the company's
own ATS. They are one job with several listings — collapsing them is what stops
the system applying twice, which is the failure the candidate actually pays for.

Three tiers, cheapest first:

  1. exact fingerprint   company + normalized title + city
  2. normalized URL      the same application URL seen twice
  3. fuzzy               near-identical titles at the same company and city

Tier 3 uses token-set ratio rather than plain edit distance, because
"ML Engineer, Platform" and "Platform ML Engineer" are the same job and score
poorly on edit distance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

from rapidfuzz import fuzz

from ..logging import get_logger
from ..models.job import NormalizedJob, _first_location_part, _norm

log = get_logger("pipeline.dedup")

# Preferred order when merging: a direct ATS listing beats a portal repost,
# because that is where we would rather apply.
ATS_SOURCES = frozenset(
    {
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "smartrecruiters",
        "rippling",
        "workable",
        "recruitee",
        "bamboohr",
        "breezy",
        "teamtailor",
        "personio",
    }
)

_TRACKING_PARAMS = re.compile(r"^(utm_|gh_|ref|source|trk|src|from|campaign)", re.IGNORECASE)

FUZZY_TITLE_THRESHOLD = 88


def normalize_url(url: str) -> str:
    """Strip tracking noise so the same posting linked twice compares equal."""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip().lower()

    query = "&".join(
        part
        for part in (parsed.query or "").split("&")
        if part and not _TRACKING_PARAMS.match(part.split("=")[0])
    )
    path = parsed.path.rstrip("/") or "/"
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return urlunparse(("https", host, path, "", query, ""))


@dataclass
class DedupResult:
    jobs: list[NormalizedJob] = field(default_factory=list)
    duplicates_removed: int = 0
    merged_listings: int = 0


def _same_opening(a: NormalizedJob, b: NormalizedJob) -> bool:
    if _norm(a.company) != _norm(b.company):
        return False
    if _first_location_part(a.location) != _first_location_part(b.location):
        return False
    if not a.normalized_title or not b.normalized_title:
        return False
    return fuzz.token_set_ratio(a.normalized_title, b.normalized_title) >= FUZZY_TITLE_THRESHOLD


def _merge(target: NormalizedJob, other: NormalizedJob) -> int:
    """Fold `other` into `target`, keeping the richer field wherever they differ."""
    known_urls = {normalize_url(x.url) for x in target.listings}
    added = 0
    for listing in other.listings:
        if normalize_url(listing.url) not in known_urls:
            target.listings.append(listing)
            known_urls.add(normalize_url(listing.url))
            added += 1

    if len(other.description) > len(target.description):
        target.description = other.description
    if target.posted_date is None:
        target.posted_date = other.posted_date
    elif other.posted_date:
        target.posted_date = min(target.posted_date, other.posted_date)
    if target.salary is None:
        target.salary = other.salary
    if target.min_years_experience is None:
        target.min_years_experience = other.min_years_experience

    from ..models.job import EmploymentType, Seniority, WorkMode

    if target.work_mode is WorkMode.UNKNOWN:
        target.work_mode = other.work_mode
    if target.employment_type is EmploymentType.UNKNOWN:
        target.employment_type = other.employment_type
    if target.seniority is Seniority.UNKNOWN:
        target.seniority = other.seniority

    # Prefer the ATS listing's identity — that is where we want to apply.
    if other.ats in ATS_SOURCES and target.ats not in ATS_SOURCES:
        target.ats = other.ats

    target.first_seen_at = min(target.first_seen_at, other.first_seen_at)
    target.last_seen_at = max(target.last_seen_at, other.last_seen_at)
    return added


def deduplicate(jobs: list[NormalizedJob]) -> DedupResult:
    """Collapse duplicates. Input order is preserved for the survivors."""
    result = DedupResult()
    by_fingerprint: dict[str, NormalizedJob] = {}
    by_url: dict[str, NormalizedJob] = {}
    # Bucket by company so fuzzy comparison stays O(n) in practice rather than
    # O(n²) across the whole corpus.
    by_company: dict[str, list[NormalizedJob]] = {}

    for job in jobs:
        fingerprint = job.fingerprint or job.compute_fingerprint()

        existing = by_fingerprint.get(fingerprint)

        if existing is None:
            for listing in job.listings:
                if hit := by_url.get(normalize_url(listing.url)):
                    existing = hit
                    break

        if existing is None:
            for candidate in by_company.get(_norm(job.company), []):
                if _same_opening(candidate, job):
                    existing = candidate
                    break

        if existing is not None:
            result.merged_listings += _merge(existing, job)
            result.duplicates_removed += 1
            log.debug(
                "duplicate_merged",
                company=job.company,
                title=job.title,
                kept=existing.listings[0].source,
            )
            continue

        by_fingerprint[fingerprint] = job
        by_company.setdefault(_norm(job.company), []).append(job)
        for listing in job.listings:
            by_url[normalize_url(listing.url)] = job
        result.jobs.append(job)

    return result
