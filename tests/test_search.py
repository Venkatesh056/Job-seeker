"""Finding employers by searching the web.

Most of what matters here is what the layer *refuses*. A web search for "companies in
Bangalore hiring data analysts" returns job boards, listicles and content farms far above
any company's own site, and every one of the hosts named in these tests was returned by a
real query during development. Letting them through is how a company list fills up with
LinkedIn, bebee and "Top 10 Data Analytics Companies in Bangalore".

The other thing that matters is the money. Tavily's free tier is 1,000 credits a month and
SerpAPI's is 250 searches, so the cheap one is asked first and an exhausted provider is not
asked again for the rest of a run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from careeragent.sources import search as websearch
from careeragent.sources.search.base import SearchHit, SearchResult

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ─── What is not an employer ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com/jobs/view/123",
        "https://www.naukri.com/data-analyst-jobs-in-bangalore",
        "https://in.indeed.com/q-data-analyst-l-bangalore-jobs.html",
        "https://boards.greenhouse.io/acme",
        "https://jobs.lever.co/acme",
        "https://acme.wd1.myworkdayjobs.com/careers",
        "https://www.glassdoor.co.in/Jobs/index.htm",
        "https://en.wikipedia.org/wiki/Infosys",
        "https://www.reddit.com/r/developersIndia/comments/abc",
        "https://medium.com/@someone/top-companies",
        "https://www.bebee.com/jobs",
        "https://www.f6s.com/companies/bangalore",
        "https://themanifest.com/in/data-analytics/bangalore",
        "https://clutch.co/in/it-services",
    ],
)
def test_a_board_directory_or_content_farm_is_not_an_employer(url):
    assert not websearch.is_employer_site(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.zoho.com/careers/",
        "https://amazon.jobs/en/search",
        "https://careers.microsoft.com/",
        "https://www.mcafee.com/en-us/about/careers.html",
        "https://amgen.com/careers",
    ],
)
def test_a_company_site_survives(url):
    assert websearch.is_employer_site(url)


@pytest.mark.parametrize(
    "host",
    ["startup.jobs", "freshers.jobs", "jobs.com", "careers.com", "hiring.com"],
)
def test_a_generic_word_in_front_of_a_jobs_domain_is_not_a_brand(host):
    """`amazon.jobs` is Amazon. `startup.jobs` is a job board. The TLD does not separate
    them — the word in front of it does."""
    assert not websearch.is_employer_site(f"https://{host}")


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("amazon.jobs", "amazon"),
        ("careers.amazon.com", "amazon"),
        ("jobs.zoho.com", "zoho"),
        ("zoho.com", "zoho"),
        ("startup.jobs", ""),
        ("careers.com", ""),
        ("", ""),
    ],
)
def test_the_brand_is_found_wherever_it_sits_in_the_hostname(host, expected):
    assert websearch.brand_label(host) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Top Data Analytics Companies in Bangalore",
        "Top Hiring Companies for Data Analysts in India",
        "Best Companies to Work For 2026",
        "List of IT companies in Hyderabad",
        "15 Top 10 Startups in Chennai",
    ],
)
def test_a_ranked_list_page_is_writing_about_companies_not_being_one(title):
    assert websearch.looks_like_a_listicle(title)


@pytest.mark.parametrize(
    "title",
    [
        "Careers at Zoho",
        "Data Engineer / Analyst in Bangalore, India",
        "Join our team",
        "Amgen Careers",
    ],
)
def test_an_ordinary_careers_page_title_is_not_a_listicle(title):
    assert not websearch.looks_like_a_listicle(title)


def test_a_listicle_is_dropped_even_when_its_host_looks_fine():
    """`niracore.com` is not on any block list and never will be — there is an endless
    supply of these. The title is what gives it away."""
    result = SearchResult(
        provider="tavily",
        hits=[
            SearchHit(
                title="Top Data Analytics Companies in Bangalore", url="https://niracore.com/blog"
            ),
            SearchHit(title="Careers at Amgen", url="https://amgen.com/careers"),
        ],
    )
    assert [hit.host for hit in result.employer_hits()] == ["amgen.com"]


def test_one_employer_is_not_counted_twice_because_it_ranked_twice():
    result = SearchResult(
        provider="tavily",
        hits=[
            SearchHit(title="Careers", url="https://zoho.com/careers"),
            SearchHit(title="Jobs", url="https://zoho.com/jobs"),
            SearchHit(title="Team", url="https://amgen.com/about"),
        ],
    )
    assert [hit.host for hit in result.employer_hits()] == ["zoho.com", "amgen.com"]


# ─── The money ──────────────────────────────────────────────────────────────


class FakeProvider:
    """A scripted search backend that records what it was asked."""

    def __init__(self, id_, *, hits=None, error="", exhausted=False, configured=True):
        self.id = id_
        self.label = id_
        self.signup = ""
        self.env_vars = ()
        self.calls: list[str] = []
        self._hits = hits or []
        self._error = error
        self._exhausted = exhausted
        self._configured = configured

    def configured(self):
        return self._configured

    async def search(self, query, *, client, limit=10, region=""):
        self.calls.append(query)
        return SearchResult(
            provider=self.id,
            query=query,
            hits=list(self._hits),
            calls=1,
            error=self._error,
            quota_exhausted=self._exhausted,
        )


def _hit(host="zoho.com"):
    return SearchHit(title="Careers", url=f"https://{host}/careers")


async def test_the_provider_with_more_free_budget_is_asked_first(monkeypatch):
    """Tavily gives 1,000 credits a month and SerpAPI 250, so Tavily leads. The usual
    "best provider first" instinct would empty the small budget in a single run."""
    tavily = FakeProvider("tavily", hits=[_hit()])
    serpapi = FakeProvider("serpapi", hits=[_hit("amgen.com")])
    monkeypatch.setattr(websearch, "PROVIDERS", [tavily, serpapi])

    result = await websearch.search("who is hiring", client=_NullClient())

    assert result.provider == "tavily"
    assert serpapi.calls == [], "the expensive provider must not be touched"


async def test_the_fallback_answers_when_the_first_one_cannot(monkeypatch):
    tavily = FakeProvider("tavily", error="HTTP 500")
    serpapi = FakeProvider("serpapi", hits=[_hit("amgen.com")])
    monkeypatch.setattr(websearch, "PROVIDERS", [tavily, serpapi])

    result = await websearch.search("who is hiring", client=_NullClient())

    assert result.provider == "serpapi"
    assert result.hits


async def test_an_exhausted_provider_is_not_asked_again_this_run(monkeypatch):
    """Otherwise a run of twenty queries spends twenty failed calls proving the same
    thing it already knew after the first."""
    tavily = FakeProvider("tavily", error="plan limit reached", exhausted=True)
    serpapi = FakeProvider("serpapi", hits=[_hit("amgen.com")])
    monkeypatch.setattr(websearch, "PROVIDERS", [tavily, serpapi])

    skip: set[str] = set()
    for _ in range(3):
        await websearch.search("who is hiring", client=_NullClient(), skip=skip)

    assert len(tavily.calls) == 1, "asked once, then skipped"
    assert len(serpapi.calls) == 3


async def test_an_unconfigured_provider_is_skipped_silently(monkeypatch):
    tavily = FakeProvider("tavily", configured=False)
    serpapi = FakeProvider("serpapi", hits=[_hit()])
    monkeypatch.setattr(websearch, "PROVIDERS", [tavily, serpapi])

    result = await websearch.search("x", client=_NullClient())

    assert tavily.calls == []
    assert result.provider == "serpapi"


async def test_no_provider_at_all_says_so_rather_than_returning_nothing(monkeypatch):
    """An empty result and "no key configured" look identical to a caller, and only one
    of them means "there are no companies here"."""
    monkeypatch.setattr(websearch, "PROVIDERS", [FakeProvider("tavily", configured=False)])

    result = await websearch.search("x", client=_NullClient())

    assert not result.ok
    assert "no web-search provider is configured" in result.error


async def test_every_call_is_recorded_against_the_provider(isolated, monkeypatch):
    """The Keys page reports these as a floor, so a call that is not recorded is a call
    the user is never told about."""
    from sqlalchemy import select

    from careeragent.storage import init_db, session_scope
    from careeragent.storage.models import ProviderCall

    tavily = FakeProvider("tavily", hits=[_hit()])
    monkeypatch.setattr(websearch, "PROVIDERS", [tavily])

    await init_db(drop=True)
    async with session_scope() as session:
        for _ in range(3):
            await websearch.search("x", client=_NullClient(), session=session)

    async with session_scope() as session:
        rows = (
            (await session.execute(select(ProviderCall).where(ProviderCall.provider == "tavily")))
            .scalars()
            .all()
        )
    assert rows[0].calls == 3


# ─── Turning results into leads ─────────────────────────────────────────────


async def test_a_host_several_queries_agree_on_ranks_above_a_one_off(monkeypatch):
    """A single lucky phrase is weaker evidence than four queries converging."""
    everywhere = _hit("amgen.com")
    once = _hit("zoho.com")

    class Converging(FakeProvider):
        async def search(self, query, *, client, limit=10, region=""):
            self.calls.append(query)
            hits = [everywhere] + ([once] if len(self.calls) == 1 else [])
            return SearchResult(provider=self.id, query=query, hits=hits, calls=1)

    monkeypatch.setattr(websearch, "PROVIDERS", [Converging("tavily")])

    leads, _ = await websearch.find_companies("data analyst", "Bangalore", client=_NullClient())

    assert leads[0].host == "amgen.com"
    assert len(leads[0].queries) > 1


async def test_a_careers_subdomain_is_the_same_employer_as_its_parent(monkeypatch):
    provider = FakeProvider(
        "tavily",
        hits=[
            SearchHit(title="Careers", url="https://careers.amgen.com/openings"),
            SearchHit(title="About", url="https://amgen.com/about"),
        ],
    )
    monkeypatch.setattr(websearch, "PROVIDERS", [provider])

    leads, _ = await websearch.find_companies("x", "Bangalore", client=_NullClient())

    assert [lead.host for lead in leads] == ["amgen.com"]
    assert leads[0].name == "Amgen"


async def test_a_staffing_agency_is_not_offered_as_an_employer(monkeypatch):
    """Its site has no careers board to resolve, and it crowds out real employers."""
    provider = FakeProvider(
        "tavily",
        hits=[
            SearchHit(title="Randstad Staffing Solutions", url="https://randstadstaffing.com"),
            SearchHit(title="Careers", url="https://amgen.com/careers"),
        ],
    )
    monkeypatch.setattr(websearch, "PROVIDERS", [provider])

    leads, _ = await websearch.find_companies("x", "Bangalore", client=_NullClient())

    assert "amgen.com" in [lead.host for lead in leads]
    assert not any("randstad" in lead.host for lead in leads)


def test_a_query_set_asks_about_companies_not_postings():
    """ "data analyst jobs in Bangalore" returns Naukri. The phrasing is the filter."""
    queries = websearch.queries_for("data analyst", "Bangalore")

    assert queries
    assert all("Bangalore" in q and "data analyst" in q for q in queries)
    assert any("companies" in q for q in queries)


def test_a_query_set_needs_both_a_role_and_a_place():
    assert websearch.queries_for("", "Bangalore") == []
    assert websearch.queries_for("data analyst", "") == []


# ─── Helpers ────────────────────────────────────────────────────────────────


class _NullClient:
    """Stands in for httpx.AsyncClient. The fake providers never touch it."""

    async def aclose(self):
        return None

    async def get(self, *a, **k):
        raise AssertionError("no provider should make a real request in these tests")

    async def post(self, *a, **k):
        raise AssertionError("no provider should make a real request in these tests")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("CAREERAGENT_CACHE_DIR", str(tmp_path / "cache"))

    from careeragent import config as config_module
    from careeragent import storage

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    yield tmp_path
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]


# Keep the unused import honest: SimpleNamespace documents the shape a real fetcher returns.
_ = SimpleNamespace
