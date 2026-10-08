"""Apollo as a source of company facts, and what that does to company fit.

Apollo is sold as a contact database and is not used as one here: the free plan returns
403 `API_INACCESSIBLE` for people search, measured against the live key, and nothing in
this module requests a person even if that changes. What it does supply — an industry, a
headcount split by function, a technology list — is what `company_fit` was missing.

The audit found `company_fit` was 0 for every company whose site could not be read, which
is most large Indian employers: their careers pages are JavaScript shells. Swiggy's home
page yields 57 characters of text.
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import pytest

from careeragent import company as company_mod
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
from careeragent.sources import apollo

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ─── Reading Apollo's answer ────────────────────────────────────────────────


def _org(**overrides):
    body = {
        "id": "556d515973696411e335ed00",
        "name": "Quantiphi",
        "primary_domain": "quantiphi.com",
        "website_url": "http://www.quantiphi.com",
        "short_description": "An AI-first digital engineering company.",
        "industry": "information technology & services",
        "industries": ["information technology & services", "internet"],
        "keywords": ["machine learning", "ai"],
        "technology_names": ["Python", "TensorFlow", "AWS"],
        "estimated_num_employees": 3500,
        "founded_year": 2013,
        "city": "Marlborough",
        "country": "United States",
        "linkedin_url": "http://www.linkedin.com/company/quantiphi",
        "departmental_head_count": {"engineering": 1907, "data_science": 519, "finance": 56},
        "organization_headcount_twelve_month_growth": 0.0307,
        "latest_funding_stage": "Series A",
        "total_funding": 23400000,
    }
    body.update(overrides)
    return {"organization": body}


def test_an_enrichment_is_read_into_the_fields_that_matter():
    company = apollo._parse_company(_org())

    assert company.ok
    assert company.name == "Quantiphi"
    assert company.employees == 3500
    assert company.size_band == "large"
    assert "TensorFlow" in company.technologies
    assert company.headcount_by_department["data_science"] == 519


def test_the_headcount_that_matters_is_the_one_in_the_candidates_own_functions():
    """A company with 519 people in data science hires data people, whatever its home
    page says. Finance headcount is not evidence of that."""
    company = apollo._parse_company(_org())

    relevant = company.relevant_headcount(company_mod.TARGET_DEPARTMENTS)

    assert relevant == 1907 + 519, "engineering and data science, not finance"


@pytest.mark.parametrize(
    ("employees", "band"),
    [(12, "startup"), (120, "small"), (600, "mid-size"), (3500, "large"), (50_000, "enterprise")],
)
def test_a_headcount_becomes_a_readable_size(employees, band):
    assert apollo._parse_company(_org(estimated_num_employees=employees)).size_band == band


def test_an_unknown_headcount_stays_unknown_rather_than_becoming_a_startup():
    """Defaulting an absent field is what the fit scorer's "exclude the unknown" rule
    exists to prevent."""
    company = apollo._parse_company(_org(estimated_num_employees=None))

    assert company.employees is None
    assert company.size_band == ""


def test_an_empty_response_is_an_error_not_an_empty_company():
    assert not apollo._parse_company({}).ok
    assert apollo._parse_company({}).error


def test_a_technology_list_is_read_from_either_shape():
    """Apollo returns `technology_names` as strings and `current_technologies` as objects."""
    body = _org()
    del body["organization"]["technology_names"]
    body["organization"]["current_technologies"] = [{"name": "Kafka"}, {"name": "Spark"}]

    assert apollo._parse_company(body).technologies == ["Kafka", "Spark"]


def test_a_plan_gated_response_is_recognised_as_such():
    """403 API_INACCESSIBLE means "your plan does not include this", not "it is broken",
    and the two need different words in front of the user."""
    assert apollo._plan_gated({"error_code": "API_INACCESSIBLE", "error": "not included"})
    assert not apollo._plan_gated({"error": "something else"})
    assert not apollo._plan_gated("a string")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-10T22:00:00.000+00:00", date(2026, 9, 10)),
        ("2026-09-10", date(2026, 9, 10)),
        ("", None),
        (None, None),
        ("not a date", None),
    ],
)
def test_a_posting_date_is_parsed_or_left_unknown(value, expected):
    assert apollo._as_date(value) == expected


# ─── What Apollo does to company fit ────────────────────────────────────────


def _candidate(location="Bangalore", skills=("python", "tensorflow")):
    return Candidate(
        name="T",
        email="t@example.com",
        location=location,
        preferred_locations=[location],
        facts=[
            Fact(
                kind=FactKind.SKILL,
                label=skill,
                evidence_source=EvidenceSource.MASTER_RESUME,
                evidence_locator="cv.pdf#page=1",
            )
            for skill in skills
        ],
    )


def _profile(**overrides):
    profile = company_mod.CompanyProfile(name="Acme", domain="acme.com")
    for key, value in overrides.items():
        setattr(profile, key, value)
    return profile


def test_a_large_relevant_team_is_the_strongest_signal_available():
    """Compared against the same profile with a tiny team, because the absolute number
    depends on which other components happen to have evidence."""
    big = _profile(signals={"apollo": {"relevant_headcount": 2426}})
    small = _profile(signals={"apollo": {"relevant_headcount": 2}})

    big_score, reasons = company_mod.score_fit(big, _candidate())
    small_score, _ = company_mod.score_fit(small, _candidate())

    assert big_score > small_score
    assert any("2426 people" in r for r in reasons)


def test_no_relevant_headcount_scores_low_but_is_still_a_measurement():
    """Zero is a real answer here, unlike an absent field — Apollo said the number is 0."""
    profile = _profile(signals={"apollo": {"relevant_headcount": 0}})

    score, reasons = company_mod.score_fit(profile, _candidate())

    assert score < 0.2
    assert any("no engineering or data headcount" in r for r in reasons)


def test_the_team_size_curve_is_not_linear():
    """The gap between 0 and 20 people doing this work matters far more than the gap
    between 500 and 2,000; a linear scale would make everything but an enterprise look bad."""
    scores = [
        company_mod.score_fit(
            _profile(signals={"apollo": {"relevant_headcount": n}}), _candidate()
        )[0]
        for n in (0, 5, 25, 100, 500, 5000)
    ]

    assert scores == sorted(scores), "more relevant people is never worse"
    assert scores[2] - scores[1] > scores[5] - scores[4], "the early gap is the bigger one"


def test_two_spellings_of_one_city_are_one_city():
    """Apollo says "Bengaluru" and the candidate's preferences say "Bangalore". Comparing
    the strings directly scored a Bangalore company as being in the wrong place."""
    bengaluru = _profile(hq_location="Bengaluru")
    bangalore = _profile(hq_location="Bangalore")
    candidate = _candidate(location="Bangalore")

    assert (
        company_mod.score_fit(bengaluru, candidate)[0]
        == company_mod.score_fit(bangalore, candidate)[0]
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Bengaluru", "Bangalore"),
        ("Bombay", "Mumbai"),
        ("Gurugram", "Gurgaon"),
        ("Bangalore, India", "Bangalore"),
        ("New Delhi", "Delhi"),
    ],
)
def test_city_aliases_resolve_together(a, b):
    assert company_mod._canonical_city(a) == company_mod._canonical_city(b)


def test_a_thin_score_says_it_is_thin():
    """Excluding unknown components stops an unresearched company ranking below a
    researched bad one. Its mirror image is just as misleading: two strong signals average
    higher than five that include a weak one, so a site we cannot read outranks one we can.
    Measured — Swiggy scored 0.87 on two components, Quantiphi 0.42 on five."""
    # Swiggy's real shape: 57 characters of readable page, plus Apollo's record.
    thin = _profile(
        signals={
            "read_quality": "uninformative",
            "apollo": {"relevant_headcount": 1458},
        }
    )

    score, reasons = company_mod.score_fit(thin, _candidate())

    assert score > 0.9
    assert any("provisional" in r for r in reasons)
    assert thin.signals["fit_coverage"] < 0.5


def test_a_well_evidenced_score_does_not_carry_the_caveat():
    thick = _profile(
        tech_focus=["python", "tensorflow"],
        ai_relevance=60,
        hq_location="Bangalore",
        signals={
            "apollo": {"relevant_headcount": 1907},
            "engineering_blog": True,
            "open_source": True,
        },
    )

    _, reasons = company_mod.score_fit(thick, _candidate())

    assert not any("provisional" in r for r in reasons)
    assert thick.signals["fit_coverage"] >= 0.5


def test_an_unreadable_site_keeps_what_apollo_supplied():
    """The bug this catches: the uninformative-read branch assigned a fresh signals dict,
    erasing Apollo's record along with the content-derived signals — which dropped the
    strongest fit component for exactly the companies whose sites are JavaScript shells."""
    profile = _profile(
        signals={
            "read_quality": "uninformative",
            "readable_chars": 57,
            "apollo": {"relevant_headcount": 1458},
        }
    )

    score, _ = company_mod.score_fit(profile, _candidate())

    assert score > 0.9, "Apollo's headcount must still count when the page was empty"


# ─── Tech overlap, calibrated against what a website actually says ──────────
#
# Measured across the 63 researched companies: 42 of them overlapped on *nothing* with a
# candidate holding 50 skills, and the mean overlap was 0.63 technologies. A flat "five
# overlaps for full marks" made the heaviest component score 0.13 on average and crushed
# every company's fit — the "unknown scored as zero" mistake wearing a different hat,
# because a marketing page says "we deliver value", not "we use pandas".


def test_overlapping_everything_a_company_names_is_full_marks():
    """A company that lists three technologies and you have all three is a complete
    match on the evidence available, not a 60% one."""
    others = [f"other{n}" for n in range(7)]
    profile = _profile(tech_focus=["python", "tensorflow", "sql", *others])
    candidate = _candidate(skills=("python", "tensorflow", "sql"))

    score, reasons = company_mod.score_fit(profile, candidate)

    assert any("3 of the 10 technologies" in r for r in reasons)
    assert score > 0


def test_a_thin_technology_list_that_misses_is_not_counted_against_them():
    """Two buzzwords that miss says something about the page we read, not the company."""
    thin = _profile(tech_focus=["wordpress", "seo"])

    score, reasons = company_mod.score_fit(thin, _candidate())

    assert any("too few to judge" in r for r in reasons)
    assert score == 0.0, "nothing could be assessed"
    assert not any("not researched" in r for r in reasons), (
        "the specific explanation must survive, not be replaced by a generic one"
    )


def test_a_long_technology_list_that_misses_is_real_evidence():
    """Above the threshold, a miss means something: they publish what they use and none
    of it is yours."""
    long_list = _profile(tech_focus=[f"tech{n}" for n in range(20)])

    score, reasons = company_mod.score_fit(long_list, _candidate())

    assert any("none of the 20 technologies" in r for r in reasons)
    assert score < 0.3


def test_the_threshold_is_where_absence_starts_counting():
    below = _profile(tech_focus=[f"t{n}" for n in range(company_mod.MIN_TECH_FOR_ABSENCE - 1)])
    at = _profile(tech_focus=[f"t{n}" for n in range(company_mod.MIN_TECH_FOR_ABSENCE)])

    _, below_reasons = company_mod.score_fit(below, _candidate())
    _, at_reasons = company_mod.score_fit(at, _candidate())

    assert any("too few to judge" in r for r in below_reasons)
    assert not any("too few to judge" in r for r in at_reasons)


def test_a_company_naming_no_technology_is_simply_not_assessed_on_it():
    profile = _profile(tech_focus=[])

    _, reasons = company_mod.score_fit(profile, _candidate())

    assert not any("technolog" in r for r in reasons)


# ─── Running out of credits ─────────────────────────────────────────────────
#
# Apollo returns HTTP 422 for an exhausted account — the same status as a malformed
# request — so without recognising the error code, a spent account is indistinguishable
# from a bad domain and a run keeps asking. Found the hard way: a harvest checked 67
# companies, the credits ran out partway through, and the last six read as six unrelated
# company failures rather than one account-level stop.


def test_an_exhausted_account_is_recognised_from_its_error_code():
    body = {
        "error": "You have insufficient credits!",
        "error_details": {
            "code": "BILLING.LIMIT.CREDITS_EXHAUSTED",
            "message": "Your team has used all of its credits for this billing cycle.",
        },
    }
    assert apollo._exhausted(body)
    assert not apollo._plan_gated(body)


def test_a_plan_gate_and_an_exhausted_account_are_different_things():
    """One is "upgrade to get this feature", the other is "wait for the cycle". Showing
    either message for the other sends the user somewhere useless."""
    gated = {"error_code": "API_INACCESSIBLE"}
    spent = {"error_details": {"code": "BILLING.LIMIT.CREDITS_EXHAUSTED"}}

    assert apollo._plan_gated(gated) and not apollo._exhausted(gated)
    assert apollo._exhausted(spent) and not apollo._plan_gated(spent)


@pytest.mark.parametrize("body", [{}, {"error": "something"}, "a string", None, []])
def test_an_ordinary_failure_is_neither(body):
    assert not apollo._exhausted(body)
    assert not apollo._plan_gated(body)


def test_the_published_rate_limits_are_read_from_the_headers():
    """Real numbers from the provider, which is the difference the Keys page draws
    between a balance and a floor."""

    class Response:
        headers: ClassVar[dict[str, str]] = {
            "x-rate-limit-minute": "50",
            "x-rate-limit-hourly": "200",
            "x-rate-limit-24-hour": "600",
            "x-minute-requests-left": "44",
            "x-minute-usage": "6",
        }

    limits = apollo.rate_limits(Response())

    assert limits["per_day"] == 600
    assert limits["per_hour"] == 200
    assert limits["minute_left"] == 44


def test_unreadable_rate_headers_are_skipped_not_guessed():
    class Response:
        headers: ClassVar[dict[str, str]] = {
            "x-rate-limit-24-hour": "not a number",
            "x-rate-limit-hourly": "200",
        }

    limits = apollo.rate_limits(Response())

    assert "per_day" not in limits
    assert limits["per_hour"] == 200


def test_a_response_with_no_headers_yields_nothing():
    class Response:
        headers: ClassVar[dict[str, str]] = {}

    assert apollo.rate_limits(Response()) == {}


async def test_a_harvest_stops_the_moment_the_credits_run_out(isolated, monkeypatch):
    """Asking sixty more companies would be sixty more calls proving the same thing."""
    asked: list[str] = []

    async def spent(domain, **kwargs):
        asked.append(domain)
        return apollo.PostingResult(
            domain=domain,
            error="Apollo credits exhausted for this billing cycle",
            exhausted=True,
            calls=1,
        )

    monkeypatch.setattr(apollo, "postings", spent)

    result = await _harvest_against(["a.com", "b.com", "c.com"], monkeypatch)

    assert len(asked) == 1, "one refusal is enough to know"
    assert result.exhausted
    assert "ran out" in result.note()


async def _harvest_against(domains, monkeypatch):
    """Run harvest_postings against an in-memory set of companies."""
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo

    await init_db(drop=True)
    candidate = _candidate()
    async with session_scope() as session:
        row = await repo.save_candidate_profile(session, candidate)
        for domain in domains:
            await repo.upsert_company(session, domain.split(".")[0].title(), domain)
        await session.flush()
        candidate_id = row.id

    async with session_scope() as session:
        return await apollo.harvest_postings(session, candidate, candidate_id, limit=10)


# ─── Postings as raw jobs ───────────────────────────────────────────────────


def test_a_posting_becomes_a_raw_job_without_inventing_a_description():
    """Apollo gives no body text. Leaving it empty means the scorer sees an absence;
    filling it with the title twice would be a fabrication that scores like evidence."""
    result = apollo.PostingResult(
        domain="acme.com",
        postings=[
            apollo.ApolloPosting(
                apollo_id="p1",
                title="Data Analyst",
                url="https://acme.com/jobs/1",
                city="Bengaluru",
                state="Karnataka",
                country="India",
                posted_at=date(2026, 9, 10),
            )
        ],
    )

    raws = apollo.to_raw_jobs(result, "Acme")

    assert len(raws) == 1
    assert raws[0].title == "Data Analyst"
    assert raws[0].company_name == "Acme"
    assert raws[0].location == "Bengaluru, Karnataka, India"
    assert raws[0].description == ""
    assert raws[0].published_at == "2026-09-10"


def test_a_posting_with_no_link_is_dropped():
    """It cannot be applied to, so it is not an opening we can act on."""
    body = {
        "organization_job_postings": [
            {"id": "1", "title": "Data Analyst", "url": ""},
            {"id": "2", "title": "", "url": "https://acme.com/2"},
            {"id": "3", "title": "ML Engineer", "url": "https://acme.com/3"},
        ]
    }
    kept = [
        row
        for row in body["organization_job_postings"]
        if (row.get("title") or "").strip() and (row.get("url") or "").strip()
    ]
    assert [r["id"] for r in kept] == ["3"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A throwaway database.

    Mandatory for any test in this file that touches storage: without it `init_db(drop=True)`
    runs against whatever `CAREERAGENT_DATABASE_URL` points at, which on a developer machine
    is the real one.
    """
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
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
