"""Finding employers by searching the web, rather than by reading a job board.

The job boards answer "what is open at a company we already know about". That is a
narrow question, and this project measured how narrow: 79 known boards, 1,529 postings,
and 2 of them worth applying to. Most employers in a city are not on any aggregator this
project can read, and the ones that are appear late and generically.

So this asks the other question — *which companies exist here at all* — and hands the
answer to the ATS layer, which already knows how to find a company's board from its
domain and read it forever afterwards for free. One search buys a board that keeps
paying out.

    web search  →  company domain  →  ATS discovery  →  board  →  monitored free

Three rules shape everything here.

**A search result is a claim, not a fact.** The URL is real; the title and snippet were
written by whoever wanted to rank for that phrase. Nothing here believes a snippet. A
company is only considered real once its own site has been read by the ATS layer.

**Cheap budget first.** Tavily's free tier is 1,000 credits a month and SerpAPI's is 250
searches, so Tavily is asked first and SerpAPI only answers when Tavily cannot. The usual
instinct — best provider first — would empty the small budget in one run.

**Every call is counted.** Neither provider's balance is visible per request, so calls are
recorded locally and shown on the Keys page as a floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ...config import get_settings
from ...logging import get_logger
from ..directory.base import looks_like_recruiter, normalize_company
from ..directory.providers import country_code_for
from .base import (
    AGGREGATOR_MARKERS,
    CONTENT_MARKERS,
    GENERIC_LABELS,
    SearchHit,
    SearchProvider,
    SearchResult,
    brand_label,
    host_of,
    is_employer_site,
    looks_like_a_listicle,
)
from .providers import PROVIDERS, PROVIDERS_BY_ID, SerpAPI, Tavily, available, describe

log = get_logger("sources.search")

__all__ = [
    "AGGREGATOR_MARKERS",
    "CONTENT_MARKERS",
    "GENERIC_LABELS",
    "PROVIDERS",
    "PROVIDERS_BY_ID",
    "CompanyLead",
    "Harvest",
    "SearchHit",
    "SearchProvider",
    "SearchResult",
    "SerpAPI",
    "Tavily",
    "available",
    "brand_label",
    "describe",
    "find_companies",
    "harvest",
    "host_of",
    "http_client",
    "is_employer_site",
    "looks_like_a_listicle",
    "queries_for",
    "search",
]


def http_client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=True,
        headers={"user-agent": settings.user_agent},
    )


# ─── One query, whichever provider can answer it ────────────────────────────


async def search(
    query: str,
    *,
    client: httpx.AsyncClient | None = None,
    limit: int = 10,
    region: str = "",
    session: Any = None,
    skip: set[str] | None = None,
) -> SearchResult:
    """Ask the cheapest configured provider that is still working.

    Falls through to the next provider on an error or an exhausted quota, and returns the
    last failure if none can answer — never raises, and never silently returns an empty
    result that looks like "no companies here".

    Passing `session` records the call against the provider's local count, which is what
    the Keys page reports. The count is a floor, not a balance: it cannot see calls made
    with the same key from anywhere else.
    """
    from ... import integrations

    # `skip or set()` would be wrong: an empty set is falsy, so a caller passing a fresh
    # one would get a throwaway copy and every exhaustion recorded below would be lost —
    # which is exactly the case `find_companies` hits on its first query.
    skip = set() if skip is None else skip
    owns_client = client is None
    client = client or http_client()
    failures: list[SearchResult] = []

    try:
        for provider in PROVIDERS:
            if provider.id in skip or not provider.configured():
                continue

            result = await provider.search(query, client=client, limit=limit, region=region)

            if session is not None and result.calls:
                await integrations.record_call(
                    session, provider.id, calls=result.calls, errors=0 if result.ok else 1
                )

            if result.ok and result.hits:
                return result

            failures.append(result)
            if result.quota_exhausted:
                # Not a transient failure. Say so, so a caller running many queries can
                # stop asking this provider for the rest of the run.
                log.info("search_provider_exhausted", provider=provider.id)
                skip.add(provider.id)
            else:
                log.debug("search_provider_failed", provider=provider.id, error=result.error)
    finally:
        if owns_client:
            await client.aclose()

    if failures:
        # The first failure is the cheapest provider's, which is the one worth reporting.
        return failures[0]
    return SearchResult(
        provider="none",
        query=query,
        error="no web-search provider is configured — add a Tavily or SerpAPI key",
    )


# ─── Turning results into company leads ─────────────────────────────────────


@dataclass
class CompanyLead:
    """A host that might be an employer, and why we think so.

    Deliberately thin. It carries no industry, no size, no description — those would have
    to come from a search snippet, and a snippet is written by whoever wanted to rank for
    the phrase. Everything real about a company is read from the company's own site later.
    """

    host: str
    name: str
    # The queries that surfaced it. A host found by several different queries is a better
    # lead than one found by a single lucky phrase.
    queries: list[str] = field(default_factory=list)
    hits: int = 0
    sample_title: str = ""
    sample_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "name": self.name,
            "hits": self.hits,
            "queries": self.queries,
            "sample_title": self.sample_title,
            "sample_url": self.sample_url,
        }


def _name_from_host(host: str) -> str:
    """A readable company name from a hostname.

    A guess, and labelled as one everywhere it is used: `zoho.com` → "Zoho" is right,
    `tcs.com` → "Tcs" is not. The company's own site corrects it later; this exists so a
    lead list is readable before that happens.

    Uses the brand label rather than the first one, so `careers.amazon.com` and
    `amazon.jobs` both read as "Amazon" instead of "Careers" and "Amazon".
    """
    label = brand_label(host) or host.split(".")[0]
    label = label.replace("-", " ").replace("_", " ").strip()
    return label.title() if label.islower() else label


def queries_for(role: str, location: str, *, extra: list[str] | None = None) -> list[str]:
    """The phrases worth spending a search on, for one role in one place.

    Phrased to find companies rather than postings. "data analyst jobs in Bangalore"
    returns Naukri and LinkedIn; "companies in Bangalore hiring data analysts" returns
    more company sites and fewer boards — and the ones it still returns are filtered.

    Kept small on purpose: each entry is a real call against a metered free tier.
    """
    role = (role or "").strip()
    location = (location or "").strip()
    if not role or not location:
        return []

    queries = [
        # Phrased around the company, not the posting. Asking for "data analyst jobs in
        # Bangalore" returns Naukri and LinkedIn; asking which companies are hiring
        # returns company sites, and the boards that still slip through are filtered.
        f"companies in {location} hiring {role}",
        f"{role} openings {location} company careers page",
        f"tech companies {location} {role} team",
        f"startups {location} hiring {role} apply",
    ]
    queries.extend(extra or [])
    return queries


async def find_companies(
    role: str,
    location: str,
    *,
    client: httpx.AsyncClient | None = None,
    session: Any = None,
    per_query: int = 10,
    max_queries: int = 4,
) -> tuple[list[CompanyLead], list[SearchResult]]:
    """Search for employers hiring for a role in a place.

    Returns the leads, best first, and every raw result so the caller can see what each
    query cost and which provider answered. A lead is a *candidate*: nothing here has
    confirmed the host is a company, only that it is not obviously a job board.

    Recruiters are dropped by name, because a staffing agency's site has no careers board
    to resolve and it crowds out real employers in a capped list.
    """
    owns_client = client is None
    client = client or http_client()
    region = country_code_for(location)
    leads: dict[str, CompanyLead] = {}
    results: list[SearchResult] = []
    skip: set[str] = set()

    try:
        for query in queries_for(role, location)[:max_queries]:
            result = await search(
                query,
                client=client,
                limit=per_query,
                region=region,
                session=session,
                skip=skip,
            )
            results.append(result)
            if result.quota_exhausted:
                skip.add(result.provider)
            if not result.ok:
                continue

            for hit in result.employer_hits():
                # `careers.acme.com` and `acme.com` are one employer, so leads are keyed
                # on the brand rather than the hostname. `acme.jobs` stays as it is: it is
                # a real careers site and the ATS layer can read it directly.
                host = hit.host
                labels = host.split(".")
                while len(labels) > 2 and labels[0] in GENERIC_LABELS:
                    labels = labels[1:]
                host = ".".join(labels)
                if not host or not is_employer_site(f"https://{host}"):
                    continue

                name = _name_from_host(host)
                if looks_like_recruiter(name) or looks_like_recruiter(hit.title):
                    continue

                lead = leads.get(host)
                if lead is None:
                    lead = CompanyLead(
                        host=host,
                        name=name,
                        sample_title=hit.title,
                        sample_url=hit.url,
                    )
                    leads[host] = lead
                lead.hits += 1
                if query not in lead.queries:
                    lead.queries.append(query)
    finally:
        if owns_client:
            await client.aclose()

    # A host several different queries agree on is a better lead than a single lucky one.
    ordered = sorted(leads.values(), key=lambda lead: (-len(lead.queries), -lead.hits, lead.host))
    log.info(
        "search_companies_found",
        role=role,
        location=location,
        leads=len(ordered),
        queries=len(results),
        calls=sum(r.calls for r in results),
    )
    return ordered, results


def known_hosts(names: list[str]) -> set[str]:
    """Comparison keys for companies already held, so a lead list can exclude them."""
    return {normalize_company(name) for name in names if name}


# ─── From a lead to a board that pays out forever ───────────────────────────


@dataclass
class Harvest:
    """What a search run actually bought.

    The only number here that matters is `boards`. A lead is a hostname someone's SEO put
    in front of us; a board is a feed this project can read for free, for as long as the
    company keeps using it. Everything else is bookkeeping on the way to that.
    """

    role: str = ""
    location: str = ""
    leads: list[CompanyLead] = field(default_factory=list)
    checked: int = 0
    companies_added: int = 0
    boards: list[dict[str, str]] = field(default_factory=list)
    calls: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "location": self.location,
            "leads": [lead.to_dict() for lead in self.leads],
            "lead_count": len(self.leads),
            "checked": self.checked,
            "companies_added": self.companies_added,
            "boards": self.boards,
            "board_count": len(self.boards),
            "search_calls": self.calls,
            "errors": self.errors[:6],
            "note": self.note(),
        }

    def note(self) -> str:
        if not self.leads:
            return (
                f"No company leads for {self.role} in {self.location}. The search returned "
                "job boards and listicles rather than employers."
            )
        if not self.boards:
            return (
                f"{len(self.leads)} lead(s), none with a readable careers board. A company "
                "whose board cannot be found has to be applied to by hand."
            )
        return (
            f"{len(self.boards)} board(s) from {self.checked} lead(s) checked. Each one is "
            "now monitored for free — the search cost is paid once."
        )


async def harvest(
    role: str,
    location: str,
    *,
    session: Any,
    candidate_id: str = "",
    max_leads: int = 12,
    per_query: int = 10,
) -> Harvest:
    """Search for employers, then resolve each one's careers board and keep it.

    This is the whole point of the layer. A search costs a metered credit and returns a
    hostname; an ATS board costs nothing to read and keeps returning openings for as long
    as the company hires. Converting the first into the second is what makes the spend
    worth it, so the report counts boards rather than leads.

    Leads already in the database are skipped before their site is read — re-resolving a
    board we already monitor would be a wasted request against someone else's server.
    """
    from sqlalchemy import select

    from ...storage import repository as repo
    from ...storage.models import Company
    from ..ats.discover import discover_from_website

    out = Harvest(role=role, location=location)

    async with http_client() as client:
        leads, results = await find_companies(
            role, location, client=client, session=session, per_query=per_query
        )
        out.calls = sum(r.calls for r in results)
        out.errors = [f"{r.provider}: {r.error}" for r in results if r.error]

        known = {
            (row[0] or "").lower()
            for row in (await session.execute(select(Company.domain))).all()
            if row[0]
        }
        out.leads = [lead for lead in leads if lead.host.lower() not in known][:max_leads]

        for lead in out.leads:
            out.checked += 1
            try:
                found = await discover_from_website(lead.host, client)
            except Exception as exc:
                # One unreachable site must not end the run: the others are still worth
                # resolving, and a site that is down now may be up next time.
                log.debug("lead_unreadable", host=lead.host, error=f"{type(exc).__name__}")
                continue
            if not found:
                continue

            company = await repo.upsert_company(session, lead.name, lead.host)
            out.companies_added += 1
            for provider_id, slug in found:
                # job_count 0: the board has not been read yet. Monitoring treats a board
                # it has never crawled as due immediately, which is exactly right here.
                await repo.upsert_ats_board(
                    session, provider_id, slug, company.id, "web_search", 0, True
                )
                out.boards.append({"company": company.name, "provider": provider_id, "slug": slug})

    log.info(
        "search_harvest",
        role=role,
        location=location,
        leads=len(out.leads),
        boards=len(out.boards),
        calls=out.calls,
    )
    return out
