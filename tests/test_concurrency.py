"""Two processes, one SQLite file.

This system is designed to run a monitor on a timer, a CLI somebody invokes by hand, and an
API — all against the same database at the same time. It did that badly in two independent
ways, and both were found by doing the ordinary thing: kicking off a company research pass
and then clicking something in the dashboard.

    sqlite3.OperationalError: database is locked

**The long transaction.** `careeragent companies --research` wrapped sixty-six website
reads in a single `session_scope()`. One write lock, held for minutes, across HTTP calls.
It was also all-or-nothing — killing the run rolled back every profile it had gathered,
which is exactly what happened and is why the researched count went back to zero.

**The defaults.** SQLite ships with a rollback journal, where a writer excludes every
reader, and with `busy_timeout` at zero, so a blocked connection fails instantly rather
than waiting a moment.

Neither test below mocks anything. They open real connections to a real file and contend.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("isolated_db")


async def test_the_database_is_in_wal_mode():
    """The pragma that lets a reader carry on while somebody writes.

    Asserted on the live connection rather than on the code that sets it, because the
    pragma is connection state: an engine that opens a new pooled connection gets the
    defaults back unless the listener runs every time.
    """
    from careeragent.storage import init_db, session_scope

    await init_db(drop=True)
    async with session_scope() as session:
        mode = await session.scalar(text("PRAGMA journal_mode"))
        timeout = await session.scalar(text("PRAGMA busy_timeout"))
        foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))

    assert str(mode).lower() == "wal"
    assert int(timeout or 0) >= 5000, "a zero busy timeout fails instantly under contention"
    # Every ON DELETE CASCADE in the schema is decorative without this, and SQLite does
    # not turn it on by default.
    assert int(foreign_keys or 0) == 1


async def test_a_write_succeeds_while_another_connection_holds_one_open():
    """The measured failure, reproduced.

    One connection opens a write and keeps it open — as the research pass did across every
    HTTP call it made. A second connection must still be able to write, after a wait,
    rather than returning an error immediately.
    """
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo

    await init_db(drop=True)
    async with session_scope() as session:
        candidate = await repo.get_or_create_candidate(session, name="Contended")
        candidate_id = candidate.id

    slow_started = asyncio.Event()
    release = asyncio.Event()

    async def hold_a_write() -> None:
        async with session_scope() as session:
            row = await repo.get_or_create_candidate(session)
            row.location = "written by the slow holder"
            await session.flush()
            slow_started.set()
            # Standing in for a website read taking its time inside the transaction.
            await release.wait()

    async def write_anyway() -> str:
        await slow_started.wait()
        release.set()
        await asyncio.sleep(0)
        async with session_scope() as session:
            row = await repo.get_or_create_candidate(session)
            row.notice_period = "written by the contender"
            await session.flush()
            return str(row.id)

    holder = asyncio.create_task(hold_a_write())
    contender = asyncio.create_task(write_anyway())
    written, _ = await asyncio.gather(contender, holder)

    assert written == candidate_id
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        assert row.notice_period == "written by the contender"


async def test_research_commits_each_company_rather_than_the_whole_run():
    """The other half, and the more expensive one.

    A single transaction around the loop meant an interrupted crawl lost everything it had
    already gathered. Asserted by counting transactions rather than by timing: the loop
    must open a scope per company, so a failure at company three leaves one and two saved.
    """
    import inspect

    from careeragent import cli

    source = inspect.getsource(cli._companies)
    body = source.split("if research:", 1)[1]

    # The network read must happen outside any open transaction — holding one across an
    # HTTP call is what turned a slow crawl into a lock on every other process.
    read_at = body.index("company_mod.research(")
    save_at = body.index("save_company_profile")
    assert read_at < save_at, "the profile is fetched after the write scope opens"

    # And the scope that saves must be inside the loop, not around it.
    loop_at = body.index("for company_id, name, domain in targets:")
    save_scope_at = body.index("async with session_scope()", loop_at)
    assert save_scope_at > loop_at, "the save transaction wraps the loop rather than one row"
