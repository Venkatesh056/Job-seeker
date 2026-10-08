"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

# Always imported: autogenerate renders this project's custom column types by
# their qualified name without importing them, and a migration that raises
# NameError only does so on a fresh database.
import careeragent.storage.types  # noqa: F401
from alembic import op
${imports if imports else ""}
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
