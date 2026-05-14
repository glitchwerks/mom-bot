"""day_role_map table

Maps (guild_id, day_number) to a Discord role snowflake for Epic 2.6.
The seed/refresh routine in ``mom_bot.roles.seed`` keeps this table
current on every bot startup.

Revision ID: a26d62
Revises: 0002_reminders_schema
Create Date: 2026-05-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a26d62"
down_revision: str | Sequence[str] | None = "0002_reminders_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``day_role_map`` table."""
    op.create_table(
        "day_role_map",
        sa.Column("guild_id", sa.BigInteger, primary_key=True, nullable=False),
        sa.Column("day_number", sa.Integer, primary_key=True, nullable=False),
        sa.Column("discord_role_id", sa.BigInteger, nullable=False),
        sa.Column("role_display_name", sa.String(100), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    """Drop the ``day_role_map`` table."""
    op.drop_table("day_role_map")
