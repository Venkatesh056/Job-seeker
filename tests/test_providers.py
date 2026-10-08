"""ATS provider parsing and slug derivation.

These run against captured response shapes, never the live APIs. A parser test
that needs the network is a test that fails when someone else deploys.

The `live` marker covers the handful that do hit real endpoints; they are
excluded by default (see pyproject.toml).
"""

from __future__ import annotations

import pytest

from careeragent.sources.ats.providers import (
    PROVIDERS,
    Ashby,
    BambooHR,
    Breezy,
    Greenhouse,
    Lever,
    Personio,
    Recruitee,
    Rippling,
    SmartRecruiters,
    Workday,
    slug_variants,
    strip_html,
)

# ─── Slug derivation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://boards.greenhouse.io/stripe", "stripe"),
        ("https://job-boards.greenhouse.io/acme/jobs/123", "acme"),
        ("https://boards.greenhouse.io/embed/job_board?for=widgetco", "widgetco"),
        ("stripe.com", "stripe"),
    ],
)
def test_greenhouse_slug(value: str, expected: str) -> None:
    assert Greenhouse.slug_from(value) == expected


def test_workday_slug_carries_host_and_site() -> None:
    url = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x"
    assert Workday.slug_from(url) == "nvidia.wd5.myworkdayjobs.com::NVIDIAExternalCareerSite"


def test_workday_slug_skips_locale_segment() -> None:
    url = "https://acme.wd1.myworkdayjobs.com/fr-FR/Careers"
    assert Workday.slug_from(url) == "acme.wd1.myworkdayjobs.com::Careers"


def test_workday_slug_empty_for_non_workday_host() -> None:
    assert Workday.slug_from("https://acme.com/careers") == ""


def test_slug_variants_covers_observed_differences() -> None:
    # datadoghq.com's real board is `datadog`
    assert "datadog" in slug_variants("datadoghq")
    assert slug_variants("acme")[0] == "acme"
    assert "acme" in slug_variants("get-acme")


# ─── Parsing ────────────────────────────────────────────────────────────────


def test_greenhouse_parse() -> None:
    payload = {
        "jobs": [
            {
                "id": 42,
                "title": "ML Engineer",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                "location": {"name": "Bangalore, India"},
                "departments": [{"name": "Engineering"}],
                "offices": [{"name": "India"}],
                "first_published": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-02T00:00:00Z",
                "content": "<p>Build models.</p><ul><li>PyTorch</li></ul>",
            }
        ]
    }
    jobs = Greenhouse.parse(payload, "acme")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.title == "ML Engineer"
    assert job.source_job_id == "42"
    assert job.location == "Bangalore, India"
    assert job.department == "Engineering"
    assert "PyTorch" in job.description
    assert "<p>" not in job.description


def test_greenhouse_parse_rejects_foreign_shape() -> None:
    assert Greenhouse.parse({"offers": []}, "acme") is None
    assert Greenhouse.parse([], "acme") is None


def test_lever_parse_and_epoch_date() -> None:
    payload = [
        {
            "id": "abc",
            "text": "Data Scientist",
            "hostedUrl": "https://jobs.lever.co/acme/abc",
            "categories": {"location": "Remote", "department": "Data", "commitment": "Full-time"},
            "createdAt": 1756684800000,
            "descriptionPlain": "Work with data.",
        }
    ]
    jobs = Lever.parse(payload, "acme")
    assert jobs is not None and jobs[0].title == "Data Scientist"
    assert jobs[0].remote is True
    assert jobs[0].published_at is not None and jobs[0].published_at.startswith("2025-")


def test_lever_rejects_rippling_payload() -> None:
    """Lever, Rippling and Breezy all return bare arrays. Shape proves nothing —
    mistaking one for another yields jobs with empty titles, failing silently."""
    rippling_payload = [{"uuid": "u1", "name": "Engineer", "url": "https://x"}]
    assert Lever.parse(rippling_payload, "acme") is None
    assert Rippling.parse(rippling_payload, "acme") is not None


def test_rippling_rejects_lever_payload() -> None:
    lever_payload = [{"id": "1", "text": "Engineer", "hostedUrl": "https://x"}]
    assert Rippling.parse(lever_payload, "acme") is None


def test_breezy_rejects_other_bare_arrays() -> None:
    assert Breezy.parse([{"uuid": "u", "name": "X"}], "acme") is None
    assert Breezy.parse([{"friendly_id": "f", "id": "1", "name": "X", "url": "u"}], "acme")


def test_ashby_parse() -> None:
    payload = {
        "jobs": [
            {
                "id": "j1",
                "title": "  AI Engineer  ",
                "jobUrl": "https://jobs.ashbyhq.com/acme/j1",
                "location": "Remote",
                "isRemote": True,
                "employmentType": "FullTime",
                "descriptionPlain": "Ship agents.",
            }
        ]
    }
    jobs = Ashby.parse(payload, "acme")
    assert jobs is not None and jobs[0].title == "AI Engineer"  # stripped
    assert jobs[0].remote is True


