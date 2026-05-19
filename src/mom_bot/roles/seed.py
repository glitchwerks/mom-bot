"""Seed and refresh the ``day_role_map`` table on bot startup (Epic 2.6).

Called from :meth:`~mom_bot.main.MomBot.on_ready` once the gateway is live.
Iterates the bot's guilds, finds Discord roles named
``"Siege - Day {N} Attacker"`` for days 1 and 2, and upserts them into the
``day_role_map`` table with structured logging of any rename or
snowflake-change events.

Idempotency guarantee: calling this function twice in a row with no
intervening Discord changes produces zero database writes on the second call.
The ``updated_at`` column is only bumped by SQLAlchemy on an actual UPDATE
statement, so its timestamp serves as a cheap write-audit signal in tests.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.roles.models import DayRoleMap

if TYPE_CHECKING:
    import discord

__all__ = ["DAY_ROLE_NAME_TEMPLATE", "seed_day_role_map"]

_logger = logging.getLogger(__name__)

# Attack days the seed routine manages.  Hardcoded per issue #62 — only
# days 1 and 2 exist in the current RAID cadence.
_ATTACK_DAYS = (1, 2)

# Discord role name pattern used to locate siege-day roles on each guild.
# Live logs from ca-mom-bot--0000010 (issue #127) confirmed both prod guilds
# use this naming convention rather than the previously hardcoded
# "Attack Day {day}" pattern.
DAY_ROLE_NAME_TEMPLATE = "Siege - Day {day} Attacker"


async def seed_day_role_map(
    client: discord.Client,
    session_factory: sessionmaker[Session],
) -> None:
    """Seed or refresh ``day_role_map`` for every guild the bot is in.

    For each guild visible to *client* and each attack day in
    ``_ATTACK_DAYS``:

    - If a role matching ``DAY_ROLE_NAME_TEMPLATE`` (e.g.
      ``"Siege - Day {day} Attacker"``) is not found on the guild, log
      a WARNING with the sentinel ``DAY_ROLE_NOT_FOUND`` and skip to the
      next day.
    - If no row exists in ``day_role_map``, insert one and log INFO with
      ``DAY_ROLE_SEEDED``.
    - If the row's ``discord_role_id`` differs from the live role's ID
      (snowflake change — role was deleted and recreated), update both
      fields and log INFO with ``DAY_ROLE_SNOWFLAKE_CHANGED``.
    - If the snowflake matches but ``role_display_name`` differs (cosmetic
      rename), update only ``role_display_name`` and log DEBUG.
    - If both the snowflake and the display name match, do nothing.

    This function is async because it will be called from an ``on_ready``
    coroutine; the database operations themselves are synchronous SQLAlchemy.

    Args:
        client: Connected :class:`discord.Client` whose ``.guilds``
            attribute is fully populated (i.e. called after ``on_ready``).
        session_factory: Bound :class:`~sqlalchemy.orm.sessionmaker` used
            to open database sessions.
    """
    for guild in client.guilds:
        for day in _ATTACK_DAYS:
            role_name = DAY_ROLE_NAME_TEMPLATE.format(day=day)

            # Locate the role on this guild by name.
            role = next(
                (r for r in guild.roles if r.name == role_name),
                None,
            )
            if role is None:
                available = sorted(r.name for r in guild.roles)
                _logger.warning(
                    "DAY_ROLE_NOT_FOUND guild_id=%s day=%s" " expected=%r available=%r",
                    guild.id,
                    day,
                    role_name,
                    available,
                )
                continue

            _upsert_day_role(
                session_factory=session_factory,
                guild_id=guild.id,
                day=day,
                role_id=role.id,
                role_name=role.name,
            )


def _upsert_day_role(
    session_factory: sessionmaker[Session],
    guild_id: int,
    day: int,
    role_id: int,
    role_name: str,
) -> None:
    """Insert or selectively update one ``day_role_map`` row.

    Encapsulates the four-branch upsert logic described in
    :func:`seed_day_role_map`.

    Args:
        session_factory: Bound session factory.
        guild_id: Discord guild snowflake.
        day: Attack day number (1-indexed).
        role_id: Current Discord role snowflake.
        role_name: Current Discord role display name.
    """
    with session_factory() as session:
        stmt = select(DayRoleMap).where(
            DayRoleMap.guild_id == guild_id,
            DayRoleMap.day_number == day,
        )
        existing: DayRoleMap | None = session.execute(stmt).scalar_one_or_none()

        if existing is None:
            # No row — insert.
            row = DayRoleMap(
                guild_id=guild_id,
                day_number=day,
                discord_role_id=role_id,
                role_display_name=role_name,
            )
            session.add(row)
            session.commit()
            _logger.info(
                "DAY_ROLE_SEEDED guild_id=%s day=%s role_id=%s",
                guild_id,
                day,
                role_id,
            )
            return

        if existing.discord_role_id != role_id:
            # Snowflake changed — role was deleted and recreated.
            old_role_id = existing.discord_role_id
            old_name = existing.role_display_name
            existing.discord_role_id = role_id
            existing.role_display_name = role_name
            session.commit()
            _logger.info(
                "DAY_ROLE_SNOWFLAKE_CHANGED guild_id=%s day=%s "
                "old_role_id=%s new_role_id=%s old_name=%s new_name=%s",
                guild_id,
                day,
                old_role_id,
                role_id,
                old_name,
                role_name,
            )
            return

        if existing.role_display_name != role_name:
            # Same snowflake, cosmetic rename only.
            old_display = existing.role_display_name
            existing.role_display_name = role_name
            session.commit()
            _logger.debug(
                "DAY_ROLE_NAME_UPDATED guild_id=%s day=%s role_id=%s " "old_name=%s new_name=%s",
                guild_id,
                day,
                role_id,
                old_display,
                role_name,
            )
            return

        # Snowflake and name both match — true no-op (no DB write).
