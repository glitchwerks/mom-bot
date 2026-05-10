"""reminders schema

Creates the ``reminders`` and ``reminder_sent`` tables for the Epic 1
Discord reminder scheduler (plan §§ 4).

DDL-only migration — no seed data. Seed rows are inserted at runtime by
the bot on first boot from Key Vault values (see plan § 4 Seed-on-boot).

Constraints included:

- ``reminders.fire_time_utc`` CHECK: seconds component must be zero
  (SQLite-compatible predicate using ``strftime``).
- ``reminders.weekday`` CHECK: value must be in the range 0-6 (Mon-Sun).
- ``reminder_sent`` UNIQUE (reminder_id, fire_date_utc): at most one sent
  record per reminder per UTC calendar day.
- ``reminder_sent.reminder_id`` FK → ``reminders.id`` ON DELETE CASCADE:
  deleting a reminder cascades to its send history.

Revision ID: 0002_reminders_schema
Revises: 2f03efc88bf2
Create Date: 2026-05-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_reminders_schema"
down_revision: str | Sequence[str] | None = "2f03efc88bf2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``reminders`` and ``reminder_sent`` tables."""
    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("channel_id", sa.Integer, nullable=False),
        sa.Column("weekday", sa.Integer, nullable=False),
        sa.Column("fire_time_utc", sa.Time, nullable=False),
        sa.Column("message_template", sa.Text, nullable=False),
        sa.Column("role_mention_id", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("name", name="uq_reminders_name"),
        sa.CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_weekday"),
        sa.CheckConstraint(
            "CAST(strftime('%S', fire_time_utc) AS INTEGER) = 0",
            name="ck_fire_time_no_seconds",
        ),
    )

    op.create_table(
        "reminder_sent",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "reminder_id",
            sa.Integer,
            sa.ForeignKey("reminders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fire_date_utc", sa.Date, nullable=False),
        sa.Column(
            "sent_at",
            sa.TIMESTAMP,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "reminder_id",
            "fire_date_utc",
            name="uq_reminder_sent_per_day",
        ),
    )


def downgrade() -> None:
    """Drop ``reminder_sent`` and ``reminders`` tables."""
    # Drop reminder_sent first because it holds the FK reference.
    op.drop_table("reminder_sent")
    op.drop_table("reminders")
