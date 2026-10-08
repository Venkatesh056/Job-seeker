"""Remembering people across sightings.

The property that matters most is that a *weaker* later sighting cannot destroy a better
earlier one. Company pages get redesigned, crawls get thinner, and a record that lost a
verified address to a subsequent empty read would be worse than useless — it would look
like a downgrade in the company rather than in our knowledge of it.

The second property is that relevance is stored with the question it answered. The same
engineering manager is a 96 for an ML candidate and an 8 for someone applying to finance,
so a number with no job attached is unattributable.
"""

from __future__ import annotations

import pytest

from careeragent.models.person import ContactDetail, Person, SourceKind, Verification
from careeragent.people import rank
from careeragent.people import store as people_store

pytestmark = pytest.mark.anyio

TEAM_PAGE = "https://acme.com/team"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def people_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
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


def _person(name="Priya Sharma", title="VP Engineering", *, email=None, verified=True):
    return Person(
        name=name,
        title=title,
        company="Acme",
        email=ContactDetail(
            value=email,
            kind=SourceKind.TEAM_PAGE,
            source_url=TEAM_PAGE,
            verification=Verification.VERIFIED if verified else Verification.UNVERIFIED,
            confidence=0.9 if verified else 0.3,
        )
        if email
        else None,
        sources=[{"url": TEAM_PAGE, "kind": "team_page"}],
    )


async def _company(name="Acme"):
    from careeragent import storage
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    await storage.init_db()
    async with session_scope() as session:
        row = await repo.upsert_company(session, name, f"{name.lower()}.com")
        await session.flush()
        return row.id


async def test_a_person_is_stored_and_read_back(people_db):
    from careeragent.storage import session_scope

    company_id = await _company()

    async with session_scope() as session:
        added, updated = await people_store.save(
            session, company_id, "Acme", [_person(email="priya@acme.com")]
        )
    assert (added, updated) == (1, 0)

    async with session_scope() as session:
        rows = await people_store.for_company(session, company_id)
        stored = people_store.serialize(rows[0], "Acme")
    assert stored["name"] == "Priya Sharma"
    assert stored["function"] == "engineering"
    assert stored["email"] == "priya@acme.com"
    assert stored["email_source"] == TEAM_PAGE
    assert stored["contactable_now"]


async def test_seeing_someone_twice_updates_rather_than_duplicates(people_db):
    from careeragent.storage import session_scope

    company_id = await _company()

    async with session_scope() as session:
        await people_store.save(session, company_id, "Acme", [_person(title="Engineering Manager")])
    async with session_scope() as session:
        added, updated = await people_store.save(
            session, company_id, "Acme", [_person(email="priya@acme.com")]
        )

    assert (added, updated) == (0, 1)
    async with session_scope() as session:
        assert len(await people_store.for_company(session, company_id)) == 1


async def test_a_weaker_later_sighting_does_not_downgrade_a_verified_address(people_db):
    """A thinner crawl must not silently destroy a good record."""
    from careeragent.storage import session_scope

    company_id = await _company()

    async with session_scope() as session:
        await people_store.save(
            session, company_id, "Acme", [_person(email="priya@acme.com", verified=True)]
        )
    async with session_scope() as session:
        await people_store.save(
            session, company_id, "Acme", [_person(email="other@acme.com", verified=False)]
        )

    async with session_scope() as session:
        rows = await people_store.for_company(session, company_id)
        assert rows[0].email == "priya@acme.com"
        assert rows[0].email_verification == "verified"


async def test_a_thinner_page_does_not_erase_a_title(people_db):
    from careeragent.storage import session_scope

    company_id = await _company()

    async with session_scope() as session:
        await people_store.save(session, company_id, "Acme", [_person(title="VP Engineering")])
    async with session_scope() as session:
        await people_store.save(session, company_id, "Acme", [_person(title="")])

    async with session_scope() as session:
        rows = await people_store.for_company(session, company_id)
        assert rows[0].title == "VP Engineering"


async def test_relevance_is_stored_with_the_job_it_answered(people_db):
    """A number with no job attached is unattributable: the same manager is a 96 for an ML
    candidate and an 8 for someone applying to finance."""
    from careeragent.storage import session_scope

    company_id = await _company()
    subject = _person("Priya Sharma", "AI Engineering Manager", email="priya@acme.com")

    async with session_scope() as session:
        await people_store.save(session, company_id, "Acme", [subject])

    ranked = rank([subject], target_role="Machine Learning Engineer", company_size=500)
    async with session_scope() as session:
        written = await people_store.record_relevance(session, company_id, ranked, job_id="job-123")

    assert written == 1
    async with session_scope() as session:
        rows = await people_store.for_company(session, company_id)
        assert rows[0].relevance > 90
        assert rows[0].scored_for_job_id == "job-123"
        assert rows[0].relevance_reasons


async def test_filters_narrow_the_list(people_db):
    from careeragent.storage import session_scope

    company_id = await _company()
    async with session_scope() as session:
        await people_store.save(
            session,
            company_id,
            "Acme",
            [
                _person("A Sharma", "AI Engineering Manager", email="a@acme.com"),
                _person("B Rao", "Finance Manager", email="b@acme.com"),
                _person("C Iyer", "Data Scientist"),
            ],
        )

    async with session_scope() as session:
        ai_only = [r.name for r in await people_store.search(session, function="ai_ml")]
        with_email = await people_store.search(session, has_email=True)
        can_hire = [r.name for r in await people_store.search(session, hiring_authority=True)]
        with_email_count = len(with_email)

    assert ai_only == ["A Sharma"]
    assert with_email_count == 2
    assert "C Iyer" not in can_hire


async def test_contactable_only_means_published_and_verified(people_db):
    from careeragent.storage import session_scope

    company_id = await _company()
    async with session_scope() as session:
        await people_store.save(
            session,
            company_id,
            "Acme",
            [
                _person("A Sharma", "AI Engineering Manager", email="a@acme.com", verified=True),
                _person("B Rao", "AI Engineering Manager", email="b@acme.com", verified=False),
            ],
        )

    async with session_scope() as session:
        contactable = [r.name for r in await people_store.search(session, contactable_only=True)]

    assert contactable == ["A Sharma"]


async def test_a_stored_record_never_yields_a_contact_without_a_source(people_db):
    """A corrupt row degrades to "no contact" rather than breaking the page listing it."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import PersonRecord

    company_id = await _company()
    async with session_scope() as session:
        session.add(
            PersonRecord(
                company_id=company_id,
                name="Odd Row",
                normalized_name="odd row",
                email="nobody@acme.com",
                email_source=None,
            )
        )

    async with session_scope() as session:
        rows = await people_store.for_company(session, company_id)
        rebuilt = people_store.to_person(rows[0])
        assert rebuilt.email is None


async def test_every_source_page_is_kept(people_db):
    """Provenance is kept rather than summarised: a person seen on both the leadership and
    team pages carries both URLs."""
    from careeragent.storage import session_scope

    company_id = await _company()
    first = _person()
    second = _person()
    second.sources = [{"url": "https://acme.com/leadership", "kind": "leadership_page"}]

    async with session_scope() as session:
        await people_store.save(session, company_id, "Acme", [first])
    async with session_scope() as session:
        await people_store.save(session, company_id, "Acme", [second])

    async with session_scope() as session:
        rows = await people_store.for_company(session, company_id)
        urls = {source.url for source in rows[0].sources}

    assert urls == {TEAM_PAGE, "https://acme.com/leadership"}
