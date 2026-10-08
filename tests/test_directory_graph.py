"""Company directory sources and the LangGraph campaigns.

No network and no browser. The directory sources are driven through a stub HTTP client
so the parsing, the quota handling and the recruiter filter are all testable without a
key, and the graphs run against fake sources so the fan-out and the interrupt are
testable without spending anyone's free tier.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from careeragent.sources import directory as dirsrc
from careeragent.sources.directory.base import (
    Company,
    CompanyHit,
    SourceResult,
    aggregate,
    looks_like_recruiter,
    normalize_company,
)
from careeragent.sources.directory.providers import (
    Adzuna,
    Jooble,
    JSearch,
    Remotive,
    qualify,
)

# ─── A stub HTTP client ─────────────────────────────────────────────────────


class _Response:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Client:
    """Records what was asked for and returns what it was told to."""

    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None):
        self.calls.append((url, params or {}))
        return _Response(self.payload, self.status)

    async def post(self, url: str, json: dict | None = None, headers: dict | None = None):
        self.calls.append((url, json or {}))
        return _Response(self.payload, self.status)


# ─── Name normalization ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Infosys Limited", "Infosys"),
        ("Wipro Technologies Pvt Ltd", "Wipro"),
        ("Zoho Corporation", "Zoho"),
        ("Acme Labs, Inc.", "Acme"),
        ("TCS India", "TCS"),
    ],
)
def test_company_names_normalize_together(a, b):
    """One employer, however the aggregator spelled their legal entity."""
    assert normalize_company(a) == normalize_company(b)


def test_different_companies_stay_different():
    assert normalize_company("Infosys") != normalize_company("Wipro")


@pytest.mark.parametrize(
    "name",
    [
        "ABC Staffing Solutions",
        "XYZ Manpower Consultancy",
        "Leading MNC",
        "Confidential",
        "A leading product company",
        "Talent Solutions India",
    ],
)
def test_recruiters_are_recognised(name):
    """Staffing rows cannot be enriched or applied to; they only crowd the list."""
    assert looks_like_recruiter(name)


@pytest.mark.parametrize("name", ["Infosys", "Zoho", "Swiggy", "Razorpay Software"])
def test_real_employers_are_not_flagged(name):
    assert not looks_like_recruiter(name)


# ─── Aggregation ────────────────────────────────────────────────────────────


def hit(name: str, source: str = "adzuna", **kwargs) -> CompanyHit:
    return CompanyHit(name=name, source=source, **kwargs)


def test_aggregate_merges_one_employer_across_sources():
    results = [
        ("ml", SourceResult("a", hits=[hit("Infosys Ltd", "adzuna", job_title="ML Engineer")])),
        ("ml", SourceResult("b", hits=[hit("Infosys", "jsearch", job_title="Data Scientist")])),
    ]
    companies = aggregate(results)
    assert len(companies) == 1
    assert companies[0].open_roles == 2
    assert set(companies[0].sources) == {"adzuna", "jsearch"}
    assert len(companies[0].sample_titles) == 2


def test_aggregate_ranks_multi_source_companies_first():
    """Three aggregators agreeing is a better lead than one posting seen once."""
    results = [
        ("ml", SourceResult("a", hits=[hit("Seen Once", "adzuna")])),
        ("ml", SourceResult("b", hits=[hit("Seen Twice", "adzuna"), hit("Seen Twice", "jooble")])),
    ]
    companies = aggregate(results)
    assert companies[0].name == "Seen Twice"


def test_aggregate_drops_recruiters_by_default():
    results = [("ml", SourceResult("a", hits=[hit("ABC Staffing Solutions"), hit("Zoho")]))]
    assert [c.name for c in aggregate(results)] == ["Zoho"]


def test_aggregate_can_keep_recruiters():
    results = [("ml", SourceResult("a", hits=[hit("ABC Staffing Solutions")]))]
    assert len(aggregate(results, drop_recruiters=False)) == 1


def test_aggregate_records_families_and_newest_post():
    results = [
        ("nlp", SourceResult("a", hits=[hit("Acme", posted_date=date(2026, 1, 1))])),
        ("mlops", SourceResult("a", hits=[hit("Acme", posted_date=date(2026, 6, 1))])),
    ]
    company = aggregate(results)[0]
    assert set(company.families) == {"nlp", "mlops"}
    assert company.newest_post == date(2026, 6, 1)


def test_company_is_not_applyable_without_an_ats():
    company = Company(name="Acme", key="acme")
    assert not company.applyable
    company.ats_provider, company.ats_slug = "greenhouse", "acme"
    assert company.applyable


# ─── Source parsing ─────────────────────────────────────────────────────────


async def test_adzuna_parses_a_response(monkeypatch):
    monkeypatch.setenv("CAREERAGENT_ADZUNA_APP_ID", "id")
    monkeypatch.setenv("CAREERAGENT_ADZUNA_APP_KEY", "key")
    client = _Client(
        {
            "results": [
                {
                    "title": "Data Scientist",
                    "company": {"display_name": "Acme Analytics"},
                    "location": {"display_name": "Bengaluru, Karnataka"},
                    "redirect_url": "https://adzuna.in/job/1",
                    "created": "2026-09-01T10:00:00Z",
                    "category": {"label": "IT Jobs"},
                },
                {"title": "No Company", "company": {}},
            ]
        }
    )
    result = await Adzuna().search("Data Scientist", "Bangalore", client=client)

    assert result.ok
    assert result.calls == 1
    assert len(result.hits) == 1, "a row with no employer name is not a lead"
    assert result.hits[0].name == "Acme Analytics"
    assert result.hits[0].posted_date == date(2026, 9, 1)
    assert "/jobs/in/" in client.calls[0][0], "Bangalore must route to the India market"


@pytest.mark.parametrize(
    ("location", "country"),
    [
        ("Bangalore", "in"),
        ("Chennai, India", "in"),
        ("London", "gb"),
        ("New York", "us"),
        ("Singapore", "sg"),
        ("Somewhere Unknown", "in"),
    ],
)
def test_adzuna_country_routing(location, country):
    assert Adzuna.country_for(location) == country


async def test_adzuna_reports_quota_exhaustion(monkeypatch):
    """A 429 must stop the campaign re-asking, not look like an empty region."""
    monkeypatch.setenv("CAREERAGENT_ADZUNA_APP_ID", "id")
    monkeypatch.setenv("CAREERAGENT_ADZUNA_APP_KEY", "key")
    result = await Adzuna().search("x", "Bangalore", client=_Client(None, status=429))
    assert result.quota_exhausted
    assert not result.ok


async def test_a_source_without_a_key_fails_softly(monkeypatch):
    monkeypatch.delenv("CAREERAGENT_ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    result = await Adzuna().search("x", "Bangalore", client=_Client({}))
    assert not result.ok
    assert result.hits == []
    assert "key" in result.error


async def test_jsearch_keeps_the_employer_website(monkeypatch):
    """The most valuable field any aggregator returns: it feeds ATS resolution."""
    monkeypatch.setenv("CAREERAGENT_RAPIDAPI_KEY", "k")
    client = _Client(
        {
            "data": [
                {
                    "employer_name": "Acme",
                    "employer_website": "https://www.acme.com/about",
                    "job_title": "ML Engineer",
                    "job_city": "Bengaluru",
                    "job_state": "Karnataka",
                    "job_apply_link": "https://acme.com/apply",
                }
            ]
        }
    )
    result = await JSearch().search("ML Engineer", "Bangalore", client=client)
    assert result.hits[0].domain == "acme.com"
    assert result.hits[0].location == "Bengaluru, Karnataka"


async def test_an_aggregator_domain_is_not_the_employers(monkeypatch):
    monkeypatch.setenv("CAREERAGENT_RAPIDAPI_KEY", "k")
    client = _Client(
        {
            "data": [
                {
                    "employer_name": "Acme",
                    "employer_website": "https://www.linkedin.com/company/acme",
                    "job_title": "ML Engineer",
                }
            ]
        }
    )
    result = await JSearch().search("x", "Bangalore", client=client)
    assert result.hits[0].domain is None


async def test_jooble_posts_rather_than_gets(monkeypatch):
    monkeypatch.setenv("CAREERAGENT_JOOBLE_KEY", "k")
    client = _Client({"jobs": [{"company": "Acme", "title": "Analyst", "location": "Pune"}]})
    result = await Jooble().search("Analyst", "Pune", client=client)
    assert result.hits[0].name == "Acme"
    assert client.calls[0][1]["keywords"] == "Analyst"


async def test_jooble_names_the_country_because_a_bare_city_returns_nothing(monkeypatch):
    """Measured against the live API: "Chennai" returns 0 rows, "Chennai, India" returns 83."""
    monkeypatch.setenv("CAREERAGENT_JOOBLE_KEY", "k")
    client = _Client({"jobs": []})
    await Jooble().search("Analyst", "Chennai", client=client)
    assert client.calls[0][1]["location"] == "Chennai, India"


# ─── Qualifying a location ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Bangalore", "Bangalore, India"),
        ("Bengaluru", "Bengaluru, India"),
        ("Pune", "Pune, India"),
        ("London", "London, United Kingdom"),
        ("Austin", "Austin, United States"),
        ("Dubai", "Dubai, United Arab Emirates"),
        # Already qualified — do not say it twice.
        ("Bangalore, India", "Bangalore, India"),
        ("India", "India"),
        ("Singapore", "Singapore"),
        ("United Kingdom", "United Kingdom"),
        # No country to name.
        ("Remote", "Remote"),
        ("Work from home", "Work from home"),
        ("Anywhere", "Anywhere"),
        ("", ""),
        ("  ", ""),
    ],
)
def test_a_bare_city_gains_its_country_and_nothing_else_does(location, expected):
    assert qualify(location) == expected


def test_qualifying_is_idempotent():
    """A location that has been through this once must not grow a second country."""
    for location in ("Bangalore", "London", "Remote", "Chennai, India"):
        assert qualify(qualify(location)) == qualify(location)


async def test_remotive_needs_no_key():
    result = await Remotive().search(
        "Data", "Remote", client=_Client({"jobs": [{"company_name": "Acme", "title": "Data Eng"}]})
    )
    assert result.ok
    assert result.hits[0].name == "Acme"


async def test_unparseable_json_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("CAREERAGENT_ADZUNA_APP_ID", "id")
    monkeypatch.setenv("CAREERAGENT_ADZUNA_APP_KEY", "key")
    result = await Adzuna().search("x", "Bangalore", client=_Client(None, status=200))
    assert not result.ok
    assert result.hits == []


def test_keyless_sources_are_always_available():
    """A fresh install with no signup must still return something."""
    keyless = [s for s in dirsrc.SOURCES if not s.needs_key]
    assert keyless
    assert all(s.configured() for s in keyless)


def test_describe_tells_you_where_to_get_missing_keys():
    for source in dirsrc.describe():
        if source["needs_key"] and not source["configured"]:
            assert source["signup"].startswith("http"), source["id"]


# ─── The campaign graph ─────────────────────────────────────────────────────


def test_campaign_graph_has_five_agents():
    from careeragent.graph import build_campaign_graph

    nodes = {n for n in build_campaign_graph().get_graph().nodes if not n.startswith("__")}
    assert nodes == {
        "profile_agent",
        "sourcing_agent",
        "aggregate_agent",
        "enrichment_agent",
        "rank_agent",
    }


def test_sourcing_fan_out_respects_the_call_budget():
    """A single run must not spend a month of JSearch's 200 free calls."""
    from careeragent.graph.campaign import CALL_BUDGET, fan_out_sourcing

    state = {
        "resolved_families": [
            {"id": f} for f in ("machine_learning", "data_analytics", "nlp", "data_science")
        ],
        "locations": ["Bangalore", "Chennai", "Pune"],
        "max_age_days": 30,
    }
    sends = fan_out_sourcing(state)
    if isinstance(sends, str):
        pytest.skip("no directory source is configured in this environment")

    per_source: dict[str, int] = {}
    for send in sends:
        source_id = send.arg["source_id"]
        per_source[source_id] = per_source.get(source_id, 0) + 1

    for source_id, count in per_source.items():
        assert count <= CALL_BUDGET.get(source_id, 10), source_id


