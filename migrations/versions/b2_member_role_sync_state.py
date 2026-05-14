"""member_role_sync_state table

Persists idempotency and stale-write state for the
``POST /api/internal/role-sync`` sidecar endpoint (Epic 2.6 B2).

Each row records the last-seen request key and response payload for a given
``discord_id``.  This table survives restarts and has no eviction policy —
its maximum cardinality is the number of distinct guild members ever synced
(bounded by guild size).

Schema design notes
-------------------
- ``discord_id`` is stored as ``TEXT`` (not ``BigInteger``) because the
  contract spec (§ 2) treats Discord snowflakes as opaque strings.  Receivers
  MUST NOT cast to integer.
- ``last_assigned_at`` is an ISO-8601 UTC string.  Lexicographic comparison on
  UTC ISO-8601 strings is monotonically correct, so ``<`` / ``>`` comparisons
  work without parsing.
- ``last_response_added`` and ``last_response_removed`` are JSON-encoded
  ``list[int]`` (Discord role snowflakes).
- ``last_day_number`` is ``NULL`` when the last action was ``unassign`` and
  no day_number was provided by the caller.

Revision ID: b2_member_role_sync_state
Revises: a26d62
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2_member_role_sync_state"
down_revision: str | Sequence[str] | None = "a26d62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``member_role_sync_state`` table."""
    op.create_table(
        "member_role_sync_state",
        sa.Column("discord_id", sa.Text, primary_key=True, nullable=False),
        sa.Column("last_assigned_at", sa.Text, nullable=False),
        sa.Column("last_action", sa.Text, nullable=False),
        sa.Column("last_day_number", sa.Integer, nullable=True),
        sa.Column("last_correlation_id", sa.Text, nullable=False),
        sa.Column("last_response_status", sa.Text, nullable=False),
        sa.Column("last_response_added", sa.Text, nullable=False),
        sa.Column("last_response_removed", sa.Text, nullable=False),
        sa.Column("last_response_reason", sa.Text, nullable=True),
    )


def downgrade() -> None:
    """Drop the ``member_role_sync_state`` table."""
    op.drop_table("member_role_sync_state")
