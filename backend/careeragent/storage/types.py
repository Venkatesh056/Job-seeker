"""Column types that adapt to the active database.

The agent must run before any infrastructure exists — SQLite, no Docker — and
still use pgvector's indexed similarity search when Postgres is available.
Rather than maintaining two schemas, the handful of columns that actually differ
adapt themselves here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect


class UTCDateTime(TypeDecorator):
    """A timezone-aware timestamp that stays aware on SQLite.

    SQLite has no timestamp type and stores datetimes as text, dropping the
    offset — so a value written as UTC-aware reads back naive. Comparing that
    with a fresh `datetime.now(UTC)` raises
    "can't compare offset-naive and offset-aware datetimes", which only happens
    on the UPDATE path and so survives any test that just inserts.

    Attaching UTC on load fixes it once, here, rather than at every comparison.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        # Naive input is assumed UTC: everything in this system writes UTC.
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)


class JSONType(TypeDecorator):
    """JSONB on Postgres (indexable, binary), plain JSON on SQLite."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class EmbeddingType(TypeDecorator):
    """`vector(n)` on Postgres, a JSON array of floats on SQLite.

    On SQLite the similarity search falls back to an in-Python cosine scan. That
    is fine at personal scale — a few thousand jobs — and it means semantic
    matching works on day one instead of waiting for infrastructure.
    """

    impl = Text
    cache_ok = True

    def __init__(self, dim: int = 384, **kwargs: Any) -> None:
        self.dim = dim
        super().__init__(**kwargs)

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return json.dumps([float(x) for x in value])

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return list(value)
        return json.loads(value)
