"""Database engine and session management.

Defaults to SQLite in the project's data directory so the agent runs with no
infrastructure at all. Point `CAREERAGENT_DATABASE_URL` at the Compose Postgres
(port 5433) to get pgvector-backed similarity search; nothing else changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import get_settings
from ..logging import get_logger
from .models import Base
from .safety import assert_destructive_allowed

log = get_logger("storage.db")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def resolve_url() -> str:
    """The active database URL.

    An explicit setting wins. Otherwise SQLite, because requiring Docker before
    the first run would make the tool useless to someone trying it out.
    """
    settings = get_settings()
    if configured := (settings.database_url or "").strip():
        return configured

    db_path = Path(settings.storage_dir).parent / "careeragent.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def is_postgres(url: str | None = None) -> bool:
    return (url or resolve_url()).startswith("postgresql")


# How long a connection waits for another writer before giving up. SQLite's default is
# zero — fail instantly — which is the wrong answer for a process that shares its database
# with a monitor on a timer and a CLI somebody runs by hand. Ten seconds is far longer than
# any write here takes and far shorter than anybody's patience.
SQLITE_BUSY_TIMEOUT_MS = 10_000


def _tune_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """Make SQLite behave like something two processes can share.

    Set per connection because that is the only place these pragmas live — they are
    connection state, not database state, so an engine that opens a new pooled connection
    gets the defaults again unless this runs every time.

    WAL is the one that matters. In the default rollback-journal mode a writer excludes
    every reader, so the company research pass — which holds a write open while it reads a
    company's website — made every concurrent API request fail with "database is locked".
    Under WAL, readers carry on against the last committed snapshot and only writer-versus
    -writer contention remains, which `busy_timeout` then turns into a brief wait.

    `journal_mode` is persistent once set and is a no-op on a file already in WAL; it is
    issued every time anyway because it costs nothing and because assuming otherwise is how
    a fresh database ends up in the old mode.
    """
    cursor = dbapi_connection.cursor()
    try:
        # FIRST, and the order is not cosmetic. Switching journal mode takes a brief
        # exclusive lock, so on a database another process is already using it fails —
        # and with the default timeout of zero it fails *instantly*. Setting the timeout
        # first is what lets the switch below wait its turn instead of throwing.
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")

        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception as exc:
            # Not fatal. A database still held exclusively by a long job stays in its
            # current mode and this connection carries on with a busy timeout, which is
            # worse than WAL and far better than refusing to start. Killing the API
            # because a background crawl is mid-write is the wrong failure.
            log.warning("sqlite_wal_unavailable", error=str(exc)[:160])

        # NORMAL rather than FULL: under WAL it is durable across application crashes and
        # only risks the last transactions in an OS-level crash, which is the right trade
        # for a job-search corpus that can be re-crawled.
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Referential integrity is not on by default in SQLite. Every ON DELETE CASCADE in
        # the schema is decorative without this.
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = resolve_url()
        kwargs: dict = {"echo": False, "future": True}
        if is_postgres(url):
            kwargs.update(pool_size=5, max_overflow=5, pool_pre_ping=True)
        _engine = create_async_engine(url, **kwargs)

        if not is_postgres(url):
            # `sync_engine` because the event fires on the underlying DBAPI connection,
            # which is what actually accepts a pragma.
            event.listen(_engine.sync_engine, "connect", _tune_sqlite)

        log.info(
            "database_engine_created",
            dialect="postgresql" if is_postgres(url) else "sqlite",
            shared_access="wal" if not is_postgres(url) else "native",
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A transactional session. Commits on success, rolls back on any exception."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db(drop: bool = False, *, force: bool = False) -> None:
    """Create the schema.

    Used directly for SQLite and for tests. Postgres deployments should go
    through Alembic instead, so that schema changes are reviewable and
    reversible rather than implicit.

    `drop=True` destroys every table, and it refuses to do that to a database that does not
    look disposable. This guard exists because it was needed: a test that touched storage
    was written without the fixture that redirects `CAREERAGENT_DATABASE_URL` at a temp
    file, so a routine test run dropped a real database holding 1,756 jobs, 67 researched
    companies and 81 boards. Nothing in the test's own code looked wrong — the fixture was
    simply absent, and absence is exactly what a reviewer does not see.

    `force=True` is for a caller that genuinely means it, such as a deliberate CLI reset.
    """
    url = resolve_url()
    if drop:
        assert_destructive_allowed(url, "drop every table", force=force)

    engine = get_engine()
    async with engine.begin() as conn:
        if is_postgres():
            from sqlalchemy import text

            # Safe to repeat; the Compose init script normally does this first.
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        if drop:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    log.info("database_ready", url_scheme=url.split(":")[0], dropped=drop)


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
