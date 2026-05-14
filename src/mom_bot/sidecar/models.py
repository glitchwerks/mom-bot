"""SQLAlchemy ORM model for the role-sync idempotency table (Epic 2.6 B2).

``MemberRoleSyncState`` records the last-seen request key and response
payload for each ``discord_id`` that has passed through the
``POST /api/internal/role-sync`` endpoint.  The table is the mechanism for:

- **Idempotency (exact replay):** if the incoming ``(assigned_at, action,
  day_number)`` matches the stored key, the stored response is returned
  without re-invoking the role service.
- **Stale-write rejection:** if the incoming ``assigned_at`` is strictly
  older than ``last_assigned_at``, the request is rejected with
  ``status=skipped, reason=stale_write`` without invoking the role service.

Schema notes
------------
- ``discord_id`` is ``TEXT`` (opaque string per contract § 2 — never cast to
  integer).
- ``last_assigned_at`` is a raw ISO-8601 UTC string.  Lexicographic ordering
  of UTC ISO-8601 timestamps is monotonically correct, so string ``<`` / ``>``
  comparisons work for ordering without parsing.
- ``last_response_added`` and ``last_response_removed`` are JSON-encoded
  ``list[int]`` strings (Discord role snowflakes).
- ``last_day_number`` is ``NULL`` when the last action was ``unassign`` and
  the caller supplied no ``day_number`` field.
"""

from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from mom_bot.db import Base

__all__ = ["MemberRoleSyncState"]


class MemberRoleSyncState(Base):
    """Persists idempotency and stale-write state for the role-sync endpoint.

    One row per ``discord_id``.  Updated (UPSERT) on every fresh write;
    left untouched on exact replay and stale write.

    Attributes:
        discord_id: Discord snowflake string — primary key.
        last_assigned_at: ISO-8601 UTC timestamp of the last accepted
            request for this member.  Used for stale-write ordering.
        last_action: ``"assign"`` or ``"unassign"`` — part of the
            idempotency key.
        last_day_number: Attack-day number from the last accepted request,
            or ``None`` when the last action was ``unassign`` with no
            ``day_number`` provided.  Part of the idempotency key.
        last_correlation_id: ``correlation_id`` from the last accepted
            request.  Stored for operator tracing; not part of the key.
        last_response_status: ``"applied"``, ``"partial"``, ``"skipped"``,
            or ``"failed"`` — the status returned to the caller.
        last_response_added: JSON-encoded ``list[int]`` of role snowflakes
            added on the last accepted request.
        last_response_removed: JSON-encoded ``list[int]`` of role snowflakes
            removed on the last accepted request.
        last_response_reason: Optional reason code from the last accepted
            response, or ``None`` when status was ``"applied"``.
    """

    __tablename__ = "member_role_sync_state"

    discord_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    last_assigned_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_action: Mapped[str] = mapped_column(Text, nullable=False)
    last_day_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    last_response_status: Mapped[str] = mapped_column(Text, nullable=False)
    last_response_added: Mapped[str] = mapped_column(Text, nullable=False)
    last_response_removed: Mapped[str] = mapped_column(Text, nullable=False)
    last_response_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
