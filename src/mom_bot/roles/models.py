"""SQLAlchemy ORM model for the day-to-role mapping table (Epic 2.6).

Defines ``DayRoleMap``, a keyed table that maps (guild_id, day_number) to
a Discord role snowflake.  The seed/refresh routine in ``roles.seed`` keeps
this table current on every bot startup, logging a structured event whenever
a role is renamed or its snowflake changes.

Schema design rationale (issue #62):

- Composite primary key on (guild_id, day_number) — one row per attack day
  per guild.  No surrogate key is needed because this pair is already
  globally unique and stable.
- ``discord_role_id`` stores the Discord snowflake so the scheduler can
  mention the role without a live guild lookup on every fire.
- ``role_display_name`` caches the human-readable name for log messages
  and future display use.  It is updated independently of the snowflake so
  that cosmetic renames (same role, new name) produce a DEBUG log, not an
  INFO snowflake-change event.
- ``updated_at`` uses ``onupdate`` so SQLAlchemy bumps it automatically on
  any UPDATE — important for the noop branch, which must NOT touch this
  column when the row is already current.
"""

from __future__ import annotations

import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from mom_bot.db import Base

__all__ = ["DayRoleMap"]


class DayRoleMap(Base):
    """Maps a guild's attack-day number to its Discord role.

    Each row records which Discord role corresponds to a given RAID attack
    day for a specific guild.  The seed routine creates or refreshes rows
    on every bot startup.

    Attributes:
        guild_id: Discord guild snowflake (part of composite PK).
        day_number: RAID attack day number, 1-indexed (part of composite PK).
        discord_role_id: Discord role snowflake for this (guild, day) pair.
        role_display_name: Human-readable role name cached at seed time.
        updated_at: Wall-clock UTC timestamp of the last INSERT or UPDATE.
    """

    __tablename__ = "day_role_map"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, nullable=False)
    day_number: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    discord_role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role_display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
