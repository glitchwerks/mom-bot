"""Widen reminder snowflake columns from INTEGER to BIGINT.

Discord snowflakes are 64-bit unsigned integers.  The original schema used
``sa.Integer`` (32-bit) for ``reminders.channel_id`` and
``reminders.role_mention_id``, which silently worked under SQLite (dynamic
integer width) but raises ``psycopg.errors.NumericValueOutOfRange`` on
PostgreSQL when a real snowflake value (e.g. 1385263344684109955) is
inserted.

This migration widens both columns to ``BIGINT`` on PostgreSQL.  The
``ALTER COLUMN`` DDL is a no-op on SQLite because SQLite stores all integers
as dynamically-sized 64-bit values — there is no 32-bit ``INTEGER`` type at
the storage layer and no ``ALTER COLUMN ... TYPE`` syntax.  We therefore
branch on the active dialect at migration runtime (same pattern as the
``fire_time_utc`` CHECK in 0002_reminders_schema).

The table is empty in prod at migration time so no ``USING`` cast expression
is required — there are no existing rows to coerce.

Revision ID: 0003_widen_reminder_snowflakes
Revises: b2_member_role_sync_state
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_widen_reminder_snowflakes"
down_revision: str | Sequence[str] | None = "b2_member_role_sync_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    """Return True when the active dialect is PostgreSQL.

    SQLite has no ``ALTER COLUMN ... TYPE`` syntax and stores all integers
    as 64-bit values natively, so the widen is a true no-op there.

    Returns:
        ``True`` if the bound engine is PostgreSQL, ``False`` otherwise.
    """
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Widen ``channel_id`` and ``role_mention_id`` to BIGINT on Postgres."""
    if not _is_postgres():
        return

    op.alter_column(
        "reminders",
        "channel_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )
    op.alter_column(
        "reminders",
        "role_mention_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Revert ``channel_id`` and ``role_mention_id`` to INTEGER on Postgres."""
    if not _is_postgres():
        return

    op.alter_column(
        "reminders",
        "channel_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "reminders",
        "role_mention_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
