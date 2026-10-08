"""Apollo: company facts and job postings, keyed on a domain.

Apollo is sold as a contact database, and that is not what it is used for here. **The free
plan refuses people search outright** — `mixed_people/search` and `mixed_companies/search`
both return 403 `API_INACCESSIBLE`, measured against the live key — so contact discovery is
not on the table at this tier. What the free plan does allow turns out to be worth more:

    organizations/enrich              industry, size, tech stack, headcount by department
    organizations/{id}/job_postings   every opening, with a date and a URL

Both matter for a specific, measured reason. `company_fit` is one of eight components in
the opportunity score, and it is **0 for every company that has not been researched** — the
audit found that nothing in the database could reach "high priority" because of it. Reading
a company's own site fixes that one company at a time and only when the site is readable;
one enrichment call fills the same fields for any company with a domain.

And the job postings are a discovery source that needs no board slug to be guessed. Apollo
returned 147 postings for Quantiphi and 29 for Zoho, each with `posted_at` — which is what
the freshness rule needs and what most aggregators omit.

**On privacy.** Nothing here requests, stores or returns a person. The two endpoints used
describe an organisation and its openings. That is a deliberate boundary, not an accident
of the plan tier: if people search were enabled tomorrow, using it would need its own
decision about whose data is being collected and why.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

from ..config import get_settings
from ..logging import get_logger

log = get_logger("sources.apollo")

BASE = "https://api.apollo.io/api/v1"

# Apollo's own words when the plan does not include an endpoint. Recognised so an
# upgrade-gated feature reads as "your plan does not include this" rather than as a bug.
PLAN_GATED = "API_INACCESSIBLE"

# And when the credits for the billing cycle are gone. Apollo returns this as HTTP 422,
# which is otherwise "malformed request" — so without recognising the code, an exhausted
# account looks exactly like a bad domain, and a run keeps asking. Measured the hard way:
# 67 companies were checked, the credits ran out partway, and the last six read as six
# unrelated company failures.
CREDITS_EXHAUSTED = "BILLING.LIMIT.CREDITS_EXHAUSTED"


def _error_code(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    details = body.get("error_details")
    if isinstance(details, dict):
        return str(details.get("code") or "")
    return str(body.get("error_code") or "")


def _exhausted(body: Any) -> bool:
    return _error_code(body) == CREDITS_EXHAUSTED


def rate_limits(response: Any) -> dict[str, Any]:
    """The quota Apollo publishes on every response.

    Real numbers from the provider rather than our own count, which is the difference the
    Keys page draws between a balance and a floor.
    """
    headers = getattr(response, "headers", {}) or {}
    out: dict[str, Any] = {}
    for header, name in (
        ("x-rate-limit-minute", "per_minute"),
        ("x-rate-limit-hourly", "per_hour"),
        ("x-rate-limit-24-hour", "per_day"),
        ("x-minute-requests-left", "minute_left"),
        ("x-minute-usage", "minute_used"),
        ("x-hourly-usage", "hour_used"),
        ("x-24-hour-usage", "day_used"),
    ):
        raw = headers.get(header)
        if raw is None:
            continue
        try:
            out[name] = int(str(raw).split(",")[0].strip())
        except (TypeError, ValueError):
            continue
    return out


def _key() -> str:
    """The Apollo key from the environment, then the OS keyring. Never returned."""
    if value := os.environ.get("APOLLO_API_KEY", "").strip():
        return value
    try:
        import keyring

        from ..config import KEYRING_SERVICE

        stored = keyring.get_password(KEYRING_SERVICE, "apollo_api_key")
        if stored:
            return stored.strip()
    except Exception:
        pass
    return ""


def configured() -> bool:
    return bool(_key())


def http_client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=True,
        headers={"x-api-key": _key(), "content-type": "application/json"},
    )


# ─── What comes back ────────────────────────────────────────────────────────


@dataclass
class ApolloCompany:
    """An organisation as Apollo describes it.

    Every field is optional because Apollo's coverage is uneven, and an absent field is
    left absent rather than defaulted — the fit scorer excludes what it does not know
    instead of scoring it zero, and a fabricated default would defeat that.
    """

    apollo_id: str = ""
    name: str = ""
    domain: str = ""
    website: str = ""
    description: str = ""
    industry: str = ""
    industries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    employees: int | None = None
    founded_year: int | None = None
    city: str = ""
    country: str = ""
    linkedin_url: str = ""
    # Apollo's headcount split by function. `data_science: 519` says more about whether a
    # company is worth applying to as a data candidate than any description does.
    headcount_by_department: dict[str, int] = field(default_factory=dict)
    headcount_growth_12m: float | None = None
    latest_funding_stage: str = ""
    total_funding: float | None = None
    error: str = ""
    # Set when Apollo says the billing cycle's credits are gone. Distinct from a plain
    # error because it means "stop asking", not "try the next company".
    exhausted: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.name)

    @property
    def size_band(self) -> str:
        """A readable size, from the headcount Apollo reports."""
        n = self.employees
        if n is None:
            return ""
        if n < 50:
            return "startup"
        if n < 250:
            return "small"
        if n < 1000:
            return "mid-size"
        if n < 10_000:
            return "large"
        return "enterprise"

    def relevant_headcount(self, functions: tuple[str, ...]) -> int:
        """How many people work in the functions the candidate is aiming at."""
        return sum(self.headcount_by_department.get(name, 0) or 0 for name in functions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "apollo_id": self.apollo_id,
            "name": self.name,
            "domain": self.domain,
            "website": self.website,
            "description": self.description,
            "industry": self.industry,
            "industries": self.industries,
            "keywords": self.keywords[:30],
            "technologies": self.technologies[:40],
            "employees": self.employees,
            "size_band": self.size_band,
            "founded_year": self.founded_year,
            "city": self.city,
            "country": self.country,
            "linkedin_url": self.linkedin_url,
            "headcount_by_department": self.headcount_by_department,
            "headcount_growth_12m": self.headcount_growth_12m,
            "latest_funding_stage": self.latest_funding_stage,
            "total_funding": self.total_funding,
            "error": self.error or None,
        }


@dataclass
class ApolloPosting:
    """One opening, as Apollo saw it on the company's own careers system."""

    apollo_id: str = ""
    title: str = ""
    url: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    posted_at: date | None = None
    last_seen_at: date | None = None

    @property
    def location(self) -> str:
        parts = [p for p in (self.city, self.state, self.country) if p]
        return ", ".join(parts)