def test_sourcing_fan_out_stops_on_a_profile_error():
    from langgraph.graph import END

    from careeragent.graph.campaign import fan_out_sourcing

    assert fan_out_sourcing({"error": "no candidate"}) == END


def test_enrichment_fan_out_is_capped():
    """Resolving an ATS costs real requests against the company's own site."""
    from careeragent.graph.campaign import fan_out_enrichment

    state = {
        "companies": [{"key": f"c{i}", "name": f"Co {i}"} for i in range(100)],
        "enrich_limit": 12,
    }
    sends = fan_out_enrichment(state)
    assert len(sends) == 12


def test_enrichment_fan_out_skips_to_ranking_when_there_is_nothing():
    from careeragent.graph.campaign import fan_out_enrichment

    assert fan_out_enrichment({"companies": []}) == "rank_agent"


def test_campaign_state_reducers_merge_concurrent_writes():
    """The reason this is a graph: a dozen agents append to one list."""
    from careeragent.graph.state import CampaignState, _merge_counts

    assert _merge_counts({"adzuna": 2}, {"adzuna": 3, "jooble": 1}) == {
        "adzuna": 5,
        "jooble": 1,
    }
    assert "hits" in CampaignState.__annotations__


# ─── The apply graph ────────────────────────────────────────────────────────


