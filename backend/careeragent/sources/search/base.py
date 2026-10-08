"""What a web search returns, and what may be believed about it.

A search result is the least trustworthy input in this system. A job board's API returns
a structured posting from a known company; a web search returns whatever ranked well for
a phrase, written by anyone, including pages that exist to be found by exactly this kind
of query. So the types here keep two things separate on purpose: the **fact** that a page
exists at a URL, which is verifiable, and the **text** on it, which is a claim.

Nothing here decides anything. It fetches, filters obvious non-employers, and hands back
hits for something else to judge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

# Hosts that are never the employer. A search for "AI companies hiring in Bangalore"
# returns job boards, listicles, and directory pages far above any company's own site,
# and treating those as employers is how a company list fills up with "LinkedIn".
AGGREGATOR_MARKERS = (
    "adzuna",
    "ambitionbox",
    "angel.co",
    "bayt",
    "bebee",
    "builtin",
    "clutch.co",
    "crunchbase",
    "cutshort",
    "designrush",
    "dice.com",
    "f6s.com",
    "foundit",
    "freshersworld",
    "glassdoor",
    "goodfirms",
    "greenhouse.io",
    "hirist",
    "iimjobs",
    "indeed",
    "instahyre",
    "internshala",
    "jobsforher",
    "jooble",
    "lever.co",
    "linkedin",
    "monster",
    "naukri",
    "quora",
    "rapidapi",
    "reddit",
    "shine.com",
    "simplyhired",
    "themanifest",
    "themuse",
    "timesjobs",
    "topdevelopers",
    "wellfound",
    "wikipedia",
    "workday",
    "ycombinator",
    "youtube",
    "ziprecruiter",
)

# Hostnames whose first label is a generic job word rather than a brand. Measured against
# live results: a search for employers in Bangalore returns `startup.jobs`, `freshers.jobs`
# and `nextleap.app` alongside `amazon.jobs`, and only the last is an employer. The `.jobs`
# TLD does not separate them — the word in front of it does.
GENERIC_LABELS = frozenset(
    {
        "apply",
        "career",
        "careers",
        "employment",
        "fresher",
        "freshers",
        "graduate",
        "graduates",
        "hire",
        "hiring",
        "intern",
        "internship",
        "internships",
        "job",
        "jobs",
        "opening",
        "openings",
        "recruit",
        "recruiter",
        "recruiting",
        "recruitment",
        "startup",
        "startups",
        "vacancies",
        "vacancy",
        "work",
    }
)

# Content farms and social platforms. Separate from the list above because these are not
# job boards at all — they are pages *about* companies, which is not a company.
CONTENT_MARKERS = (
    "blogspot",
    "facebook",
    "instagram",
    "medium.com",
    "pinterest",
    "slideshare",
    "substack",
    "twitter",
    "x.com",
)


def host_of(url: str) -> str:
    """The bare hostname, lowercased, without `www.`. Empty when the URL is unusable."""
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def brand_label(host: str) -> str:
    """The label that identifies the employer in a hostname.

    `amazon.jobs` and `careers.amazon.com` are both Amazon. A careers subdomain puts the
    brand second; a `.jobs` or `.careers` TLD puts it first. Returns "" when no label is
    a brand — which is what disqualifies `startup.jobs`.
    """
    labels = [label for label in (host or "").lower().split(".") if label]
    if not labels:
        return ""
    # Drop a leading careers-style subdomain: careers.amazon.com -> amazon.com
    while len(labels) > 2 and labels[0] in GENERIC_LABELS:
        labels = labels[1:]
    head = labels[0]
    return "" if head in GENERIC_LABELS else head


def is_employer_site(url: str) -> bool:
    """Could this URL plausibly be a company's own site?

    A negative answer is reliable — these hosts are definitely not employers. A positive
    answer is only "not obviously disqualified", which is why nothing downstream treats it
    as proof that a company exists; the company's own site is read before anything is
    believed about it.
    """
    host = host_of(url)
    if not host or "." not in host:
        return False
    if any(marker in host for marker in (*AGGREGATOR_MARKERS, *CONTENT_MARKERS)):
        return False
    return bool(brand_label(host))


# A page whose title reads like a ranked list is writing *about* companies, not being one.
# Measured against live results: "Top Data Analytics Companies in Bangalore" and "Top Hiring
# Companies for Data Analysts" were both being taken as employers.
#
# Judging on a title is normally the wrong move here — a title is text someone wrote to rank
# for a phrase, and this layer's whole posture is that such text is a claim, not a fact. It
# is safe in this one direction only: the title is used to *reject*, never to believe. The
# worst case is dropping a real employer whose homepage title happens to start "Top", which
# costs one lead; the opposite mistake fills the company list with content farms.
_LISTICLE_PHRASES = (
    "companies in",
    "companies for",
    "companies hiring",
    "best companies",
    "top companies",
    "top 10",
    "top 20",
    "top 50",
    "list of",
)


def looks_like_a_listicle(title: str) -> bool:
    lowered = f" {(title or '').lower()} "
    return any(phrase in lowered for phrase in _LISTICLE_PHRASES)


@dataclass
class SearchHit:
    """One result. `url` is a fact; `title` and `snippet` are claims someone wrote."""

    title: str
    url: str
    snippet: str = ""
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return host_of(self.url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "host": self.host,
            "source": self.source,
        }


@dataclass
class SearchResult:
    """One provider's answer to one query, including how much it cost."""

    provider: str
    query: str = ""
    hits: list[SearchHit] = field(default_factory=list)
    calls: int = 0
    error: str = ""
    # Set when the provider says the quota is gone, so the caller stops asking rather
    # than spending the rest of a run on failures.
    quota_exhausted: bool = False

    @property
    def ok(self) -> bool:
        return not self.error

    def employer_hits(self) -> list[SearchHit]:
        """Hits that could be a company's own site, deduplicated by host."""
        seen: dict[str, SearchHit] = {}
        for hit in self.hits:
            if looks_like_a_listicle(hit.title):
                continue
            if is_employer_site(hit.url) and hit.host not in seen:
                seen[hit.host] = hit
        return list(seen.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "query": self.query,
            "hits": [h.to_dict() for h in self.hits],
            "count": len(self.hits),
            "calls": self.calls,
            "error": self.error or None,
            "quota_exhausted": self.quota_exhausted,
        }


class SearchProvider(Protocol):
    """One web-search backend.

    Every implementation fails into an empty `SearchResult` rather than raising, for the
    same reason the directory sources do: a company hunt that dies because one free tier
    ran out is worse than one that returns fewer companies.
    """

    id: str
    label: str
    signup: str

    def configured(self) -> bool: ...

    async def search(
        self, query: str, *, client: Any, limit: int = 10, region: str = ""
    ) -> SearchResult: ...
