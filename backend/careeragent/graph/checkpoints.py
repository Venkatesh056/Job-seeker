"""Durable checkpointing — spec §45.

`InMemorySaver` survived an approval that took hours. It did not survive a restart, and
a restart is the ordinary case: you close the laptop, `uvicorn --reload` picks up a file
change, the machine sleeps. Every parked approval vanished, and with it the tailored
resume, the generated answers and the knowledge of which step the run had reached.

The applications themselves were never lost — they sit in `AWAITING_APPROVAL` in the
database, and the non-graph approval path picks them up. But the *graph* had to start
over, which means re-tailoring and re-opening the form. That is the difference between
"resume from checkpoint" and "restart from scratch" the spec asks about.

Backend follows the application database, so there is one thing to back up and one thing
to point at a new machine:

    sqlite://       → AsyncSqliteSaver, in a file beside the application database
    postgresql://   → AsyncPostgresSaver, same cluster
    neither works   → InMemorySaver, loudly

The fallback is deliberate and noisy. A checkpointer that cannot open its store must not
stop the agent from working — but it must not pretend to be durable either, so it logs a
warning and `describe()` reports what actually happened.

Saver lifetime is the awkward part of this API: both async savers are context managers
that own a connection pool, so they cannot simply be constructed and returned. They are
opened once at application startup and closed at shutdown, which is why this module
holds module-level state rather than building one per call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import get_settings
from ..logging import get_logger
from ..storage.db import is_postgres, resolve_url

log = get_logger("graph.checkpoints")

_saver: Any = None
_stack: Any = None
_backend: str = "none"
_error: str = ""


def checkpoint_path() -> Path:
    """Where the SQLite checkpoint store lives.

    Beside the application database rather than inside it: LangGraph owns this schema
    and migrates it on its own, and mixing it into the Alembic-managed database would
    mean two tools writing the same file's schema.
    """
    settings = get_settings()
    path = Path(settings.storage_dir).parent / "checkpoints.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def open_checkpointer() -> Any:
    """Open the durable saver for this process. Idempotent.

    Called from the API lifespan and from the CLI before a graph runs.
    """
    global _saver, _stack, _backend, _error
    if _saver is not None:
        return _saver

    from contextlib import AsyncExitStack

    _stack = AsyncExitStack()
    url = resolve_url()

    try:
        if is_postgres(url):
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            # LangGraph wants a libpq URL; SQLAlchemy's driver suffix is not one.
            dsn = url.replace("postgresql+psycopg://", "postgresql://").replace(
                "postgresql+asyncpg://", "postgresql://"
            )
            _saver = await _stack.enter_async_context(AsyncPostgresSaver.from_conn_string(dsn))
            await _saver.setup()
            _backend = "postgres"
        else:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            _saver = await _stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(checkpoint_path()))
            )
            await _saver.setup()
            _backend = "sqlite"

        log.info("checkpointer_open", backend=_backend)
        return _saver

    except Exception as exc:
        # Durability is a property worth losing loudly rather than a reason to refuse
        # to run. The agent still works; parked approvals just will not survive a
        # restart, and `describe()` says so.
        _error = f"{type(exc).__name__}: {exc}"[:200]
        log.warning("checkpointer_fallback_in_memory", error=_error)
        from langgraph.checkpoint.memory import InMemorySaver

        _saver = InMemorySaver()
        _backend = "memory"
        return _saver


async def close_checkpointer() -> None:
    global _saver, _stack, _backend
    if _stack is not None:
        try:
            await _stack.aclose()
        except Exception as exc:
            log.debug("checkpointer_close_failed", error=str(exc)[:120])
    _saver = None
    _stack = None
    _backend = "none"


def current() -> Any:
    """The open saver, or None if `open_checkpointer` has not run.

    Returning None rather than silently opening one keeps the lifetime explicit: a saver
    opened on demand inside a request would be closed with that request's task group.
    """
    return _saver


def describe() -> dict[str, Any]:
    """What durability this process actually has. Reported by /api/v1/status."""
    return {
        "backend": _backend,
        "durable": _backend in {"sqlite", "postgres"},
        "path": str(checkpoint_path()) if _backend == "sqlite" else None,
        "error": _error,
        "note": (
            "Parked approvals survive a restart."
            if _backend in {"sqlite", "postgres"}
            else "In-memory only — a restart loses parked approvals. "
            "The applications themselves are safe in the database."
        ),
    }
