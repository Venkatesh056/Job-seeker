"""Fetching the description a job was discovered without.

Apollo's feed reaches companies whose careers page cannot be read, which is its value, and
hands back openings with no body text. Skill matching reads the description, so a job
without one matches zero skills — and the scorer cannot tell "wants none of your skills"
from "nobody has read this yet". Measured: all 211 Apollo postings scored reject, the best
at 0.33, including a one-day-old internship in Bangalore.

The tests that matter here are about **not** fetching. 211 requests to other people's
servers, to score jobs the user will never see, is not a reasonable trade — so the filters
that run on what a description-less posting already carries are the thing worth pinning.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from careeragent.models.candidate import SearchConfig
from careeragent.pipeline import describe

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _job(
    *,
    title="Data Analyst",
    location="Bangalore",
    posted_days_ago=1,
    url="https://acme.com/jobs/1",
):
    return SimpleNamespace(
        id="job-1",
        title=title,
        location=location,
        posted_date=date.today() - timedelta(days=posted_days_ago),
        description="",
        listings=[SimpleNamespace(url=url)] if url else [],
    )


def _config(**overrides):
    values: dict = {
        "locations": ["Bangalore", "Chennai"],
        "max_job_age_days": 30,
        "experience_band": "entry",
        "work_modes": ["remote", "hybrid", "onsite"],
    }
    values.update(overrides)
    return SearchConfig(**values)


# ─── What is worth a request ────────────────────────────────────────────────


def test_a_fresh_local_junior_posting_is_worth_reading():
    assert describe.worth_fetching(_job(), _config()) == ""


def test_a_posting_with_no_link_cannot_be_read():
    assert "no URL" in describe.worth_fetching(_job(url=""), _config())


def test_a_stale_posting_is_not_worth_a_request():
    """It would be rejected for its age with or without a description."""
    reason = describe.worth_fetching(_job(posted_days_ago=90), _config())
    assert "more than 30 days" in reason


def test_a_senior_posting_is_not_worth_a_request_to_a_junior_candidate():
    """Unreachable however well its description reads, and the title already says so."""
    reason = describe.worth_fetching(_job(title="Senior Data Scientist"), _config())
    assert "senior-level" in reason


def test_a_lead_posting_is_refused_the_same_way():
    reason = describe.worth_fetching(_job(title="Lead Data Engineer"), _config())
    assert "lead-level" in reason


def test_a_senior_posting_is_read_when_the_candidate_is_senior():
    reason = describe.worth_fetching(
        _job(title="Senior Data Scientist"), _config(experience_band="senior")
    )
    assert reason == ""


def test_a_posting_in_another_country_cannot_become_local_by_being_read():
    reason = describe.worth_fetching(_job(location="Austin, Texas, United States"), _config())
    assert "not somewhere you want" in reason


def test_a_country_is_less_specific_than_a_city_not_somewhere_else():
    """A posting that says only "India" is not a mismatch with "Bangalore" — it is
    unknown, and unknown is exactly what one request resolves. Sixteen real postings were
    being refused a read on this confusion."""
    assert describe.worth_fetching(_job(location="India"), _config()) == ""


@pytest.mark.parametrize("location", ["Bengaluru", "Bengaluru, Karnataka, India", "Chennai"])
def test_the_places_the_candidate_wants_are_read(location):
    assert describe.worth_fetching(_job(location=location), _config()) == ""


def test_a_remote_posting_is_read_when_remote_is_wanted():
    assert describe.worth_fetching(_job(location="Remote"), _config()) == ""


def test_a_remote_posting_is_skipped_when_remote_is_not_wanted():
    reason = describe.worth_fetching(
        _job(location="Remote"), _config(work_modes=["onsite", "hybrid"])
    )
    assert "not looking for remote" in reason


def test_a_posting_with_no_location_at_all_is_not_guessed_about():
    reason = describe.worth_fetching(_job(location=""), _config(work_modes=["onsite"]))
    assert reason, "an unknown location with no remote preference is not worth a request"


def test_no_location_preference_means_every_place_is_acceptable():
    reason = describe.worth_fetching(
        _job(location="Austin, Texas, United States"), _config(locations=[])
    )
    assert reason == ""


# ─── Reading the page ───────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status=200, text="", content_type="text/html"):
        self.status_code = status
        self.text = text
        self.headers = {"content-type": content_type}


class FakeClient:
    def __init__(self, response):
        self._response = response
        self.urls: list[str] = []

    async def get(self, url, headers=None):
        self.urls.append(url)
        return self._response


async def test_a_readable_page_gives_its_text():
    html = "<html><body><p>" + ("We use python and sql. " * 40) + "</p></body></html>"
    client = FakeClient(FakeResponse(text=html))

    text = await describe._read("https://acme.com/jobs/1", client)

    assert "python" in text
    assert len(text) > describe.MIN_USEFUL_CHARS


async def test_a_missing_page_is_empty_not_an_error():
    """A posting that cannot be read leaves the job exactly as it was — the same state it
    was already in."""
    assert await describe._read("https://acme.com/x", FakeClient(FakeResponse(404))) == ""


async def test_a_non_html_response_is_ignored():
    client = FakeClient(FakeResponse(text="{}", content_type="application/json"))
    assert await describe._read("https://acme.com/x", client) == ""


async def test_a_network_failure_is_empty_not_a_crash():
    import httpx

    class Broken:
        async def get(self, *a, **k):
            raise httpx.ConnectTimeout("nope")

    assert await describe._read("https://acme.com/x", Broken()) == ""


# ─── End to end against a real database ─────────────────────────────────────


async def test_only_the_worthwhile_jobs_are_fetched_and_rescored(isolated, monkeypatch):
    from careeragent.storage import session_scope
    from careeragent.storage.models import Job

    candidate, candidate_id = await _seed(
        [
            ("job-fresh", "Data Analyst", "Bangalore", 2),
            ("job-stale", "Data Analyst", "Bangalore", 200),
            ("job-senior", "Senior Data Architect", "Bangalore", 2),
            ("job-abroad", "Data Analyst", "Austin, Texas, United States", 2),
        ]
    )

    fetched: list[str] = []

    async def fake_read(url, client):
        fetched.append(url)
        return "We are looking for someone with python and sql. " * 30

    monkeypatch.setattr(describe, "_read", fake_read)

    async with session_scope() as session:
        result = await describe.describe_missing(session, candidate, candidate_id)

    assert result.considered == 4
    assert result.attempted == 1, "only the fresh, local, junior posting"
    assert result.described == 1
    assert len(fetched) == 1

    async with session_scope() as session:
        assert (await session.get(Job, "job-fresh")).description
        assert not (await session.get(Job, "job-stale")).description


async def test_a_page_that_renders_nothing_is_counted_not_stored(isolated, monkeypatch):
    """A JavaScript shell yields a handful of characters. Storing that as a description
    would be worse than storing nothing — the scorer would read it as real evidence."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import Job

    candidate, candidate_id = await _seed([("job-1", "Data Analyst", "Bangalore", 2)])

    async def empty_shell(url, client):
        return "Loading..."

    monkeypatch.setattr(describe, "_read", empty_shell)

    async with session_scope() as session:
        result = await describe.describe_missing(session, candidate, candidate_id)

    assert result.attempted == 1
    assert result.described == 0
    assert result.unreadable == 1

    async with session_scope() as session:
        assert (await session.get(Job, "job-1")).description == ""


