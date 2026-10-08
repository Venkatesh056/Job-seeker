"""Natural-language parsing and the HTTP API.

The safety property here is that parsing is inert: `/commands` reports what it
understood and what it *would* do, and cannot itself start a run or submit
anything. A misread instruction should waste nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from careeragent.nl import parse_search_command

# ─── Command parsing ────────────────────────────────────────────────────────


def test_parses_role_location_and_recency() -> None:
    config, summary = parse_search_command("Find AI/ML internships in Bangalore posted this week")
    assert "ML Engineer" in config.roles
    assert "Bangalore" in config.locations
    assert config.experience_band == "internship"
    assert config.max_job_age_days == 7
    assert "Bangalore" in summary


def test_parses_multiple_locations() -> None:
    config, _ = parse_search_command("entry-level ML Engineer jobs in Bangalore and Chennai")
    assert {"Bangalore", "Chennai"} <= set(config.locations)
    assert config.experience_band == "entry"


def test_parses_explicit_day_window() -> None:
    config, _ = parse_search_command("AI Engineer roles posted in the last 3 days")
    assert config.max_job_age_days == 3


def test_remote_adds_both_work_mode_and_location() -> None:
    config, _ = parse_search_command("Find remote GenAI internships")
    assert "remote" in config.work_modes
    assert "Remote" in config.locations
    assert "GenAI Engineer" in config.roles


def test_parses_exclusions() -> None:
    config, _ = parse_search_command("Data Analyst roles excluding crypto")
    assert any("crypto" in term for term in config.keywords_exclude)


def test_extracts_a_top_n_limit_without_acting_on_it() -> None:
    """'Apply to the top 10' sets a limit. It must not imply permission to
    apply — that is a separate, explicit action."""
    config, summary = parse_search_command("Apply to the top 10 data analyst roles in Hyderabad")
    assert config.limit == 10
    assert "Data Analyst" in config.roles
    assert "appl" not in summary.lower()


def test_unrecognised_instruction_says_so_rather_than_guessing() -> None:
    config, summary = parse_search_command("do something useful")
    assert not config.roles
    assert not config.locations
    assert "Nothing specific was recognised" in summary


@pytest.mark.parametrize(
    ("text", "role"),
    [
        ("nlp engineer roles", "NLP Engineer"),
        ("data scientist positions", "Data Scientist"),
        ("computer vision jobs", "Computer Vision Engineer"),
        ("backend software engineer", "Software Engineer"),
        ("mlops openings", "MLOps Engineer"),
    ],
)
def test_role_vocabulary(text: str, role: str) -> None:
    config, _ = parse_search_command(text)
    assert role in config.roles


# ─── API ────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client against an isolated database, so tests never touch real data."""
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))

    from careeragent import config as config_module
    from careeragent import storage

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]

    from careeragent.api import app

    with TestClient(app) as c:
        yield c

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]


def test_status_reports_a_working_system(client) -> None:
    from careeragent.sources.ats import PROVIDERS

    body = client.get("/api/v1/status").json()
    assert body["ats_platforms"] == len(PROVIDERS)
    assert body["autonomy_level"] <= 4, "must not default to auto-submit"
    assert body["portal_adapters_enabled"] is False


def test_providers_lists_every_ats(client) -> None:
    """Asserts the platforms we depend on are present, not a magic count —
    adding a provider should not break an unrelated test."""
    from careeragent.sources.ats import PROVIDERS

    providers = client.get("/api/v1/providers").json()
    assert len(providers) == len(PROVIDERS)
    ids = {p["id"] for p in providers}
    assert {"greenhouse", "lever", "ashby", "workday", "keka"} <= ids


def test_candidate_missing_returns_404_with_guidance(client) -> None:
    response = client.get("/api/v1/candidate")
    assert response.status_code == 404
    assert "ingest-resume" in response.json()["detail"]


def test_jobs_empty_before_discovery(client) -> None:
    assert client.get("/api/v1/jobs").json() == []


def test_unknown_job_returns_404(client) -> None:
    assert client.get("/api/v1/jobs/does-not-exist").status_code == 404