@dataclass
class PostingResult:
    domain: str = ""
    postings: list[ApolloPosting] = field(default_factory=list)
    calls: int = 0
    error: str = ""
    plan_gated: bool = False
    exhausted: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


# ─── Reading them ───────────────────────────────────────────────────────────


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_company(body: dict[str, Any]) -> ApolloCompany:
    org = body.get("organization") or {}
    if not org:
        return ApolloCompany(error="no organization in response")

    return ApolloCompany(
        apollo_id=str(org.get("id") or ""),
        name=(org.get("name") or "").strip(),
        domain=(org.get("primary_domain") or "").strip(),
        website=(org.get("website_url") or "").strip(),
        description=(org.get("short_description") or "").strip()[:2000],
        industry=(org.get("industry") or "").strip(),
        industries=[str(i) for i in (org.get("industries") or [])][:8],
        keywords=[str(k) for k in (org.get("keywords") or [])][:60],
        # `technology_names` is the readable list; `current_technologies` is the same
        # thing as objects. Either may be present.
        technologies=[str(t) for t in (org.get("technology_names") or [])][:120]
        or [str(t.get("name")) for t in (org.get("current_technologies") or []) if t.get("name")][
            :120
        ],
        employees=org.get("estimated_num_employees"),
        founded_year=org.get("founded_year"),
        city=(org.get("city") or "").strip(),
        country=(org.get("country") or "").strip(),
        linkedin_url=(org.get("linkedin_url") or "").strip(),
        headcount_by_department={
            str(k): int(v or 0) for k, v in (org.get("departmental_head_count") or {}).items()
        },
        headcount_growth_12m=org.get("organization_headcount_twelve_month_growth"),
        latest_funding_stage=(org.get("latest_funding_stage") or "").strip(),
        total_funding=org.get("total_funding"),
    )


def _plan_gated(body: Any) -> bool:
    return isinstance(body, dict) and body.get("error_code") == PLAN_GATED


