"""The database cannot be destroyed by accident, and can be recovered when it is.

Written after a test dropped a real database — 1,756 jobs, 67 researched companies, 81 ATS
boards. The tests below are the ones I would want to have existed beforehand.

The most important is `test_a_production_drop_is_rejected`: it reproduces the incident
exactly, against a database that looks like production, through the same function, and
asserts a refusal. If that test ever starts passing by doing the drop, the guard is gone.
"""

from __future__ import annotations

import sqlite3

import pytest

from careeragent.storage import backup
from careeragent.storage.safety import UnsafeDatabaseOperation, is_disposable

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def production_like(tmp_path, monkeypatch):
    """A database that looks exactly like the one that was lost.

    `tmp_path` keeps the file harmless, but `is_disposable` is patched to answer False so
    the guard sees what it would see in production. Testing a safety net against a
    genuinely production database is not an option, and faking the *signal* rather than
    the *location* is the honest way to test it.
    """
    from careeragent import config as config_module
    from careeragent import storage

    path = tmp_path / "careeragent.db"
    monkeypatch.setenv("CAREERAGENT_ENV", "production")
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(storage.db, "assert_destructive_allowed", _strict_guard(path))
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    yield path
    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]


def _strict_guard(path):
    """The real guard, but with this one tmp file treated as non-disposable."""
    from careeragent.storage import safety

    def guard(url: str, operation: str, *, force: bool = False) -> None:
        real_is_disposable = safety.is_disposable
        try:
            safety.is_disposable = lambda u: False if str(path) in u else real_is_disposable(u)
            safety.assert_destructive_allowed(url, operation, force=force)
        finally:
            safety.is_disposable = real_is_disposable

    return guard


# ─── The incident, reproduced ───────────────────────────────────────────────


async def test_a_production_drop_is_rejected(production_like):
    """The exact shape of the accident: `init_db(drop=True)` against a real database.

    If this test ever passes by performing the drop rather than refusing it, the guard has
    been removed and 1,756 rows are one `pytest` away from gone again.
    """
    from careeragent import storage

    await storage.init_db()  # create the schema first, as a real database would have one

    with sqlite3.connect(str(production_like)) as connection:
        before = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    assert before > 0, "the fixture must actually have tables to lose"

    with pytest.raises(UnsafeDatabaseOperation):
        await storage.init_db(drop=True)

    with sqlite3.connect(str(production_like)) as connection:
        after = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    assert after == before, "the tables must still be there"


async def test_a_forced_production_drop_is_still_rejected(production_like):
    """`force` is for a caller that asked a human. It is not an override for production —
    a prompt answered with a stale CAREERAGENT_ENV is not informed consent."""
    from careeragent import storage

    await storage.init_db()

    with pytest.raises(UnsafeDatabaseOperation):
        await storage.init_db(drop=True, force=True)


async def test_data_survives_a_refused_drop(production_like):
    """Not just the schema — the rows."""
    from careeragent import storage
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    await storage.init_db()
    async with session_scope() as session:
        await repo.upsert_company(session, "Acme Analytics", "acme.example")

    with pytest.raises(UnsafeDatabaseOperation):
        await storage.init_db(drop=True)

    async with session_scope() as session:
        from sqlalchemy import func, select

        from careeragent.storage.models import Company

        assert (await session.execute(select(func.count()).select_from(Company))).scalar_one() == 1


# ─── Snapshots ──────────────────────────────────────────────────────────────


@pytest.fixture
def live_db(tmp_path, monkeypatch):
    from careeragent import config as config_module
    from careeragent import storage

    path = tmp_path / "test_corpus.db"
    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    yield path
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]


async def _seed(path, names):
    from careeragent import storage
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    await storage.init_db()
    async with session_scope() as session:
        for name in names:
            await repo.upsert_company(session, name, f"{name.lower()}.example")


async def _company_names(url):
    from sqlalchemy import select

    from careeragent.storage import session_scope
    from careeragent.storage.models import Company

    async with session_scope() as session:
        return sorted(row[0] for row in (await session.execute(select(Company.name))).all())


async def test_a_snapshot_captures_the_data(live_db):
    url = f"sqlite+aiosqlite:///{live_db}"
    await _seed(live_db, ["Acme", "Beta"])

    snap = backup.snapshot(url, label="after seeding")

    assert snap.path.exists()
    assert snap.bytes > 0
    assert snap.label == "after seeding"
    with sqlite3.connect(str(snap.path)) as connection:
        assert connection.execute("SELECT count(*) FROM company").fetchone()[0] == 2


