"""Directory source adapters.

Five aggregators, three of which need a free key. Each one does the same job — turn
(query, location) into a list of employers who are hiring — and each is written to fail
into an empty result rather than an exception, because a campaign that dies because one
free tier ran out is worse than a campaign that returns fewer companies.

Quota discipline is the recurring theme. Adzuna's free tier is hundreds of calls a day
and JSearch's is 200 a *month*, so:

  * every source reports `calls` and the campaign budgets against it,
  * a 429 sets `quota_exhausted`, which stops the campaign hitting that source again
    for the rest of the run rather than burning the remainder on failures,
  * results-per-page is maxed, because one call returning 50 companies costs the same
    as one returning 10.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, ClassVar

import httpx

from ...config import get_settings
from ...logging import get_logger
from .base import CompanyHit, SourceResult

log = get_logger("sources.directory.providers")


def _env(*names: str) -> str | None:
    """A key from the environment, then the OS keyring.

    Same resolution order as the LLM keys: an explicit env var wins, the keyring is the
    default store, and neither is ever returned by the API.
    """
    for name in names:
        if value := os.environ.get(name):
            return value.strip()
    try:
        import keyring

        from ...config import KEYRING_SERVICE

        for name in names:
            if value := keyring.get_password(KEYRING_SERVICE, name.lower()):
                return value.strip()
    except Exception:
        pass
    return None


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2].rstrip("Z"), fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _domain_from(url: str | None) -> str | None:
    if not url:
        return None
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    # An aggregator's own domain is not the employer's.
    if not host or any(
        marker in host
        for marker in (
            "adzuna",
            "jooble",
            "rapidapi",
            "themuse",
            "indeed",
            "linkedin",
            "glassdoor",
            "naukri",
            "monster",
            "shine.com",
            "timesjobs",
        )
    ):
        return None
    return host


# ─── Where a location is ────────────────────────────────────────────────────

# Which country each city belongs to. Adzuna needs the code for its URL path; Jooble
# needs the printed name, because it will not resolve a bare city (see `qualify`).
CITY_COUNTRY: dict[str, tuple[str, ...]] = {
    "in": (
        "india",
        "bangalore",
        "bengaluru",
        "chennai",
        "mumbai",
        "delhi",
        "new delhi",
        "pune",
        "hyderabad",
        "kolkata",
        "coimbatore",
        "noida",
        "gurgaon",
        "gurugram",
        "ahmedabad",
        "kochi",
        "jaipur",
        "indore",
        "trivandrum",
    ),
    "gb": ("united kingdom", "uk", "london", "manchester", "birmingham", "edinburgh"),
    "us": (
        "united states",
        "usa",
        "new york",
        "san francisco",
        "seattle",
        "austin",
        "boston",
        "chicago",
        "los angeles",
    ),
    "ae": ("uae", "dubai", "abu dhabi"),
    "sg": ("singapore",),
    "au": ("australia", "sydney", "melbourne", "brisbane"),
    "ca": ("canada", "toronto", "vancouver", "montreal"),
    "de": ("germany", "berlin", "munich", "hamburg"),
}

COUNTRY_NAMES: dict[str, str] = {
    "in": "India",
    "gb": "United Kingdom",
    "us": "United States",
    "ae": "United Arab Emirates",
    "sg": "Singapore",
    "au": "Australia",
    "ca": "Canada",
    "de": "Germany",
}

# The spellings that already mean the country rather than a city inside it. Kept apart
# from CITY_COUNTRY because "already names its country" must not be satisfied by naming
# a city — "Bangalore" is in the India list and is exactly what needs the suffix.
COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "in": ("india",),
    "gb": ("united kingdom", "uk", "great britain", "britain", "england"),
    "us": ("united states", "usa", "us", "u.s.", "america"),
    "ae": ("united arab emirates", "uae"),
    "sg": ("singapore",),
    "au": ("australia",),
    "ca": ("canada",),
    "de": ("germany", "deutschland"),
}

# A location that already names a country, or has no country to name.
_UNQUALIFIABLE = ("remote", "anywhere", "worldwide", "work from home", "wfh")


def country_code_for(location: str) -> str:
    """Which market a location belongs to. Defaults to India.

    The default is deliberate rather than neutral: this is built for a candidate
    searching Indian cities, and silently searching the UK for "Bangalore" would
    return nothing with no explanation.
    """
    lowered = (location or "").lower()
    for code, markers in CITY_COUNTRY.items():
        if any(marker in lowered for marker in markers):
            return code
    return "in"


def qualify(location: str) -> str:
    """Append the country to a bare city, for sources that will not resolve one.

    Jooble returns **zero** results for "Bangalore", "Chennai" or "Hyderabad" and 83 for
    any of them suffixed with ", India". It resolves the country and ignores the city —
    the three qualified cities return the identical 83 rows that plain "India" does — so
    this buys country-level coverage where there was none, not city-level precision.
    Callers must not read a qualified location as a city filter.
    """
    clean = (location or "").strip().strip(",")
    if not clean:
        return clean
    lowered = clean.lower()
    if any(word in lowered for word in _UNQUALIFIABLE):
        return clean

    code = country_code_for(clean)
    name = COUNTRY_NAMES.get(code, "")
    if not name:
        return clean

    # Already names its country somewhere in the string, so adding it again would give
    # "India, India" for a plain country and "Pune, India, India" on a second pass.
    parts = {part.strip().lower() for part in lowered.split(",")}
    aliases = COUNTRY_ALIASES.get(code, ())
    if any(alias in parts for alias in aliases) or name.lower() in lowered:
        return clean
    return f"{clean}, {name}"


# ─── Adzuna ─────────────────────────────────────────────────────────────────


class Adzuna:
    """https://developer.adzuna.com — instant free app_id/app_key, ~20 countries.

    The best single source for this job: covers India properly, returns a structured
    `company.display_name` on every row, and supports `max_days_old` so a campaign can
    ask only for fresh postings instead of filtering them afterwards.
    """

    id = "adzuna"
    label = "Adzuna"
    needs_key = True
    signup = "https://developer.adzuna.com/signup"

    # Adzuna is per-country, and the country is part of the path rather than a filter.
    # The map lives at module level because Jooble needs the same knowledge.
    COUNTRIES: ClassVar[dict[str, tuple[str, ...]]] = CITY_COUNTRY

    def configured(self) -> bool:
        return bool(_env("CAREERAGENT_ADZUNA_APP_ID", "ADZUNA_APP_ID")) and bool(
            _env("CAREERAGENT_ADZUNA_APP_KEY", "ADZUNA_APP_KEY")
        )

    @classmethod
    def country_for(cls, location: str) -> str:
        """Which Adzuna market a location belongs to. Defaults to India."""
        return country_code_for(location)

    async def search(
        self,
        query: str,
        location: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 50,
        max_age_days: int = 30,
    ) -> SourceResult:
        result = SourceResult(source=self.id)
        app_id = _env("CAREERAGENT_ADZUNA_APP_ID", "ADZUNA_APP_ID")
        app_key = _env("CAREERAGENT_ADZUNA_APP_KEY", "ADZUNA_APP_KEY")
        if not (app_id and app_key):
            result.error = "no Adzuna key configured"
            return result

        country = self.country_for(location)
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": query,
            "where": location,
            "results_per_page": str(min(limit, 50)),
            "max_days_old": str(max_age_days),
            "content-type": "application/json",
        }
        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"

        try:
            response = await client.get(url, params=params)
            result.calls = 1
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}"
            return result

        if response.status_code == 429:
            result.error = "Adzuna daily quota exhausted"
            result.quota_exhausted = True
            return result
        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}"
            return result

        try:
            payload = response.json()
        except ValueError:
            result.error = "unparseable JSON"
            return result

        for row in payload.get("results") or []:
            company = ((row.get("company") or {}).get("display_name") or "").strip()
            if not company:
                continue
            result.hits.append(
                CompanyHit(
                    name=company,
                    source=self.id,
                    location=((row.get("location") or {}).get("display_name") or ""),
                    job_title=(row.get("title") or "").strip(),
                    job_url=row.get("redirect_url") or "",
                    category=((row.get("category") or {}).get("label") or ""),
                    posted_date=_as_date(row.get("created")),
                    salary_min=row.get("salary_min"),
                    salary_max=row.get("salary_max"),
                    description=(row.get("description") or "")[:600],
                    raw={"id": row.get("id")},
                )
            )
        return result


# ─── JSearch (RapidAPI) ─────────────────────────────────────────────────────


class JSearch:
    """Google Jobs index via RapidAPI. Free tier 200 requests/month.

    Reaches LinkedIn, Indeed, Naukri and Glassdoor postings *through Google's index*,
    which is the difference between using those platforms' published data and scraping
    them. The project refuses to scrape them directly; reading an aggregator that
    licenses the data is a different act.

    Its quota is the tightest of any source here — 200 a month is roughly six searches
    a day — so the campaign treats it as a precision source and calls it last.
    """

    id = "jsearch"
    label = "JSearch (Google Jobs)"
    needs_key = True
    signup = "https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch"

    def configured(self) -> bool:
        return bool(_env("CAREERAGENT_RAPIDAPI_KEY", "RAPIDAPI_KEY"))

    async def search(
        self,
        query: str,
        location: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 50,
        max_age_days: int = 30,
    ) -> SourceResult:
        result = SourceResult(source=self.id)
        key = _env("CAREERAGENT_RAPIDAPI_KEY", "RAPIDAPI_KEY")
        if not key:
            result.error = "no RapidAPI key configured"
            return result

        date_posted = "month"
        if max_age_days <= 1:
            date_posted = "today"
        elif max_age_days <= 3:
            date_posted = "3days"
        elif max_age_days <= 7:
            date_posted = "week"

        try:
            response = await client.get(
                "https://jsearch.p.rapidapi.com/search",
                params={
                    "query": f"{query} in {location}",
                    "page": "1",
                    "num_pages": "1",
                    "date_posted": date_posted,
                },
                headers={
                    "x-rapidapi-key": key,
                    "x-rapidapi-host": "jsearch.p.rapidapi.com",
                },
            )
            result.calls = 1
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}"
            return result

        if response.status_code in (429, 403):
            result.error = "JSearch monthly quota exhausted"
            result.quota_exhausted = True
            return result
        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}"
            return result

        try:
            payload = response.json()
        except ValueError:
            result.error = "unparseable JSON"
            return result

        for row in (payload.get("data") or [])[:limit]:
            company = (row.get("employer_name") or "").strip()
            if not company:
                continue
            city = row.get("job_city") or ""
            region = row.get("job_state") or row.get("job_country") or ""
            result.hits.append(
                CompanyHit(
                    name=company,
                    source=self.id,
                    # JSearch returns the employer's own website, which is the single
                    # most valuable field any source here gives us: it feeds straight
                    # into ATS resolution.
                    domain=_domain_from(row.get("employer_website")),
                    location=", ".join(p for p in (city, region) if p),
                    job_title=(row.get("job_title") or "").strip(),
                    job_url=row.get("job_apply_link") or row.get("job_google_link") or "",
                    category=row.get("job_employment_type") or "",
                    posted_date=_as_date(row.get("job_posted_at_datetime_utc")),
                    description=(row.get("job_description") or "")[:600],
                    raw={"publisher": row.get("job_publisher")},
                )
            )
        return result


# ─── Jooble ─────────────────────────────────────────────────────────────────


class Jooble:
    """https://jooble.org/api/about — free key on request, POST-based.

    Weaker structured data than Adzuna (no category, no reliable domain) but no
    published monthly cap, which makes it the right source to lean on for breadth once
    the metered ones are spent.
    """

    id = "jooble"
    label = "Jooble"
    needs_key = True
    signup = "https://jooble.org/api/about"

    def configured(self) -> bool:
        return bool(_env("CAREERAGENT_JOOBLE_KEY", "JOOBLE_KEY"))

    async def search(
        self,
        query: str,
        location: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 50,
        max_age_days: int = 30,
    ) -> SourceResult:
        result = SourceResult(source=self.id)
        key = _env("CAREERAGENT_JOOBLE_KEY", "JOOBLE_KEY")
        if not key:
            result.error = "no Jooble key configured"
            return result

        try:
            response = await client.post(
                f"https://jooble.org/api/{key}",
                json={"keywords": query, "location": qualify(location), "page": "1"},
                headers={"content-type": "application/json"},
            )
            result.calls = 1
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}"
            return result

        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}"
            result.quota_exhausted = response.status_code == 429
            return result

        try:
            payload = response.json()
        except ValueError:
            result.error = "unparseable JSON"
            return result

        for row in (payload.get("jobs") or [])[:limit]:
            company = (row.get("company") or "").strip()
            if not company:
                continue
            result.hits.append(
                CompanyHit(
                    name=company,
                    source=self.id,
                    location=(row.get("location") or "").strip(),
                    job_title=(row.get("title") or "").strip(),
                    job_url=row.get("link") or "",
                    category=row.get("type") or "",
                    posted_date=_as_date(row.get("updated")),
                    description=(row.get("snippet") or "")[:600],
                )
            )
        return result


# ─── Keyless sources ────────────────────────────────────────────────────────


class TheMuse:
    """https://www.themuse.com/developers/api/v2 — keyless, generous.

    Curated rather than comprehensive: a few thousand employers, mostly product
    companies, skewed to the US. Worth including because it needs no signup at all, so
    a fresh install returns *something* before the user has any keys.
    """

    id = "themuse"
    label = "The Muse"
    needs_key = False
    signup = ""

    def configured(self) -> bool:
        return True

    async def search(
        self,
        query: str,
        location: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 50,
        max_age_days: int = 30,
    ) -> SourceResult:
        result = SourceResult(source=self.id)
        try:
            response = await client.get(
                "https://www.themuse.com/api/public/jobs",
                params={"page": "0", "location": location, "descending": "true"},
            )
            result.calls = 1
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}"
            return result

        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}"
            result.quota_exhausted = response.status_code == 429
            return result

        try:
            payload = response.json()
        except ValueError:
            result.error = "unparseable JSON"
            return result

        needle = query.lower().split()
        for row in (payload.get("results") or [])[: limit * 2]:
            company = ((row.get("company") or {}).get("name") or "").strip()
            title = (row.get("name") or "").strip()
            if not company:
                continue
            # The Muse has no keyword filter on the public endpoint, so the query is
            # applied here rather than skipped — otherwise every family would return
            # the same undifferentiated page.
            if needle and not any(word in title.lower() for word in needle):
                continue
            locations = [
                loc.get("name", "") for loc in (row.get("locations") or []) if isinstance(loc, dict)
            ]
            result.hits.append(
                CompanyHit(
                    name=company,
                    source=self.id,
                    location=locations[0] if locations else location,
                    job_title=title,
                    job_url=((row.get("refs") or {}).get("landing_page") or ""),
                    category=", ".join(
                        c.get("name", "")
                        for c in (row.get("categories") or [])
                        if isinstance(c, dict)
                    ),
                    posted_date=_as_date(row.get("publication_date")),
                )
            )
            if len(result.hits) >= limit:
                break
        return result


class Arbeitnow:
    """https://www.arbeitnow.com/api/job-board-api — keyless, remote-heavy, EU-skewed.

    Included for the remote case: a candidate searching "Remote" gets almost nothing
    from a city-indexed aggregator, and this board is entirely remote and visa-sponsored
    roles.
    """

    id = "arbeitnow"
    label = "Arbeitnow"
    needs_key = False
    signup = ""

    def configured(self) -> bool:
        return True

    async def search(
        self,
        query: str,
        location: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 50,
        max_age_days: int = 30,
    ) -> SourceResult:
        result = SourceResult(source=self.id)
        try:
            response = await client.get("https://www.arbeitnow.com/api/job-board-api")
            result.calls = 1
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}"
            return result

        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}"
            # Keyless does not mean unlimited. Marking it exhausted stops the campaign
            # spending the rest of the run re-asking a source that is refusing.
            result.quota_exhausted = response.status_code == 429
            return result

        try:
            payload = response.json()
        except ValueError:
            result.error = "unparseable JSON"
            return result

        words = query.lower().split()
        remote_wanted = "remote" in (location or "").lower()
        for row in payload.get("data") or []:
            company = (row.get("company_name") or "").strip()
            title = (row.get("title") or "").strip()
            if not company:
                continue
            if words and not any(word in title.lower() for word in words):
                continue
            if remote_wanted and not row.get("remote"):
                continue
            result.hits.append(
                CompanyHit(
                    name=company,
                    source=self.id,
                    location=(row.get("location") or "")
                    + (" (remote)" if row.get("remote") else ""),
                    job_title=title,
                    job_url=row.get("url") or "",
                    category=", ".join(row.get("job_types") or []),
                    posted_date=_as_date(row.get("created_at")),
                    description=(row.get("description") or "")[:600],
                )
            )
            if len(result.hits) >= limit:
                break
        return result


class Remotive:
    """https://remotive.com/api/remote-jobs — keyless, fully remote roles."""

    id = "remotive"
    label = "Remotive"
    needs_key = False
    signup = ""

    def configured(self) -> bool:
        return True

    async def search(
        self,
        query: str,
        location: str,
        *,
        client: httpx.AsyncClient,
        limit: int = 50,
        max_age_days: int = 30,
    ) -> SourceResult:
        result = SourceResult(source=self.id)
        try:
            response = await client.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": query, "limit": str(min(limit, 50))},
            )
            result.calls = 1
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}"
            return result

        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}"
            result.quota_exhausted = response.status_code == 429
            return result

        try:
            payload = response.json()
        except ValueError:
            result.error = "unparseable JSON"
            return result

        for row in (payload.get("jobs") or [])[:limit]:
            company = (row.get("company_name") or "").strip()
            if not company:
                continue
            result.hits.append(
                CompanyHit(
                    name=company,
                    source=self.id,
                    location=(row.get("candidate_required_location") or "Remote"),
                    job_title=(row.get("title") or "").strip(),
                    job_url=row.get("url") or "",
                    category=row.get("category") or "",
                    posted_date=_as_date(row.get("publication_date")),
                    description=(row.get("description") or "")[:600],
                )
            )
        return result


# Ordered by cost, cheapest first. The campaign spends the unmetered sources before
# the metered ones, so a run that exhausts a quota has already banked free results.
SOURCES: tuple[Any, ...] = (
    TheMuse(),
    Arbeitnow(),
    Remotive(),
    Adzuna(),
    Jooble(),
    JSearch(),
)

SOURCES_BY_ID: dict[str, Any] = {s.id: s for s in SOURCES}


def available() -> list[Any]:
    return [s for s in SOURCES if s.configured()]


def describe() -> list[dict[str, Any]]:
    """What is wired, what is configured, and where to get the missing keys."""
    return [
        {
            "id": s.id,
            "label": s.label,
            "needs_key": s.needs_key,
            "configured": s.configured(),
            "signup": getattr(s, "signup", ""),
        }
        for s in SOURCES
    ]


def http_client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=True,
        headers={"user-agent": settings.user_agent, "accept": "application/json"},
    )
