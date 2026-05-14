"""Day-role toggle service and startup preflight for mom-bot (Epic 2.6 B1).

Provides two public callables:

``apply_day_role``
    Async service function called by the sidecar webhook endpoint (#65) and
    future slash commands.  Resolves a ``day_number → discord_role_id`` via
    the ``day_role_map`` table, then calls ``Member.add_roles()`` /
    ``Member.remove_roles()`` and returns a :class:`RoleSyncResult`
    conforming to the wire contract in ``siege-web/docs/webhooks/day-role-sync.md``.

``run_preflight``
    Synchronous function called once after the ``day_role_map`` seed
    completes in ``on_ready``.  Iterates every mapped role and compares its
    hierarchy position against the bot's top role.  Raises
    :class:`~mom_bot.config.ConfigError` if any mapped role is ranked
    at-or-above the bot's highest role (would cause 403 on every sync call).

Design notes
------------
- The ``_hierarchy_loss_emitted`` module-level ``set`` deduplicates the
  ``ROLE_HIERARCHY_LOST_AT_RUNTIME`` log event to one-per-affected-role-id
  per process lifetime, preventing log spam when many members hit the same
  broken hierarchy in quick succession.
- Both functions take ``session_factory`` rather than a session so they open
  and close their own sessions, matching the pattern in ``roles/seed.py``.
- Discord API calls (``add_roles`` / ``remove_roles``) are only made from
  ``apply_day_role``; ``run_preflight`` makes no mutating API calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.config import ConfigError
from mom_bot.roles.models import DayRoleMap

if TYPE_CHECKING:
    import discord

__all__ = [
    "RoleSyncResult",
    "apply_day_role",
    "run_preflight",
]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reason code constants (wire-contract § 3)
# ---------------------------------------------------------------------------

REASON_MEMBER_NOT_IN_GUILD: str = "member_not_in_guild"
REASON_ROLE_NOT_SEEDED: str = "role_not_seeded"
REASON_ALREADY_HAS_ROLE: str = "already_has_role"
REASON_ALREADY_LACKS_ROLE: str = "already_lacks_role"
REASON_REMOVE_OTHER_DAY_FAILED_403: str = "remove_of_other_day_failed_403"

# ---------------------------------------------------------------------------
# In-process dedup set for ROLE_HIERARCHY_LOST_AT_RUNTIME (plan § B1)
# ---------------------------------------------------------------------------

# Stores role_id values for which the runtime-hierarchy-loss event has already
# been emitted in this process.  One-per-affected-role-per-process to avoid
# flooding logs when many members hit the same broken hierarchy simultaneously.
_hierarchy_loss_emitted: set[int] = set()


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class RoleSyncResult:
    """Structured result returned by :func:`apply_day_role`.

    Conforms to the wire contract response shape defined in
    ``siege-web/docs/webhooks/day-role-sync.md`` § 3.

    Attributes:
        status: Overall outcome of the operation.  One of:
            ``"applied"`` — all mutations succeeded;
            ``"skipped"`` — no mutation was attempted (idempotent or
            member/role absent);
            ``"partial"`` — one of two mutations succeeded, the other
            failed (typically a 403 on the other-day-role removal);
            ``"failed"`` — the primary mutation failed.
        added: List of Discord role IDs that were successfully added.
        removed: List of Discord role IDs that were successfully removed.
        reason: Optional reason code from the six contract codes, or
            ``None`` when the status is ``"applied"``.
        last_assigned_at: Reserved for the idempotency layer in #65; the
            service itself always returns ``None`` here.
    """

    status: Literal["applied", "skipped", "partial", "failed"]
    added: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    reason: str | None = None
    last_assigned_at: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_day_role_map(
    guild_id: int,
    session_factory: sessionmaker[Session],
) -> dict[int, int]:
    """Load all day→role_id entries for *guild_id* from the database.

    Args:
        guild_id: Discord guild snowflake to filter by.
        session_factory: Bound session factory.

    Returns:
        A mapping ``{day_number: discord_role_id}`` for the given guild.
        Empty if no rows are seeded.
    """
    with session_factory() as session:
        rows = (
            session.execute(select(DayRoleMap).where(DayRoleMap.guild_id == guild_id))
            .scalars()
            .all()
        )
    return {row.day_number: row.discord_role_id for row in rows}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def apply_day_role(
    *,
    guild: discord.Guild,
    discord_id: int,
    action: Literal["assign", "unassign"],
    day_number: int | None,
    correlation_id: str,
    session_factory: sessionmaker[Session],
) -> RoleSyncResult:
    """Add or remove a day role on a guild member.

    For ``action="assign"`` of day N:
    1. Resolve the day-N role from ``day_role_map``.
    2. Fetch the member; return ``skipped/member_not_in_guild`` if absent.
    3. If member already holds day N → return ``skipped/already_has_role``.
    4. Identify whether the member holds any *other* day role M (M ≠ N).
       If so, attempt ``remove_roles(role_M)``:
       - ``discord.Forbidden`` → record ``remove_of_other_day_failed_403``;
         continue to add step with ``partial`` pending.
    5. Attempt ``add_roles(role_N)``:
       - ``discord.Forbidden`` → log WARNING + trigger hierarchy-loss check;
         return ``failed``.
       - Success → return ``applied`` (or ``partial`` if step 4 had a 403).

    For ``action="unassign"`` of day N:
    1. Resolve the day-N role; return ``skipped/role_not_seeded`` if absent.
    2. Fetch the member; return ``skipped/member_not_in_guild`` if absent.
    3. If member does not hold day N → return ``skipped/already_lacks_role``.
    4. Attempt ``remove_roles(role_N)``; return ``applied`` on success.

    Args:
        guild: The connected :class:`discord.Guild` in which the member lives.
        discord_id: Discord member snowflake.
        action: ``"assign"`` to add the role, ``"unassign"`` to remove it.
        day_number: The attack-day number to act on.  Required for both
            actions — raises :exc:`ValueError` if ``None`` is passed for
            ``action="assign"``.  For ``"unassign"`` it identifies which
            day's role to remove.
        correlation_id: Opaque string from the caller used in all log lines
            for request tracing.
        session_factory: Bound SQLAlchemy session factory used to look up
            ``day_role_map`` rows.

    Returns:
        A :class:`RoleSyncResult` describing the outcome.

    Raises:
        ValueError: If ``action="assign"`` and ``day_number`` is ``None``
            (programming error — callers must supply the day to assign).
    """
    if action == "assign" and day_number is None:
        raise ValueError("day_number is required when action='assign'")

    # Load the full day→role map for this guild.
    day_map = _load_day_role_map(guild.id, session_factory)

    if action == "assign":
        return await _handle_assign(
            guild=guild,
            discord_id=discord_id,
            day_number=day_number,  # type: ignore[arg-type]  # guaranteed non-None
            day_map=day_map,
            correlation_id=correlation_id,
        )
    else:
        return await _handle_unassign(
            guild=guild,
            discord_id=discord_id,
            day_number=day_number,
            day_map=day_map,
            correlation_id=correlation_id,
        )


async def _handle_assign(
    *,
    guild: discord.Guild,
    discord_id: int,
    day_number: int,
    day_map: dict[int, int],
    correlation_id: str,
) -> RoleSyncResult:
    """Implement the assign path for :func:`apply_day_role`.

    Args:
        guild: Target guild.
        discord_id: Member snowflake.
        day_number: Attack day to assign.
        day_map: Pre-loaded ``{day_number: role_id}`` mapping for this guild.
        correlation_id: Tracing identifier for log lines.

    Returns:
        A :class:`RoleSyncResult` for the assign operation.
    """
    import discord as _discord

    if day_number not in day_map:
        _logger.warning(
            "role_not_seeded correlation_id=%s guild_id=%s day_number=%s",
            correlation_id,
            guild.id,
            day_number,
        )
        return RoleSyncResult(
            status="skipped",
            reason=REASON_ROLE_NOT_SEEDED,
        )

    target_role_id = day_map[day_number]
    target_role = guild.get_role(target_role_id)

    # Guard: seeded role ID not present in guild (race between seed and
    # admin deletion, or preflight not yet run).  Surface as role_not_seeded
    # so the caller knows the map entry exists but the live role does not.
    if target_role is None:
        _logger.warning(
            "role_not_in_guild_at_runtime correlation_id=%s guild_id=%s "
            "day_number=%s role_id=%s",
            correlation_id,
            guild.id,
            day_number,
            target_role_id,
        )
        return RoleSyncResult(
            status="skipped",
            reason=REASON_ROLE_NOT_SEEDED,
        )

    member = guild.get_member(discord_id)
    if member is None:
        _logger.warning(
            "member_not_in_guild correlation_id=%s discord_id=%s",
            correlation_id,
            discord_id,
        )
        return RoleSyncResult(
            status="skipped",
            reason=REASON_MEMBER_NOT_IN_GUILD,
        )

    # Collect the IDs of day roles the member currently holds.
    member_role_ids = {r.id for r in member.roles}

    if target_role_id in member_role_ids:
        _logger.info(
            "already_has_role correlation_id=%s discord_id=%s role_id=%s",
            correlation_id,
            discord_id,
            target_role_id,
        )
        return RoleSyncResult(
            status="skipped",
            reason=REASON_ALREADY_HAS_ROLE,
        )

    # Identify any *other* day role the member holds (M ≠ N).
    other_day_role_id: int | None = None
    other_day_role = None
    for other_day, other_rid in day_map.items():
        if other_day != day_number and other_rid in member_role_ids:
            other_day_role_id = other_rid
            other_day_role = next((r for r in member.roles if r.id == other_rid), None)
            break

    # Track whether the other-day removal hit a 403.
    remove_failed_403 = False
    removed_ids: list[int] = []

    if other_day_role is not None:
        try:
            await member.remove_roles(other_day_role)
            removed_ids.append(other_day_role_id)  # type: ignore[arg-type]
        except _discord.Forbidden:
            _logger.warning(
                "remove_roles_forbidden correlation_id=%s discord_id=%s "
                "role_id=%s day_number=%s",
                correlation_id,
                discord_id,
                other_day_role_id,
                day_number,
            )
            remove_failed_403 = True

    # Now attempt the add.
    try:
        await member.add_roles(target_role)
    except _discord.Forbidden:
        _logger.warning(
            "add_roles_forbidden correlation_id=%s discord_id=%s " "role_id=%s day_number=%s",
            correlation_id,
            discord_id,
            target_role_id,
            day_number,
        )
        _maybe_emit_hierarchy_loss(
            guild=guild,
            role_id=target_role_id,
            discord_id=discord_id,
            correlation_id=correlation_id,
        )
        return RoleSyncResult(
            status="failed",
            added=[],
            removed=removed_ids,
        )

    if remove_failed_403:
        return RoleSyncResult(
            status="partial",
            added=[target_role_id],
            removed=[],
            reason=REASON_REMOVE_OTHER_DAY_FAILED_403,
        )

    return RoleSyncResult(
        status="applied",
        added=[target_role_id],
        removed=removed_ids,
    )


async def _handle_unassign(
    *,
    guild: discord.Guild,
    discord_id: int,
    day_number: int | None,
    day_map: dict[int, int],
    correlation_id: str,
) -> RoleSyncResult:
    """Implement the unassign path for :func:`apply_day_role`.

    Args:
        guild: Target guild.
        discord_id: Member snowflake.
        day_number: Attack day to unassign (used to resolve the role).
        day_map: Pre-loaded ``{day_number: role_id}`` mapping.
        correlation_id: Tracing identifier for log lines.

    Returns:
        A :class:`RoleSyncResult` for the unassign operation.
    """
    if day_number not in day_map:
        _logger.warning(
            "role_not_seeded correlation_id=%s guild_id=%s day_number=%s",
            correlation_id,
            guild.id,
            day_number,
        )
        return RoleSyncResult(
            status="skipped",
            reason=REASON_ROLE_NOT_SEEDED,
        )

    target_role_id = day_map[day_number]
    target_role = guild.get_role(target_role_id)

    # Guard: seeded role ID not present in guild (race between seed and
    # admin deletion).  If the role is gone there's nothing to remove.
    if target_role is None:
        _logger.warning(
            "role_not_in_guild_at_runtime correlation_id=%s guild_id=%s "
            "day_number=%s role_id=%s",
            correlation_id,
            guild.id,
            day_number,
            target_role_id,
        )
        return RoleSyncResult(
            status="skipped",
            reason=REASON_ROLE_NOT_SEEDED,
        )

    member = guild.get_member(discord_id)
    if member is None:
        _logger.warning(
            "member_not_in_guild correlation_id=%s discord_id=%s",
            correlation_id,
            discord_id,
        )
        return RoleSyncResult(
            status="skipped",
            reason=REASON_MEMBER_NOT_IN_GUILD,
        )

    member_role_ids = {r.id for r in member.roles}

    if target_role_id not in member_role_ids:
        _logger.info(
            "already_lacks_role correlation_id=%s discord_id=%s role_id=%s",
            correlation_id,
            discord_id,
            target_role_id,
        )
        return RoleSyncResult(
            status="skipped",
            reason=REASON_ALREADY_LACKS_ROLE,
        )

    import discord as _discord

    try:
        await member.remove_roles(target_role)
    except _discord.Forbidden:
        _logger.warning(
            "remove_roles_forbidden correlation_id=%s discord_id=%s " "role_id=%s day_number=%s",
            correlation_id,
            discord_id,
            target_role_id,
            day_number,
        )
        _maybe_emit_hierarchy_loss(
            guild=guild,
            role_id=target_role_id,
            discord_id=discord_id,
            correlation_id=correlation_id,
        )
        return RoleSyncResult(
            status="failed",
            added=[],
            removed=[],
        )
    return RoleSyncResult(
        status="applied",
        added=[],
        removed=[target_role_id],
    )


def _maybe_emit_hierarchy_loss(
    *,
    guild: discord.Guild,
    role_id: int,
    discord_id: int,
    correlation_id: str,
) -> None:
    """Emit ``ROLE_HIERARCHY_LOST_AT_RUNTIME`` if hierarchy is now inverted.

    Called after any ``discord.Forbidden`` from ``add_roles`` or
    ``remove_roles``.  Re-fetches the target role's current position and the
    bot's top role position.  If the bot's top role is now at-or-below the
    target role, emits an ERROR log exactly once per ``role_id`` per process.

    Args:
        guild: The guild in which the 403 occurred.
        role_id: Snowflake of the role that could not be mutated.
        discord_id: The affected member's snowflake, for log context.
        correlation_id: Caller-supplied tracing string.
    """
    if role_id in _hierarchy_loss_emitted:
        return

    target_role = guild.get_role(role_id)
    bot_top = guild.me.top_role

    if target_role is None:
        return

    if target_role.position >= bot_top.position:
        _hierarchy_loss_emitted.add(role_id)
        _logger.error(
            "ROLE_HIERARCHY_LOST_AT_RUNTIME correlation_id=%s discord_id=%s "
            "role_id=%s role_position=%s bot_top_role_position=%s",
            correlation_id,
            discord_id,
            role_id,
            target_role.position,
            bot_top.position,
        )


def run_preflight(
    *,
    guild: discord.Guild,
    session_factory: sessionmaker[Session],
) -> None:
    """Validate role hierarchy for every mapped day role at startup.

    Reads every ``DayRoleMap`` row for *guild*, then for each row:

    - If ``guild.get_role(role_id)`` returns ``None``: log WARNING with
      ``role_not_in_guild`` and increment the missing count.
    - Else if ``role.position >= guild.me.top_role.position``: log CRITICAL
      with ``ROLE_HIERARCHY_MISCONFIGURED`` — the bot cannot manage this
      role.

    After iterating all rows, emits an INFO ``role_preflight_complete`` log
    with ``total``, ``violations``, and ``missing`` counts.

    If any violations were found, raises :class:`~mom_bot.config.ConfigError`
    so the bot exits rather than starting with a broken role configuration.

    Args:
        guild: The connected :class:`discord.Guild` to inspect.
        session_factory: Bound SQLAlchemy session factory for reading
            ``day_role_map`` rows.

    Raises:
        ConfigError: If any mapped role is ranked at-or-above the bot's
            highest role in the guild's hierarchy.
    """
    day_map = _load_day_role_map(guild.id, session_factory)
    bot_top = guild.me.top_role

    total = 0
    violations = 0
    missing = 0

    for day_number, role_id in day_map.items():
        total += 1
        role = guild.get_role(role_id)

        if role is None:
            missing += 1
            _logger.warning(
                "role_not_in_guild guild_id=%s day_number=%s role_id=%s",
                guild.id,
                day_number,
                role_id,
            )
            continue

        if role.position >= bot_top.position:
            violations += 1
            _logger.critical(
                "ROLE_HIERARCHY_MISCONFIGURED guild_id=%s day_number=%s "
                "role_id=%s role_name=%s role_position=%s "
                "bot_top_role_position=%s",
                guild.id,
                day_number,
                role_id,
                role.name,
                role.position,
                bot_top.position,
            )

    _logger.info(
        "role_preflight_complete guild_id=%s total=%s violations=%s missing=%s",
        guild.id,
        total,
        violations,
        missing,
    )

    if violations:
        raise ConfigError(
            message=(
                f"ROLE_HIERARCHY_MISCONFIGURED: {violations} day role(s) are ranked "
                f"at or above the bot's top role in guild {guild.id}. "
                "Fix the Discord role hierarchy before restarting."
            )
        )