async def test_a_restore_brings_the_data_back(live_db):
    """The reason this module exists."""
    from sqlalchemy import delete

    from careeragent import storage
    from careeragent.storage import session_scope
    from careeragent.storage.models import Company

    url = f"sqlite+aiosqlite:///{live_db}"
    await _seed(live_db, ["Acme", "Beta", "Gamma"])
    snap = backup.snapshot(url, label="good state")

    async with session_scope() as session:
        await session.execute(delete(Company))
    assert await _company_names(url) == []

    await storage.dispose()
    backup.restore(url, snap.name, force=True)
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]

    assert await _company_names(url) == ["Acme", "Beta", "Gamma"]


async def test_a_restore_snapshots_what_it_is_about_to_replace(live_db):
    """Restoring the wrong backup is a mistake somebody makes exactly once, and then
    wants the previous state back."""
    from careeragent import storage

    url = f"sqlite+aiosqlite:///{live_db}"
    await _seed(live_db, ["Acme"])
    first = backup.snapshot(url, label="one company")

    await _seed(live_db, ["Beta", "Gamma"])
    await storage.dispose()

    backup.restore(url, first.name, force=True)

    labels = [s.label for s in backup.list_snapshots(url)]
    assert "before restore" in labels


def test_restoring_an_unknown_snapshot_lists_what_exists(live_db):
    url = f"sqlite+aiosqlite:///{live_db}"

    with pytest.raises(backup.BackupError, match="no snapshot named"):
        backup.restore(url, "not-a-snapshot", force=True)


async def test_a_restore_is_guarded_like_any_destructive_operation(live_db, monkeypatch):
    url = f"sqlite+aiosqlite:///{live_db}"
    await _seed(live_db, ["Acme"])
    snap = backup.snapshot(url)

    monkeypatch.setenv("CAREERAGENT_ENV", "production")
    try:
        with pytest.raises(UnsafeDatabaseOperation):
            backup.restore(url, snap.name)
    finally:
        monkeypatch.setenv("CAREERAGENT_ENV", "test")


async def test_old_snapshots_are_pruned(live_db):
    url = f"sqlite+aiosqlite:///{live_db}"
    await _seed(live_db, ["Acme"])

    for n in range(backup.KEEP + 4):
        backup.snapshot(url, label=f"n{n}")

    assert len(backup.list_snapshots(url)) <= backup.KEEP


def test_postgres_is_directed_at_the_right_tool():
    """`pg_dump` is correct and not worth reimplementing badly."""
    with pytest.raises(backup.BackupError, match="pg_dump"):
        backup.snapshot("postgresql+asyncpg://u:p@localhost/careeragent")


async def test_health_reports_integrity_and_protection(live_db):
    url = f"sqlite+aiosqlite:///{live_db}"
    await _seed(live_db, ["Acme"])

    before = backup.health(url)
    assert before["integrity"] == "ok"
    assert before["tables"] > 0
    assert before["snapshots"] == 0
    assert "No snapshot yet" in before["note"]

    backup.snapshot(url)
    after = backup.health(url)
    assert after["snapshots"] == 1
    assert after["latest_snapshot"] is not None


def test_health_of_a_database_that_is_not_there():
    health = backup.health("sqlite+aiosqlite:///nowhere/at/all.db")
    assert health["exists"] is False
    assert health["tables"] == 0


# ─── The repository-wide sweep ──────────────────────────────────────────────


def test_no_test_file_drops_tables_without_isolating_first():
    """The incident in one assertion.

    Every test that drops a schema must first point `CAREERAGENT_DATABASE_URL` somewhere
    disposable — either through a fixture in its own file or through the suite-wide
    default in conftest. A file that drops without either is the exact hole that was open.
    """
    from pathlib import Path

    tests_dir = Path(__file__).parent
    offenders = []
    for file in tests_dir.glob("test_*.py"):
        text = file.read_text(encoding="utf-8")
        if "drop=True" not in text:
            continue
        if "CAREERAGENT_DATABASE_URL" in text or "isolated_db" in text:
            continue
        offenders.append(file.name)

    assert not offenders, (
        f"{offenders} drop schemas without redirecting the database URL. "
        "Use the `isolated_db` fixture from conftest, or set the URL with monkeypatch."
    )


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///data/careeragent.db",
        "postgresql+asyncpg://u:p@prod.internal/careeragent",
    ],
)
def test_the_real_databases_are_never_disposable(url):
    assert not is_disposable(url)