def test_smartrecruiters_paginates_to_total() -> None:
    payload = {"totalFound": 250, "content": []}
    assert SmartRecruiters.next_offset(payload, 100) == 100
    assert SmartRecruiters.next_offset(payload, 250) is None


def test_workday_paginates_and_keeps_relative_date_as_label() -> None:
    payload = {
        "total": 40,
        "jobPostings": [
            {
                "title": "MLOps Engineer",
                "externalPath": "/job/Bangalore/MLOps_JR123",
                "locationsText": "Bangalore, India",
                "bulletFields": ["JR123"],
                "postedOn": "Posted Yesterday",
            }
        ],
    }
    jobs = Workday.parse(payload, "acme.wd5.myworkdayjobs.com::Careers")
    assert jobs is not None
    job = jobs[0]
    assert job.source_job_id == "JR123"
    # A relative label must not be passed off as a date.
    assert job.published_at is None
    assert job.posted_label == "Posted Yesterday"
    assert Workday.next_offset(payload, 20) == 20
    assert Workday.next_offset(payload, 40) is None


def test_workday_body_is_fixed_at_twenty() -> None:
    """Raising the limit returns nothing at all, so this must stay 20."""
    assert Workday.body("host::Site", 40) == {
        "appliedFacets": {},
        "limit": 20,
        "offset": 40,
        "searchText": "",
    }


def test_recruitee_parse_salary() -> None:
    payload = {
        "offers": [
            {
                "id": 7,
                "title": "NLP Engineer",
                "careers_url": "https://acme.recruitee.com/o/nlp",
                "location": "Pune",
                "salary": {"min": 10, "max": 20, "currency": "INR", "period": "year"},
                "employment_type_code": "full_time",
            }
        ]
    }
    jobs = Recruitee.parse(payload, "acme")
    assert jobs is not None
    assert jobs[0].salary == "10–20 INR/year"
    assert jobs[0].employment_type == "full time"


def test_recruitee_endpoint_keeps_trailing_slash() -> None:
    """Without it the API 404s."""
    assert Recruitee.endpoint("acme").endswith("/api/offers/")


def test_bamboohr_parse() -> None:
    payload = {
        "result": [
            {
                "id": 9,
                "jobOpeningName": "Data Analyst",
                "location": {"city": "Chennai", "state": "TN"},
                "departmentLabel": "Analytics",
                "isRemote": False,
            }
        ]
    }
    jobs = BambooHR.parse(payload, "acme")
    assert jobs is not None
    assert jobs[0].url == "https://acme.bamboohr.com/careers/9"
    assert jobs[0].location == "Chennai, TN"


def test_personio_parses_xml() -> None:
    xml = """<workzag-jobs><position>
      <id>123</id><name>GenAI Intern</name><office>Berlin</office>
      <department>AI</department><employmentType>intern</employmentType>
      <jobDescriptions><![CDATA[<p>Build things.</p>]]></jobDescriptions>
    </position></workzag-jobs>"""
    jobs = Personio.parse(xml, "acme")
    assert jobs is not None and len(jobs) == 1
    assert jobs[0].title == "GenAI Intern"
    assert jobs[0].url == "https://acme.jobs.personio.de/job/123"
    assert "Build things." in jobs[0].description


def test_personio_rejects_non_xml() -> None:
    assert Personio.parse({"jobs": []}, "acme") is None


# ─── Invariants across every provider ───────────────────────────────────────


def test_every_provider_returns_none_for_garbage() -> None:
    """None means 'not this platform'. Returning [] instead would read as
    'this board exists and is empty', which is a different and wrong answer."""
    for provider in PROVIDERS:
        assert provider.parse({"unexpected": "shape"}, "acme") is None, provider.id
        assert provider.parse(None, "acme") is None, provider.id


def test_every_provider_declares_empty_semantics() -> None:
    for provider in PROVIDERS:
        assert isinstance(provider.empty_means_exists, bool), provider.id
        assert provider.id and provider.label


def test_strip_html_keeps_list_structure() -> None:
    text = strip_html("<p>Intro</p><ul><li>One</li><li>Two</li></ul>")
    assert "- One" in text
    assert "- Two" in text
    assert "<" not in text


def test_strip_html_unescapes_entities() -> None:
    assert strip_html("R&amp;D &lt;team&gt;") == "R&D <team>"


# ─── Live smoke tests (opt in with: pytest -m live) ─────────────────────────


