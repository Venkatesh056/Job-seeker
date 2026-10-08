"""Schema migration, including the awkward case of a database that predates it.

Three situations, and this module tells them apart rather than assuming one:

  1. **Empty database.** Run Alembic from the baseline. Normal path.
  2. **Alembic-managed database.** Run Alembic to head. Normal path.
  3. **Tables but no `alembic_version`.** A database created by `create_all` before
     migrations existed. This is the case that needs care, and it is handled by
     *adopting* it: create the tables it is missing, add the columns it is missing,
     then stamp it at head so every later change goes through Alembic normally.

Adoption only ever **adds**. It creates missing tables and appends missing columns;
it never drops a column, never changes a type, and never touches data. Anything
beyond that is reported for a human to resolve, because a migration tool that
silently reshapes an existing column is a migration tool that loses data.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateColumn

from ..logging import get_logger
from .db import get_engine, is_postgres, resolve_url
from .models import Base

log = get_logger("storage.migrate")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


async def _run_alembic(action: str, target: str = "head") -> None:
    """Run an Alembic command off the event loop.

    `env.py` calls `asyncio.run` to drive its async engine, which raises if a loop is
    already running — and every caller here is async. A worker thread gives Alembic
    the clean loop it expects without turning the whole migration path synchronous.
    """
    from alembic import command

    config = _alembic_config()
    runner = {"upgrade": command.upgrade, "downgrade": command.downgrade, "stamp": command.stamp}[
        action
    ]
    await asyncio.to_thread(runner, config, target)


def _alembic_config() -> Any:
    from alembic.config import Config

    if not ALEMBIC_INI.exists():
        raise FileNotFoundError(f"alembic.ini not found at {ALEMBIC_INI}")
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(PROJECT_ROOT / "backend" / "migrations"))
    config.set_main_option("sqlalchemy.url", resolve_url())
    return config


async def inspect_state() -> dict[str, Any]:
    """What kind of database is this? Reported before anything is changed."""
    engine = get_engine()

    def read(connection) -> dict[str, Any]:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        expected = set(Base.metadata.tables)
        return {
            "tables": sorted(tables),
            "missing_tables": sorted(expected - tables),
            "unknown_tables": sorted(tables - expected - {"alembic_version"}),
            "alembic_managed": "alembic_version" in tables,
            "empty": not (tables - {"alembic_version"}),
        }

    async with engine.connect() as connection:
        state = await connection.run_sync(read)

    state["dialect"] = "postgresql" if is_postgres() else "sqlite"
    if state["alembic_managed"]:
        async with engine.connect() as connection:
            row = await connection.execute(text("SELECT version_num FROM alembic_version"))
            state["version"] = row.scalar()
    return state


def _missing_columns(connection) -> list[tuple[str, Any]]:
    """Columns present in the models and absent from the database."""
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    out: list[tuple[str, Any]] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in present:
                out.append((table.name, column))
    return out


def _addable(column: Any) -> bool:
    """Can this column be appended to an existing table without inventing data?

    A NOT NULL column with no default cannot: every existing row would need a value
    we do not have. Those are reported rather than forced.
    """
    return column.nullable or column.default is not None or column.server_default is not None


async def adopt() -> dict[str, Any]:
    """Bring a `create_all`-era database under Alembic, adding only what is missing."""
    engine = get_engine()
    created: list[str] = []
    added: list[str] = []
    unresolvable: list[str] = []

    state = await inspect_state()
    if state["missing_tables"]:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        created = state["missing_tables"]

    def add_columns(connection) -> None:
        compiler = connection.dialect.ddl_compiler(connection.dialect, None)
        for table_name, column in _missing_columns(connection):
            if not _addable(column):
                unresolvable.append(
                    f"{table_name}.{column.name} is NOT NULL with no default; "
                    "existing rows have no value for it"
                )
                continue
            spec = compiler.process(CreateColumn(column))
            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {spec}"))
            added.append(f"{table_name}.{column.name}")

    async with engine.begin() as connection:
        await connection.run_sync(add_columns)

    # Backfill the JSON and integer defaults the ORM would have written. SQLite's
    # ADD COLUMN leaves existing rows NULL even when the model declares a default,
    # and code reading `application.preview` expects {} rather than None.
    async with engine.begin() as connection:
        for qualified in added:
            table_name, column_name = qualified.split(".", 1)
            column = Base.metadata.tables[table_name].columns[column_name]
            default = getattr(column.default, "arg", None)
            if default is None:
                continue
            value: Any
            if callable(default):
                try:
                    value = default(None)
                except TypeError:
                    continue
            else:
                value = default
            if isinstance(value, (dict, list)):
                literal = "'{}'" if isinstance(value, dict) else "'[]'"
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                literal = str(value)
            elif isinstance(value, str):
                literal = f"'{value}'"
            else:
                continue
            await connection.execute(
                text(
                    f"UPDATE {table_name} SET {column_name} = {literal} WHERE {column_name} IS NULL"
                )
            )

    await _run_alembic("stamp", "head")

    log.info(
        "database_adopted",
        tables_created=len(created),
        columns_added=len(added),
        unresolvable=len(unresolvable),
    )
    return {
        "action": "adopted",
        "tables_created": created,
        "columns_added": added,
        "unresolvable": unresolvable,
        "note": (
            "Existing database brought under Alembic. Only additions were made; "
            "nothing was dropped or retyped."
        ),
    }


async def upgrade() -> dict[str, Any]:
    """Run migrations to head, choosing the right path for this database.

    Safe to run repeatedly. This is what `careeragent migrate` calls, and what a
    deployment should run before starting the API.
    """
    state = await inspect_state()

    if state["empty"] or (not state["alembic_managed"] and not state["tables"]):
        await _run_alembic("upgrade", "head")
        log.info("database_migrated", path="fresh")
        return {"action": "created", "note": "schema created from the baseline migration"}

    if not state["alembic_managed"]:
        return await adopt()

    before = state.get("version")
    await _run_alembic("upgrade", "head")
    after = (await inspect_state()).get("version")
    log.info("database_migrated", path="upgrade", before=before, after=after)
    return {
        "action": "upgraded" if before != after else "already_current",
        "from": before,
        "to": after,
    }


async def current_revision() -> str | None:
    state = await inspect_state()
    return state.get("version")


def history() -> list[dict[str, str]]:
    """The migration chain, newest first."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config())
    return [
        {
            "revision": revision.revision,
            "down_revision": str(revision.down_revision or ""),
            "message": revision.doc or "",
        }
        for revision in script.walk_revisions()
    ]