async def enrich(
    domain: str, *, client: httpx.AsyncClient | None = None, session: Any = None
) -> ApolloCompany:
    """What Apollo knows about the company at this domain.

    Fails into an `ApolloCompany` carrying an error rather than raising: an unknown
    company must leave the fit score unknown, not break the page asking for it.
    """
    if not domain:
        return ApolloCompany(error="no domain given")
    if not configured():
        return ApolloCompany(error="no Apollo key configured")

    from .. import integrations

    owns = client is None
    client = client or http_client()
    try:
        response = await client.post(f"{BASE}/organizations/enrich", json={"domain": domain})
        if session is not None:
            await integrations.record_call(
                session, "apollo", calls=1, errors=0 if response.status_code < 400 else 1
            )
    except httpx.HTTPError as exc:
        return ApolloCompany(error=f"{type(exc).__name__}")
    finally:
        if owns:
            await client.aclose()

    try:
        body = response.json()
    except ValueError:
        return ApolloCompany(error="unparseable JSON")

    if response.status_code >= 400:
        if _plan_gated(body):
            return ApolloCompany(error="not included in your Apollo plan")
        if _exhausted(body):
            return ApolloCompany(
                error="Apollo credits exhausted for this billing cycle", exhausted=True
            )
        return ApolloCompany(error=f"HTTP {response.status_code}")

    company = _parse_company(body)
    if company.ok:
        log.debug("apollo_enriched", domain=domain, employees=company.employees)
    return company


async def postings(
    domain: str,
    *,
    client: httpx.AsyncClient | None = None,
    session: Any = None,
    apollo_id: str = "",
) -> PostingResult:
    """Every opening Apollo holds for this company.

    Costs two calls when `apollo_id` is not already known — one to resolve the domain to
    an Apollo id and one to read the postings — so callers that have just enriched a
    company should pass the id they already have.
    """
    result = PostingResult(domain=domain)
    if not configured():
        result.error = "no Apollo key configured"
        return result

    from .. import integrations

    owns = client is None
    client = client or http_client()
    try:
        if not apollo_id:
            company = await enrich(domain, client=client, session=session)
            result.calls += 1
            if not company.ok:
                result.error = company.error or "company not found"
                result.exhausted = company.exhausted
                return result
            apollo_id = company.apollo_id

        try:
            response = await client.get(f"{BASE}/organizations/{apollo_id}/job_postings")
            result.calls += 1
            if session is not None:
                await integrations.record_call(
                    session, "apollo", calls=1, errors=0 if response.status_code < 400 else 1
                )
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}"
            return result

        try:
            body = response.json()
        except ValueError:
            result.error = "unparseable JSON"
            return result

        if response.status_code >= 400:
            result.plan_gated = _plan_gated(body)
            result.exhausted = _exhausted(body)
            result.error = (
                "not included in your Apollo plan"
                if result.plan_gated
                else "Apollo credits exhausted for this billing cycle"
                if result.exhausted
                else f"HTTP {response.status_code}"
            )
            return result

        for row in body.get("organization_job_postings") or []:
            title = (row.get("title") or "").strip()
            url = (row.get("url") or "").strip()
            if not title or not url:
                # A posting with no link cannot be applied to, and one with no title
                # cannot be scored. Either way it is not an opening we can act on.
                continue
            result.postings.append(
                ApolloPosting(
                    apollo_id=str(row.get("id") or ""),
                    title=title,
                    url=url,
                    city=(row.get("city") or "").strip(),
                    state=(row.get("state") or "").strip(),
                    country=(row.get("country") or "").strip(),
                    posted_at=_as_date(row.get("posted_at")),
                    last_seen_at=_as_date(row.get("last_seen_at")),
                )
            )
    finally:
        if owns:
            await client.aclose()

    log.info("apollo_postings", domain=domain, found=len(result.postings), calls=result.calls)
    return result


# ─── Postings as a discovery source ─────────────────────────────────────────


def to_raw_jobs(result: PostingResult, company_name: str) -> list[Any]:
    """Apollo postings in the shape the normalizer already understands.

    Worth having as its own source rather than another board reader: Apollo is keyed on a
    *domain*, so it answers for companies whose ATS slug was never guessed and whose
    careers page is a JavaScript shell — the two cases the board layer cannot reach. It
    also carries `posted_at`, which most aggregators omit and the freshness rule needs.

    What it does not carry is a description, and that is the honest limitation: skill
    matching reads the description, so a job discovered this way scores on its title and
    location alone until something fetches the posting itself. Left empty rather than
    filled with the title twice, so the scorer sees an absence and not a fabrication.
    """
    from ..models.job import RawJob

    out: list[Any] = []
    for posting in result.postings:
        out.append(
            RawJob(
                source="apollo",
                source_job_id=posting.apollo_id or posting.url,
                title=posting.title,
                url=posting.url,
                company_name=company_name,
                location=posting.location,
                published_at=posting.posted_at.isoformat() if posting.posted_at else None,
                updated_at=posting.last_seen_at.isoformat() if posting.last_seen_at else None,
                description="",
                raw={"apollo_id": posting.apollo_id},
            )
        )
    return out