@pytest.mark.live
@pytest.mark.parametrize(
    ("provider", "slug"),
    [(Greenhouse, "stripe"), (Ashby, "ramp"), (Lever, "palantir")],
)
async def test_live_board_returns_jobs(provider, slug: str) -> None:
    from careeragent.sources.ats import ATSFetcher

    async with ATSFetcher() as fetcher:
        result = await fetcher.fetch_board(provider, slug, include_description=False)
    assert result.ok, result.error
    assert result.jobs, f"{provider.id}:{slug} returned no jobs"
    assert all(j.title and j.url for j in result.jobs)


# ─── Abandoned-board fallback (found on a real company) ─────────────────────


async def test_empty_guessed_board_falls_through_to_the_careers_page(monkeypatch) -> None:
    """Regression, verified against Razorpay.

    `razorpay.breezy.hr` is a REAL but abandoned board: it answers 200 with an
    empty list. Their actual openings live on Greenhouse under the slug
    `razorpaysoftwareprivatelimited`, which no guess would ever produce.
    Accepting the empty board would report "no openings" for a company that is
    actively hiring — the silent-wrong failure this codebase exists to avoid.
    """
    from careeragent.sources.ats import ATSFetcher
    from careeragent.sources.ats.providers import Breezy, Greenhouse

    async with ATSFetcher() as fetcher:

        async def fake_detect(target: str):
            return [(Breezy, "razorpay", "domain_guess")]

        async def fake_discover(host, client, settings):
            return [("greenhouse", "razorpaysoftwareprivatelimited")]

        async def fake_fetch_board(provider, slug, include_description=True, max_pages=50):
            from careeragent.models.job import RawJob
            from careeragent.sources.ats.fetcher import BoardResult

            result = BoardResult(provider=provider.id, slug=slug)
            if provider is Greenhouse:
                result.jobs = [
                    RawJob(
                        source="greenhouse",
                        source_job_id="1",
                        title="Data Scientist",
                        url="https://boards.greenhouse.io/x/jobs/1",
                    )
                ]
            return result

        monkeypatch.setattr(fetcher, "detect", fake_detect)
        monkeypatch.setattr(fetcher, "fetch_board", fake_fetch_board)
        monkeypatch.setattr("careeragent.sources.ats.fetcher.discover_from_website", fake_discover)

        results = await fetcher.fetch_for_target("razorpay.com", include_description=False)

    assert len(results) == 1
    assert results[0].provider == "greenhouse"
    assert results[0].discovered_via == "careers_page"
    assert results[0].jobs, "fell back but found nothing"


async def test_a_productive_guessed_board_is_not_second_guessed(monkeypatch) -> None:
    """The fallback must not fire when the guess already worked — that would
    double every company's request count for nothing."""
    from careeragent.models.job import RawJob
    from careeragent.sources.ats import ATSFetcher
    from careeragent.sources.ats.fetcher import BoardResult
    from careeragent.sources.ats.providers import Greenhouse

    calls: list[str] = []

    async with ATSFetcher() as fetcher:

        async def fake_detect(target: str):
            return [(Greenhouse, "acme", "domain_guess")]

        async def fake_fetch_board(provider, slug, include_description=True, max_pages=50):
            result = BoardResult(provider=provider.id, slug=slug)
            result.jobs = [
                RawJob(source="greenhouse", source_job_id="1", title="ML Intern", url="https://x/1")
            ]
            return result

        async def fake_discover(host, client, settings):
            calls.append(host)
            return []

        monkeypatch.setattr(fetcher, "detect", fake_detect)
        monkeypatch.setattr(fetcher, "fetch_board", fake_fetch_board)
        monkeypatch.setattr("careeragent.sources.ats.fetcher.discover_from_website", fake_discover)

        results = await fetcher.fetch_for_target("acme.com", include_description=False)

    assert results[0].discovered_via == "domain_guess"
    assert not calls, "careers page was fetched despite the guess working"


# ─── Keka (Indian ATS coverage) ─────────────────────────────────────────────


def test_keka_slug_carries_subdomain_and_tenant() -> None:
    """Like Workday, a Keka board needs two identifiers. The UUID is not
    derivable from anything — only the tenant's own careers page has it."""
    from careeragent.sources.ats.providers import Keka

    url = "https://jupiter.keka.com/careers/jobdetails/b5279857-cf81-4dde-a215-fc48957ee2b5"
    assert Keka.slug_from(url) == "jupiter::b5279857-cf81-4dde-a215-fc48957ee2b5"
    # Without a UUID in the URL, only the subdomain is known yet.
    assert Keka.slug_from("https://jupiter.keka.com/careers/") == "jupiter"


def test_keka_endpoint_uses_both_parts() -> None:
    from careeragent.sources.ats.providers import Keka

    endpoint = Keka.endpoint("jupiter::abc-123")
    assert endpoint.startswith("https://jupiter.keka.com/careers/api/embedjobs/default/active/")
    assert endpoint.endswith("abc-123")