def test_command_endpoint_parses_without_acting(client) -> None:
    """The key API safety property: parsing is inert."""
    body = client.post(
        "/api/v1/commands",
        json={"text": "Find AI/ML internships in Bangalore posted this week"},
    ).json()
    assert body["confirmed"] is False
    assert "ML Engineer" in body["understood"]["roles"]
    assert "Bangalore" in body["understood"]["locations"]


def test_command_rejects_empty_input(client) -> None:
    assert client.post("/api/v1/commands", json={"text": ""}).status_code == 422


def test_jobs_query_bounds_are_enforced(client) -> None:
    assert client.get("/api/v1/jobs?limit=99999").status_code == 422
    assert client.get("/api/v1/jobs?min_score=5").status_code == 422


# ─── Timezone handling (regression) ─────────────────────────────────────────


async def test_updating_an_existing_job_does_not_raise_on_timezones(tmp_path, monkeypatch) -> None:
    """Regression found on a real 1,500-job run.

    SQLite has no timestamp type: a UTC-aware datetime reads back naive, and
    comparing it with a fresh `datetime.now(UTC)` raises. It only fires on the
    UPDATE path, so a test that merely inserts would never catch it — which is
    exactly what happened.
    """
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'tz.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))

    from careeragent import config as config_module
    from careeragent import storage
    from careeragent.models.job import RawJob
    from careeragent.pipeline.normalize import normalize_all
    from careeragent.storage import repository as repo

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]

    try:
        await storage.init_db(drop=True)
        jobs = normalize_all(
            [
                RawJob(
                    source="greenhouse",
                    source_job_id="1",
                    title="ML Engineer",
                    url="https://x/1",
                    company_name="Acme",
                )
            ]
        )
        async with storage.session_scope() as session:
            inserted, updated = await repo.upsert_jobs(session, jobs)
        assert (inserted, updated) == (1, 0)

        # The same job again — this is the path that used to raise.
        async with storage.session_scope() as session:
            inserted, updated = await repo.upsert_jobs(session, jobs)
        assert (inserted, updated) == (0, 1)

        async with storage.session_scope() as session:
            row = await session.get(storage.Job, jobs[0].job_id)
            assert row.last_seen_at.tzinfo is not None, "timestamp came back naive"
    finally:
        await storage.dispose()
        config_module.get_settings.cache_clear()
        storage.db._engine = None  # type: ignore[attr-defined]
        storage.db._session_factory = None  # type: ignore[attr-defined]


async def test_a_listing_whose_job_fingerprint_changes_is_repointed(tmp_path, monkeypatch) -> None:
    """Regression from a real 1,500-job run.

    Uniqueness on (source, source_job_id) is global, but the code checked only
    the current job's listings. When a posting's fingerprint shifted between
    runs — the company name resolving differently is enough — the insert hit
    `UNIQUE constraint failed: job_listing.source, job_listing.source_job_id`
    and took the whole run's storage down with it.
    """
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'lst.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))

    from careeragent import config as config_module
    from careeragent import storage
    from careeragent.models.job import RawJob
    from careeragent.pipeline.normalize import normalize_all
    from careeragent.storage import repository as repo

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]

    def raw(company: str) -> RawJob:
        return RawJob(
            source="workable",
            source_job_id="38A0320BC5",
            title="Data Scientist",
            url="https://apply.workable.com/j/38A0320BC5",
            company_name=company,
        )

    try:
        await storage.init_db(drop=True)

        first = normalize_all([raw("Acme")])
        async with storage.session_scope() as session:
            await repo.upsert_jobs(session, first)

        # Same listing, different company name -> different fingerprint.
        second = normalize_all([raw("Acme Technologies")])
        assert second[0].job_id != first[0].job_id

        async with storage.session_scope() as session:
            await repo.upsert_jobs(session, second)  # used to raise IntegrityError

        from sqlalchemy import select

        async with storage.session_scope() as session:
            rows = (await session.execute(select(storage.JobListing))).scalars().all()
        assert len(rows) == 1, "the listing was duplicated instead of re-pointed"
        assert rows[0].job_id == second[0].job_id
    finally:
        await storage.dispose()
        config_module.get_settings.cache_clear()
        storage.db._engine = None  # type: ignore[attr-defined]
        storage.db._session_factory = None  # type: ignore[attr-defined]