@dataclass
class HarvestResult:
    """What one pass over the companies we hold actually found."""

    companies_checked: int = 0
    postings_seen: int = 0
    new_jobs: int = 0
    calls: int = 0
    plan_gated: bool = False
    exhausted: bool = False
    errors: list[str] = field(default_factory=list)

    def note(self) -> str:
        if self.plan_gated:
            return "Apollo's job postings are not included in your plan."
        if self.exhausted:
            return (
                f"Apollo's credits for this billing cycle ran out after "
                f"{self.companies_checked} companies. {self.new_jobs} new job(s) were "
                "kept; the rest will be there when the cycle resets."
            )
        if not self.companies_checked:
            return "No company with a domain to ask about."
        if not self.postings_seen:
            return f"Asked Apollo about {self.companies_checked} companies; it held no postings."
        return (
            f"{self.postings_seen} posting(s) across {self.companies_checked} companies, "
            f"{self.new_jobs} of them new. Apollo is keyed on a domain, so this reaches "
            "companies whose careers page cannot be read."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "companies_checked": self.companies_checked,
            "postings_seen": self.postings_seen,
            "new_jobs": self.new_jobs,
            "calls": self.calls,
            "plan_gated": self.plan_gated,
            "exhausted": self.exhausted,
            "errors": self.errors[:6],
            "note": self.note(),
        }


async def harvest_postings(
    session: Any,
    candidate: Any,
    candidate_id: str,
    *,
    limit: int = 40,
    company_ids: list[str] | None = None,
) -> HarvestResult:
    """Ask Apollo for openings at the companies we already hold, and keep the new ones.

    Scored on the way in with the candidate's stored preferences, the same as a monitored
    board — a job that arrives unscored is invisible to every page that ranks.

    Stops early if the plan turns out not to include postings: one 403 is enough to know,
    and forty of them is forty wasted calls.
    """
    from sqlalchemy import select

    from .. import preferences
    from ..pipeline.normalize import normalize_all
    from ..pipeline.opportunity import score_opportunity
    from ..pipeline.score import ScoreContext, score_job
    from ..storage import repository as repo
    from ..storage.models import Company, Job

    result = HarvestResult()
    if not configured():
        result.errors.append("no Apollo key configured")
        return result

    stmt = select(Company).where(Company.domain.isnot(None))
    if company_ids:
        stmt = stmt.where(Company.id.in_(company_ids))
    companies = list((await session.execute(stmt.limit(limit))).scalars().all())
    if not companies:
        return result

    context = ScoreContext(
        candidate=candidate, config=await preferences.load(session, candidate_id)
    )

    async with http_client() as client:
        for company in companies:
            found = await postings(company.domain or "", client=client, session=session)
            result.companies_checked += 1
            result.calls += found.calls

            if found.plan_gated:
                result.plan_gated = True
                break
            if found.exhausted:
                # One of these is enough to know. Asking sixty more companies would be
                # sixty more calls proving the same thing.
                result.exhausted = True
                break
            if not found.ok:
                result.errors.append(f"{company.name}: {found.error}")
                continue

            result.postings_seen += len(found.postings)
            normalized = normalize_all(to_raw_jobs(found, company.name))
            if not normalized:
                continue

            live_ids = {job.job_id for job in normalized if job.job_id}
            seen_before = {
                row[0]
                for row in (
                    await session.execute(select(Job.id).where(Job.id.in_(live_ids or {""})))
                ).all()
            }
            fresh = [job for job in normalized if job.job_id not in seen_before]
            if not fresh:
                continue

            await repo.upsert_jobs(session, fresh)
            for job in fresh:
                deterministic = score_job(job, context)
                await repo.save_score(session, candidate_id, score_opportunity(job, deterministic))
            result.new_jobs += len(fresh)

    await session.flush()
    log.info(
        "apollo_harvest",
        companies=result.companies_checked,
        postings=result.postings_seen,
        new=result.new_jobs,
        calls=result.calls,
    )
    return result