async def test_a_job_already_applied_to_is_not_fetched_again(isolated, monkeypatch):
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    candidate, candidate_id = await _seed([("job-1", "Data Analyst", "Bangalore", 2)])
    async with session_scope() as session:
        await repo.create_application(session, "job-1", candidate_id)

    async def must_not_read(url, client):
        raise AssertionError("a job already applied to needs no description")

    monkeypatch.setattr(describe, "_read", must_not_read)

    async with session_scope() as session:
        result = await describe.describe_missing(session, candidate, candidate_id)

    assert result.attempted == 0


async def test_the_freshest_jobs_are_read_first_when_the_limit_bites(isolated, monkeypatch):
    """If the limit cuts the list short, the jobs most likely to still be open are the
    ones that got read."""
    from careeragent.storage import session_scope

    candidate, candidate_id = await _seed(
        [
            ("job-old", "Data Analyst", "Bangalore", 25),
            ("job-new", "Data Engineer", "Bangalore", 1),
            ("job-mid", "Data Scientist", "Bangalore", 10),
        ]
    )

    fetched: list[str] = []

    async def fake_read(url, client):
        fetched.append(url)
        return "python sql pandas " * 60

    monkeypatch.setattr(describe, "_read", fake_read)

    async with session_scope() as session:
        await describe.describe_missing(session, candidate, candidate_id, limit=1)

    assert fetched == ["https://acme.com/job-new"]


async def test_nothing_worth_reading_says_so_plainly(isolated):
    from careeragent.storage import session_scope

    candidate, candidate_id = await _seed([("job-1", "Data Analyst", "Bangalore", 300)])

    async with session_scope() as session:
        result = await describe.describe_missing(session, candidate, candidate_id)

    assert result.considered == 1
    assert result.attempted == 0
    assert "stale, senior, or outside" in result.note()


# ─── Fixtures ───────────────────────────────────────────────────────────────


async def _seed(specs):
    """Jobs with no description, plus a candidate who wants Bangalore data work."""
    from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo
    from careeragent.storage.models import Job, JobListing

    await init_db(drop=True)
    candidate = Candidate(
        name="T",
        email="t@example.com",
        location="Bangalore",
        preferred_locations=["Bangalore", "Chennai"],
        facts=[
            Fact(
                kind=FactKind.SKILL,
                label=skill,
                evidence_source=EvidenceSource.MASTER_RESUME,
                evidence_locator="cv.pdf#page=1",
            )
            for skill in ("python", "sql")
        ],
    )
    async with session_scope() as session:
        row = await repo.save_candidate_profile(session, candidate)
        await repo.replace_facts(session, row.id, candidate.facts)
        from careeragent import preferences

        await preferences.save(
            session,
            row.id,
            SearchConfig(
                locations=["Bangalore", "Chennai"],
                max_job_age_days=30,
                experience_band="entry",
                work_modes=["remote", "hybrid", "onsite"],
            ),
        )
        for job_id, title, location, age in specs:
            session.add(
                Job(
                    id=job_id,
                    company="Acme",
                    title=title,
                    normalized_title=title.lower(),
                    location=location,
                    description="",
                    status="open",
                    posted_date=date.today() - timedelta(days=age),
                )
            )
            session.add(
                JobListing(
                    job_id=job_id,
                    source="apollo",
                    source_job_id=job_id,
                    url=f"https://acme.com/{job_id}",
                )
            )
        await session.flush()
        return candidate, row.id


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
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
