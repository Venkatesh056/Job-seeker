"""Company discovery: find *who is hiring* in a region, before finding *what*.

The existing ATS readers answer "what is open at Stripe?" — excellent, structured,
free, and useless until you already know to ask about Stripe. Discovery therefore
started from a hand-maintained seed list of company domains, which does not scale to
"every company in Bangalore hiring for data roles".

A directory source inverts it. Ask an aggregator for *jobs* matching a family in a
region, and keep the **company names** that come back. That yields a few hundred
employers who are demonstrably hiring right now, across every sector — MNCs, service
companies, product companies, manufacturers — because that is what an aggregator
indexes.

The two halves then compose, and this is the whole design:

    directory source  →  which companies are hiring here      (broad, metered, free tier)
    ATS reader        →  their real openings and apply URLs   (exact, unmetered, free)

So a hit from Adzuna is a *lead*, not a job. Applying still goes through the company's
own ATS, which is both more reliable to automate and better for the candidate than a
portal repost.

Every source degrades to an empty list. A missing key, an exhausted quota or a dead
endpoint returns nothing and records why — the campaign continues with whatever the
other sources found, because a partial answer beats a failed run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from ...logging import get_logger

log = get_logger("sources.directory")

# Suffixes stripped before comparing company names, so "Infosys Ltd", "Infosys
# Limited" and "Infosys" are one employer rather than three.
_SUFFIXES = re.compile(
    r"\b(private|pvt\.?|limited|ltd\.?|llp|inc\.?|incorporated|corp\.?|corporation|"
    r"company|co\.?|gmbh|plc|technologies|technology|solutions|services|systems|"
    r"labs|laboratories|software|consulting|india|global)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s&]")

# Staffing agencies and job-board house accounts. These post on behalf of employers
# they do not name, so a "company" row for them is a dead end: there is no careers
# page to resolve and no ATS to apply through.
RECRUITER_MARKERS = (
    "consultancy",
    "consultants",
    "staffing",
    "manpower",
    "recruit",
    "placement",
    "hr solutions",
    "hiring partner",
    "talent solutions",
    "job hai",
    "confidential",
    "client of",
    "leading mnc",
    "a leading",
    "our client",
    "hr services",
    "workforce",
)


def normalize_company(name: str) -> str:
    """A comparison key for an employer name."""
    text = _PUNCT.sub(" ", (name or "").lower())
    text = _SUFFIXES.sub(" ", text)
    return " ".join(text.split())


def looks_like_recruiter(name: str) -> bool:
    """Is this a staffing agency rather than the employer?

    Worth filtering at the door: these rows cannot be enriched (no careers page to
    resolve), cannot be applied to directly, and crowd out real employers in a capped
    result list.
    """
    lowered = f" {(name or '').lower()} "
    return any(marker in lowered for marker in RECRUITER_MARKERS)


@dataclass
class CompanyHit:
    """One employer, seen hiring for one thing, in one place."""

    name: str
    source: str
    # Everything below is optional: aggregators vary wildly in what they return.
    domain: str | None = None
    location: str = ""
    job_title: str = ""
    job_url: str = ""
    category: str = ""
    posted_date: date | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    description: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return normalize_company(self.name)

    @property
    def is_recruiter(self) -> bool:
        return looks_like_recruiter(self.name)


@dataclass
class Company:
    """An employer aggregated across every hit that mentioned them."""

    name: str
    key: str
    domain: str | None = None
    locations: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    families: list[str] = field(default_factory=list)
    sample_titles: list[str] = field(default_factory=list)
    job_urls: list[str] = field(default_factory=list)
    open_roles: int = 0
    newest_post: date | None = None
    # Filled later by ATS resolution; a company with a board can be applied to
    # directly, which is the only reason this whole layer exists.
    ats_provider: str | None = None
    ats_slug: str | None = None

    @property
    def applyable(self) -> bool:
        return bool(self.ats_provider and self.ats_slug)

    def absorb(self, hit: CompanyHit, family_id: str = "") -> None:
        self.open_roles += 1
        if hit.source not in self.sources:
            self.sources.append(hit.source)
        if hit.location and hit.location not in self.locations:
            self.locations.append(hit.location)
        if family_id and family_id not in self.families:
            self.families.append(family_id)
        if hit.job_title and hit.job_title not in self.sample_titles:
            self.sample_titles.append(hit.job_title)
        if hit.job_url and hit.job_url not in self.job_urls:
            self.job_urls.append(hit.job_url)
        if hit.domain and not self.domain:
            self.domain = hit.domain
        if hit.posted_date and (self.newest_post is None or hit.posted_date > self.newest_post):
            self.newest_post = hit.posted_date

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "key": self.key,
            "domain": self.domain,
            "locations": self.locations[:5],
            "sources": list(self.sources),
            "families": list(self.families),
            "sample_titles": self.sample_titles[:6],
            "job_urls": self.job_urls[:6],
            "open_roles": self.open_roles,
            "newest_post": self.newest_post.isoformat() if self.newest_post else None,
            "ats_provider": self.ats_provider,
            "ats_slug": self.ats_slug,
            "applyable": self.applyable,
        }


@dataclass
class SourceResult:
    """What one source returned, and what it cost. Failure is data, not an exception."""

    source: str
    hits: list[CompanyHit] = field(default_factory=list)
    calls: int = 0
    error: str = ""
    quota_exhausted: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


class DirectorySource(Protocol):
    """One aggregator. Implementations live beside this file."""

    id: str
    label: str
    needs_key: bool

    def configured(self) -> bool: ...

    async def search(
        self, query: str, location: str, *, client: Any, limit: int = 50, max_age_days: int = 30
    ) -> SourceResult: ...


def aggregate(
    results: list[tuple[str, SourceResult]], *, drop_recruiters: bool = True
) -> list[Company]:
    """Fold every source's hits into one employer list, best-attested first.

    Ordering is by how many distinct sources saw them, then by how many roles: a
    company three aggregators agree is hiring is a better lead than one that appeared
    once, and a company with eight open roles is a better lead than one with a single
    posting that may already be filled.
    """
    companies: dict[str, Company] = {}
    dropped = 0

    for family_id, result in results:
        for hit in result.hits:
            if drop_recruiters and hit.is_recruiter:
                dropped += 1
                continue
            key = hit.key
            if not key:
                continue
            company = companies.get(key)
            if company is None:
                company = Company(name=hit.name.strip(), key=key)
                companies[key] = company
            company.absorb(hit, family_id)

    if dropped:
        log.info("directory_recruiters_dropped", count=dropped)

    ordered = sorted(
        companies.values(),
        key=lambda c: (len(c.sources), c.open_roles, c.newest_post or date.min),
        reverse=True,
    )
    return ordered