def test_apply_graph_has_five_nodes():
    from careeragent.graph import build_apply_graph

    nodes = {n for n in build_apply_graph().get_graph().nodes if not n.startswith("__")}
    assert nodes == {
        "tailor_agent",
        "answer_agent",
        "form_agent",
        "approval_gate",
        "submit_agent",
    }


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"status": "awaiting_approval", "mode": "preview"}, "approval_gate"),
        ({"status": "awaiting_approval", "mode": "one_click"}, "submit_agent"),
        ({"status": "human_required", "mode": "one_click"}, "__end__"),
        ({"needs_user": ["Country"], "mode": "one_click"}, "__end__"),
        ({"holds": ["Country is empty"], "mode": "one_click"}, "__end__"),
        ({"error": "boom", "mode": "one_click"}, "__end__"),
        ({"status": "failed", "mode": "one_click"}, "__end__"),
    ],
)
def test_route_after_form(state, expected):
    """No mode can route past an unanswerable field, a hold, or a blocker."""
    from careeragent.graph.apply import route_after_form

    assert route_after_form(state) == expected


def test_one_click_cannot_override_a_human_requirement():
    """The headline safety property of the fast path."""
    from careeragent.graph.apply import route_after_form

    assert (
        route_after_form(
            {"mode": "one_click", "status": "human_required", "needs_user": ["Gender"]}
        )
        == "__end__"
    )


