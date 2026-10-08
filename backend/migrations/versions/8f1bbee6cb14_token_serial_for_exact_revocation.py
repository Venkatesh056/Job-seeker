"""token serial for exact revocation

`app_user.tokens_valid_from` alone could not revoke reliably. A JWT's `iat` is whole
seconds, so a token issued in the *same second* as a logout compared equal to the cutoff
and survived it — somebody logging out immediately after logging in would have kept their
session, and on a slow machine the bug would never show.

A serial has no clock in it. Every token this system issues carries the serial it was minted
under; revoking increments the column; a token whose serial is stale is refused. The
timestamp stays for tokens minted elsewhere (a Supabase session carries no serial of ours)
and because "when were sessions last revoked" is worth being able to answer.

Existing rows start at 0, which matches the model default, so tokens already in the wild
keep working — there are none, but a migration that silently logged everybody out would be
the wrong default for one that ships later.

Revision ID: 8f1bbee6cb14
Revises: a736476e86c2
Created: 2026-09-15 14:56:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

# Always imported: autogenerate renders this project's custom column types by
# their qualified name without importing them, and a migration that raises
# NameError only does so on a fresh database.
import careeragent.storage.types  # noqa: F401
from alembic import op

revision: str = "8f1bbee6cb14"
down_revision: str | None = "a736476e86c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("app_user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("token_serial", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("app_user", schema=None) as batch_op:
        batch_op.drop_column("token_serial")