def test_keka_empty_proves_nothing() -> None:
    """Verified live: a wrong UUID answers 200 with [], indistinguishable from
    a real board with no openings."""
    from careeragent.sources.ats.providers import Keka

    assert Keka.empty_means_exists is False


def test_keka_parse() -> None:
    from careeragent.sources.ats.providers import Keka

    payload = [
        {
            "id": 141185,
            "title": "Data Analyst",
            "description": "<div>Analyse things</div>",
            "departmentName": "Data",
            "jobLocations": [{"city": "Bengaluru", "state": "KA", "countryName": "India"}],
            "jobType": 4,
            "experience": "0-1 Years",
            "publishedOn": "2026-09-09T05:32:50.197Z",
            "skillNames": ["SQL", "Python"],
        }
    ]
    jobs = Keka.parse(payload, "acme::uuid-1")
    assert jobs is not None and len(jobs) == 1
    job = jobs[0]
    assert job.title == "Data Analyst"
    assert job.location == "Bengaluru, India"
    assert job.employment_type == "internship"  # jobType 4
    assert "Analyse things" in job.description
    assert job.raw["experience"] == "0-1 Years"


def test_keka_rejects_other_bare_arrays() -> None:
    """Lever, Rippling and Breezy also return bare arrays."""
    from careeragent.sources.ats.providers import Keka

    assert Keka.parse([{"id": "1", "text": "Engineer", "hostedUrl": "https://x"}], "a::b") is None
    assert Keka.parse([{"uuid": "u", "name": "X"}], "a::b") is None


@pytest.mark.live
async def test_live_keka_board_returns_jobs() -> None:
    from careeragent.sources.ats import ATSFetcher
    from careeragent.sources.ats.providers import Keka

    async with ATSFetcher() as fetcher:
        result = await fetcher.fetch_board(
            Keka, "jupiter::b5279857-cf81-4dde-a215-fc48957ee2b5", include_description=False
        )
    assert result.ok, result.error
    assert result.jobs


# ─── Freshteam (HTML-based, the one exception) ──────────────────────────────

FRESHTEAM_HTML = """
<html><body><div class="job-list">
  <!-- Do not remove data-portal-* attributes. -->
  <a href="/jobs/68V3608ei65Q/devops-engineer-2" class="heading"
     data-portal-location="Mumbai, India" data-portal-job-type="1"
     data-portal-remote-location=false>
    <div class="job-list-info"><div class="job-title">Devops Engineer - 2</div></div>
    <div class="job-location"><div class="location-info">
        Mumbai, Maharashtra <br/> Contract
    </div></div>
  </a>
  <a href="/jobs/HVivFopqkiC1/voice-ai-engineer" class="heading"
     data-portal-location="Remote" data-portal-job-type="2"
     data-portal-remote-location=true>
    <div class="job-list-info"><div class="job-title">Voice AI Engineer</div></div>
    <div class="job-location"><div class="location-info">
        Remote <br/> Full Time
    </div></div>
  </a>
</div></body></html>
"""


def test_freshteam_parses_job_cards() -> None:
    from careeragent.sources.ats.providers import Freshteam

    jobs = Freshteam.parse(FRESHTEAM_HTML, "haptik")
    assert jobs is not None and len(jobs) == 2

    first = jobs[0]
    assert first.title == "Devops Engineer - 2"
    assert first.source_job_id == "68V3608ei65Q"
    assert first.url == "https://haptik.freshteam.com/jobs/68V3608ei65Q/devops-engineer-2"
    assert first.location == "Mumbai, Maharashtra"
    assert first.employment_type == "Contract"

    second = jobs[1]
    assert second.remote is True
    assert second.employment_type == "Full Time"


def test_freshteam_rejects_unrelated_html() -> None:
    """A company's own careers page must not be mistaken for a Freshteam board."""
    from careeragent.sources.ats.providers import Freshteam

    assert Freshteam.parse("<html><body><h1>We are hiring</h1></body></html>", "acme") is None
    assert Freshteam.parse({"jobs": []}, "acme") is None


def test_freshteam_empty_page_yields_no_jobs_not_a_false_board() -> None:
    """A nonexistent tenant serves a near-empty 200 page, so empty proves
    nothing — verified live against a made-up subdomain."""
    from careeragent.sources.ats.providers import Freshteam

    assert Freshteam.empty_means_exists is False
    assert Freshteam.parse('<html data-portal-job-type="x"></html>', "junk") == []


def test_freshteam_slug_from_host() -> None:
    from careeragent.sources.ats.providers import Freshteam

    assert Freshteam.slug_from("https://haptik.freshteam.com/jobs") == "haptik"
    assert Freshteam.endpoint("haptik") == "https://haptik.freshteam.com/jobs"