def test_threshold_mode_submits_only_above_the_bar():
    from careeragent.graph.apply import DEFAULT_THRESHOLD, route_after_form

    high = {"mode": "threshold", "validation": {"match_score": DEFAULT_THRESHOLD + 0.05}}
    low = {"mode": "threshold", "validation": {"match_score": DEFAULT_THRESHOLD - 0.05}}
    assert route_after_form(high) == "submit_agent"
    assert route_after_form(low) == "approval_gate"


def test_declining_an_approval_does_not_submit():
    from careeragent.graph.apply import route_after_approval

    assert route_after_approval({"approved": False}) == "__end__"
    assert route_after_approval({"approved": True}) == "submit_agent"


def test_modes_are_declared():
    from careeragent.graph.apply import MODES

    assert set(MODES) == {"one_click", "preview", "threshold"}


# ─── The pool ───────────────────────────────────────────────────────────────


def test_pool_concurrency_is_bounded():
    """Each worker is a Chromium instance; unbounded is a memory bug."""
    from careeragent.graph.pool import DEFAULT_CONCURRENCY, MAX_CONCURRENCY

    assert DEFAULT_CONCURRENCY == 3
    assert MAX_CONCURRENCY <= 5


async def test_pool_refuses_a_second_batch():
    from careeragent.graph.pool import ApplyPool, Batch, Item

    pool = ApplyPool()
    pool._batches["a"] = Batch(id="a", items=[Item(job_id="j")])
    pool._current = "a"

    class _Never:
        def done(self) -> bool:
            return False

    pool._task = _Never()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="already running"):
        await pool.start(["j2"], "cand")


def test_batch_summary_separates_submitted_from_unverified():
    """Counting an unverified submission as applied corrupts every later number."""
    from careeragent.graph.pool import Batch, Item

    batch = Batch(
        id="b",
        items=[
            Item(job_id="1", status="applied"),
            Item(job_id="2", status="submission_unverified"),
            Item(job_id="3", status="human_required"),
            Item(job_id="4", status="error"),
        ],
    )
    summary = batch.summary()
    assert summary["submitted"] == 1
    assert summary["unverified"] == 1
    assert summary["needs_you"] == 1
    assert summary["failed"] == 1
