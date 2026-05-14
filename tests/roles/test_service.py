"""Tests for mom_bot.roles.service — day-role toggle service (Epic 2.6 B1).

Covers every branch called out in issue #64 acceptance criteria and the plan
§ B1 design:

Happy paths
-----------
- Assign Day 1 to a member who holds no day role → ``applied``, added=[role_1_id]
- Assign Day 2 to a member who holds Day 1 → ``applied``, added=[role_2_id],
  removed=[role_1_id]
- Unassign Day 1 from a member who holds it → ``applied``, removed=[role_1_id]

Skip reasons
------------
- Member not in guild → ``skipped``, reason=``member_not_in_guild``
- day_number not in day_role_map → ``skipped``, reason=``role_not_seeded``
- Assign but member already has the role → ``skipped``, reason=``already_has_role``
- Unassign but member does not have the role → ``skipped``,
  reason=``already_lacks_role``

Partial / failed
----------------
- Assign Day 2 to Day-1 holder; remove_roles raises Forbidden →
  ``partial``, reason=``remove_of_other_day_failed_403``
- Assign; add_roles raises Forbidden → ``failed``

Preflight
---------
- All mapped roles below bot top → 0 violations, INFO summary logged
- One mapped role at-or-above bot top → CRITICAL ``ROLE_HIERARCHY_MISCONFIGURED``
  + ConfigError raised
- One mapped role missing from guild → WARNING logged, counted as missing

Contract validation
-------------------
- action="assign" with day_number=None → ValueError
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.config import ConfigError
from mom_bot.db import Base
from mom_bot.roles.models import DayRoleMap

# ---------------------------------------------------------------------------
# Test constants — representative Discord snowflakes
# ---------------------------------------------------------------------------

_GUILD_ID = 100_000_000_000_000_001
_MEMBER_DISCORD_ID = 200_000_000_000_000_001
_ROLE_ID_DAY1 = 300_000_000_000_000_001
_ROLE_ID_DAY2 = 300_000_000_000_000_002
_BOT_TOP_ROLE_ID = 400_000_000_000_000_001

_CORRELATION_ID = "test-corr-id-001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    """In-memory SQLite session factory with DayRoleMap table.

    Returns:
        A :class:`~sqlalchemy.orm.sessionmaker` bound to a fresh in-memory
        SQLite database with the ``day_role_map`` schema applied.
    """
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def seeded_factory(session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """Session factory pre-populated with Day 1 and Day 2 rows.

    Args:
        session_factory: In-memory session factory from the base fixture.

    Returns:
        The same factory, now with two ``DayRoleMap`` rows for days 1 and 2.
    """
    with session_factory() as session:
        session.add(
            DayRoleMap(
                guild_id=_GUILD_ID,
                day_number=1,
                discord_role_id=_ROLE_ID_DAY1,
                role_display_name="Attack Day 1",
            )
        )
        session.add(
            DayRoleMap(
                guild_id=_GUILD_ID,
                day_number=2,
                discord_role_id=_ROLE_ID_DAY2,
                role_display_name="Attack Day 2",
            )
        )
        session.commit()
    return session_factory


def _make_role(role_id: int, name: str, position: int = 5) -> MagicMock:
    """Build a minimal ``discord.Role`` mock.

    Args:
        role_id: Discord snowflake for the role.
        name: Display name.
        position: Role hierarchy position (higher = more elevated).

    Returns:
        A :class:`~unittest.mock.MagicMock` with ``spec=discord.Role``.
    """
    r = MagicMock(spec=discord.Role)
    r.id = role_id
    r.name = name
    r.position = position
    return r


def _make_bot_role(position: int = 10) -> MagicMock:
    """Build a minimal mock for the bot's top role.

    Args:
        position: Bot's top role hierarchy position.

    Returns:
        A :class:`~unittest.mock.MagicMock` representing the bot's top role.
    """
    r = MagicMock(spec=discord.Role)
    r.id = _BOT_TOP_ROLE_ID
    r.name = "Bot"
    r.position = position
    return r


def _make_guild(
    *,
    member: MagicMock | None,
    roles: list[MagicMock],
    bot_top_role: MagicMock,
) -> MagicMock:
    """Build a minimal ``discord.Guild`` mock.

    Args:
        member: The mock member returned by ``get_member``, or ``None``
            to simulate a member not in the guild.
        roles: List of role mocks the guild contains.
        bot_top_role: Mock for the bot's top role in this guild.

    Returns:
        A :class:`~unittest.mock.MagicMock` with ``spec=discord.Guild``.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.id = _GUILD_ID
    guild.get_member.return_value = member

    def _get_role(role_id: int) -> MagicMock | None:
        return next((r for r in roles if r.id == role_id), None)

    guild.get_role.side_effect = _get_role

    # guild.me is the bot's Member object; .top_role is its highest role.
    guild.me = MagicMock(spec=discord.Member)
    guild.me.top_role = bot_top_role

    return guild


def _make_member(held_role_ids: list[int]) -> MagicMock:
    """Build a minimal ``discord.Member`` mock.

    Args:
        held_role_ids: List of role IDs the member currently holds.

    Returns:
        A :class:`~unittest.mock.MagicMock` with ``spec=discord.Member``
        whose ``roles`` attribute is a list of matching role mocks.
    """
    member = MagicMock(spec=discord.Member)
    member.id = _MEMBER_DISCORD_ID
    member.roles = [_make_role(rid, f"Role-{rid}") for rid in held_role_ids]
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


# ---------------------------------------------------------------------------
# Happy path — assign Day 1 (no prior day role)
# ---------------------------------------------------------------------------


async def test_assign_day1_no_prior_role_returns_applied(
    seeded_factory: sessionmaker[Session],
) -> None:
    """Assign Day 1 to a member with no existing day role → applied.

    Verifies that ``apply_day_role`` with ``action="assign"`` and
    ``day_number=1`` adds the Day-1 role and returns ``status="applied"``,
    ``added=[_ROLE_ID_DAY1]``, ``removed=[]``.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
    """
    from mom_bot.roles.service import apply_day_role

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1")
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2")
    member = _make_member(held_role_ids=[])
    guild = _make_guild(
        member=member,
        roles=[role_day1, role_day2],
        bot_top_role=_make_bot_role(position=10),
    )

    result = await apply_day_role(
        guild=guild,
        discord_id=_MEMBER_DISCORD_ID,
        action="assign",
        day_number=1,
        correlation_id=_CORRELATION_ID,
        session_factory=seeded_factory,
    )

    assert result.status == "applied"
    assert result.added == [_ROLE_ID_DAY1]
    assert result.removed == []
    assert result.reason is None
    member.add_roles.assert_awaited_once_with(role_day1)
    member.remove_roles.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path — assign Day 2 when member holds Day 1 (swap)
# ---------------------------------------------------------------------------


async def test_assign_day2_removes_day1_returns_applied(
    seeded_factory: sessionmaker[Session],
) -> None:
    """Assign Day 2 to a member who holds Day 1 → applied with both mutations.

    Verifies that the service removes the old Day-1 role and adds the new
    Day-2 role, returning ``status="applied"``, ``added=[_ROLE_ID_DAY2]``,
    ``removed=[_ROLE_ID_DAY1]``.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
    """
    from mom_bot.roles.service import apply_day_role

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1")
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2")
    # Member currently holds Day 1.
    member = _make_member(held_role_ids=[_ROLE_ID_DAY1])
    member.roles = [role_day1]  # keep same mock object for identity check
    guild = _make_guild(
        member=member,
        roles=[role_day1, role_day2],
        bot_top_role=_make_bot_role(position=10),
    )

    result = await apply_day_role(
        guild=guild,
        discord_id=_MEMBER_DISCORD_ID,
        action="assign",
        day_number=2,
        correlation_id=_CORRELATION_ID,
        session_factory=seeded_factory,
    )

    assert result.status == "applied"
    assert result.added == [_ROLE_ID_DAY2]
    assert result.removed == [_ROLE_ID_DAY1]
    assert result.reason is None
    member.remove_roles.assert_awaited_once_with(role_day1)
    member.add_roles.assert_awaited_once_with(role_day2)


# ---------------------------------------------------------------------------
# Happy path — unassign Day 1 from a member who holds it
# ---------------------------------------------------------------------------


async def test_unassign_day1_member_holds_it_returns_applied(
    seeded_factory: sessionmaker[Session],
) -> None:
    """Unassign Day 1 from a member who holds it → applied.

    Verifies that ``action="unassign"`` with ``day_number=1`` removes
    the role and returns ``status="applied"``, ``added=[]``,
    ``removed=[_ROLE_ID_DAY1]``.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
    """
    from mom_bot.roles.service import apply_day_role

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1")
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2")
    member = _make_member(held_role_ids=[_ROLE_ID_DAY1])
    member.roles = [role_day1]
    guild = _make_guild(
        member=member,
        roles=[role_day1, role_day2],
        bot_top_role=_make_bot_role(position=10),
    )

    result = await apply_day_role(
        guild=guild,
        discord_id=_MEMBER_DISCORD_ID,
        action="unassign",
        day_number=1,
        correlation_id=_CORRELATION_ID,
        session_factory=seeded_factory,
    )

    assert result.status == "applied"
    assert result.added == []
    assert result.removed == [_ROLE_ID_DAY1]
    assert result.reason is None
    member.remove_roles.assert_awaited_once_with(role_day1)
    member.add_roles.assert_not_called()


# ---------------------------------------------------------------------------
# Skip — member not in guild
# ---------------------------------------------------------------------------


async def test_assign_member_not_in_guild_returns_skipped(
    seeded_factory: sessionmaker[Session],
) -> None:
    """Assign when member is absent from the guild → skipped, member_not_in_guild.

    Verifies that when ``guild.get_member`` returns ``None`` the service
    returns ``status="skipped"`` with ``reason="member_not_in_guild"`` and
    makes no Discord API calls.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
    """
    from mom_bot.roles.service import apply_day_role

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1")
    guild = _make_guild(
        member=None,  # not in guild
        roles=[role_day1],
        bot_top_role=_make_bot_role(position=10),
    )

    result = await apply_day_role(
        guild=guild,
        discord_id=_MEMBER_DISCORD_ID,
        action="assign",
        day_number=1,
        correlation_id=_CORRELATION_ID,
        session_factory=seeded_factory,
    )

    assert result.status == "skipped"
    assert result.reason == "member_not_in_guild"
    assert result.added == []
    assert result.removed == []


# ---------------------------------------------------------------------------
# Skip — day_number not seeded in day_role_map
# ---------------------------------------------------------------------------


async def test_assign_unseeded_day_returns_skipped(
    session_factory: sessionmaker[Session],
) -> None:
    """Assign when day_number has no DB row → skipped, role_not_seeded.

    Uses an empty (unseeded) session factory so day_number=1 has no
    corresponding ``DayRoleMap`` row.

    Args:
        session_factory: Empty (unseeded) in-memory session factory.
    """
    from mom_bot.roles.service import apply_day_role

    member = _make_member(held_role_ids=[])
    guild = _make_guild(
        member=member,
        roles=[],
        bot_top_role=_make_bot_role(position=10),
    )

    result = await apply_day_role(
        guild=guild,
        discord_id=_MEMBER_DISCORD_ID,
        action="assign",
        day_number=1,
        correlation_id=_CORRELATION_ID,
        session_factory=session_factory,
    )

    assert result.status == "skipped"
    assert result.reason == "role_not_seeded"
    assert result.added == []
    assert result.removed == []


# ---------------------------------------------------------------------------
# Skip — member already has the role (assign)
# ---------------------------------------------------------------------------


async def test_assign_already_has_role_returns_skipped(
    seeded_factory: sessionmaker[Session],
) -> None:
    """Assign when member already holds the target day role → skipped.

    Verifies that ``reason="already_has_role"`` is returned and no Discord
    API call is made when the member already holds the requested day role.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
    """
    from mom_bot.roles.service import apply_day_role

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1")
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2")
    # Member already holds Day 1.
    member = _make_member(held_role_ids=[_ROLE_ID_DAY1])
    member.roles = [role_day1]
    guild = _make_guild(
        member=member,
        roles=[role_day1, role_day2],
        bot_top_role=_make_bot_role(position=10),
    )

    result = await apply_day_role(
        guild=guild,
        discord_id=_MEMBER_DISCORD_ID,
        action="assign",
        day_number=1,  # same as held role
        correlation_id=_CORRELATION_ID,
        session_factory=seeded_factory,
    )

    assert result.status == "skipped"
    assert result.reason == "already_has_role"
    assert result.added == []
    assert result.removed == []
    member.add_roles.assert_not_called()


# ---------------------------------------------------------------------------
# Skip — member doesn't have the role (unassign)
# ---------------------------------------------------------------------------


async def test_unassign_member_lacks_role_returns_skipped(
    seeded_factory: sessionmaker[Session],
) -> None:
    """Unassign when member does not hold the target role → skipped.

    Verifies that ``reason="already_lacks_role"`` is returned and no Discord
    API call is made when the member does not hold the day role being removed.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
    """
    from mom_bot.roles.service import apply_day_role

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1")
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2")
    # Member holds no day roles.
    member = _make_member(held_role_ids=[])
    guild = _make_guild(
        member=member,
        roles=[role_day1, role_day2],
        bot_top_role=_make_bot_role(position=10),
    )

    result = await apply_day_role(
        guild=guild,
        discord_id=_MEMBER_DISCORD_ID,
        action="unassign",
        day_number=1,
        correlation_id=_CORRELATION_ID,
        session_factory=seeded_factory,
    )

    assert result.status == "skipped"
    assert result.reason == "already_lacks_role"
    assert result.added == []
    assert result.removed == []
    member.remove_roles.assert_not_called()


# ---------------------------------------------------------------------------
# Partial — remove_roles raises Forbidden during swap
# ---------------------------------------------------------------------------


async def test_assign_swap_remove_forbidden_returns_partial(
    seeded_factory: sessionmaker[Session],
) -> None:
    """Assign Day 2 to Day-1 holder; remove_roles raises Forbidden → partial.

    Simulates the remove-other-day step failing with a 403 while the
    add-new-day step still succeeds.  Verifies ``status="partial"``,
    ``reason="remove_of_other_day_failed_403"``, and that the new role was
    added despite the removal failure.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
    """
    from mom_bot.roles.service import apply_day_role

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1")
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2")
    member = _make_member(held_role_ids=[_ROLE_ID_DAY1])
    member.roles = [role_day1]
    # remove_roles raises Forbidden (403).
    member.remove_roles = AsyncMock(
        side_effect=discord.Forbidden(
            MagicMock(status=403),
            "Missing Permissions",
        )
    )
    guild = _make_guild(
        member=member,
        roles=[role_day1, role_day2],
        bot_top_role=_make_bot_role(position=10),
    )

    result = await apply_day_role(
        guild=guild,
        discord_id=_MEMBER_DISCORD_ID,
        action="assign",
        day_number=2,
        correlation_id=_CORRELATION_ID,
        session_factory=seeded_factory,
    )

    assert result.status == "partial"
    assert result.reason == "remove_of_other_day_failed_403"
    assert result.added == [_ROLE_ID_DAY2]
    assert result.removed == []
    member.add_roles.assert_awaited_once_with(role_day2)


# ---------------------------------------------------------------------------
# Failed — add_roles raises Forbidden
# ---------------------------------------------------------------------------


async def test_assign_add_forbidden_returns_failed(
    seeded_factory: sessionmaker[Session],
) -> None:
    """Assign; add_roles raises Forbidden → failed.

    Verifies that when ``Member.add_roles`` raises ``discord.Forbidden``
    the service returns ``status="failed"`` with empty ``added`` and
    ``removed`` lists.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
    """
    from mom_bot.roles.service import apply_day_role

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1")
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2")
    member = _make_member(held_role_ids=[])
    member.add_roles = AsyncMock(
        side_effect=discord.Forbidden(
            MagicMock(status=403),
            "Missing Permissions",
        )
    )
    guild = _make_guild(
        member=member,
        roles=[role_day1, role_day2],
        bot_top_role=_make_bot_role(position=10),
    )

    result = await apply_day_role(
        guild=guild,
        discord_id=_MEMBER_DISCORD_ID,
        action="assign",
        day_number=1,
        correlation_id=_CORRELATION_ID,
        session_factory=seeded_factory,
    )

    assert result.status == "failed"
    assert result.added == []
    assert result.removed == []


# ---------------------------------------------------------------------------
# Failed — remove_roles raises Forbidden during unassign
# ---------------------------------------------------------------------------


async def test_unassign_remove_forbidden_returns_failed(
    seeded_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unassign; remove_roles raises Forbidden → failed.

    Verifies that when ``Member.remove_roles`` raises
    ``discord.Forbidden`` during an unassign operation the service
    returns ``status="failed"`` with empty ``added`` and ``removed``
    lists, and that ``_maybe_emit_hierarchy_loss`` is invoked (checked
    via the dedup set being populated in the service module).

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and
            Day 2 rows.
        caplog: pytest log-capture fixture.
    """
    from mom_bot.roles import service as svc_module
    from mom_bot.roles.service import apply_day_role

    # Snapshot and clear the dedup set so a fresh hierarchy-loss event
    # can fire; restore after the test to avoid leaking state.
    snapshot = set(svc_module._hierarchy_loss_emitted)
    svc_module._hierarchy_loss_emitted.clear()
    try:
        role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1", position=15)
        role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2", position=3)
        member = _make_member(held_role_ids=[_ROLE_ID_DAY1])
        member.remove_roles = AsyncMock(
            side_effect=discord.Forbidden(
                MagicMock(status=403),
                "Missing Permissions",
            )
        )
        # Bot top role is below the target role — hierarchy violation.
        bot_top = _make_bot_role(position=10)
        guild = _make_guild(
            member=member,
            roles=[role_day1, role_day2],
            bot_top_role=bot_top,
        )

        with caplog.at_level(logging.ERROR, logger="mom_bot.roles.service"):
            result = await apply_day_role(
                guild=guild,
                discord_id=_MEMBER_DISCORD_ID,
                action="unassign",
                day_number=1,
                correlation_id=_CORRELATION_ID,
                session_factory=seeded_factory,
            )

        assert result.status == "failed"
        assert result.added == []
        assert result.removed == []

        # _maybe_emit_hierarchy_loss was invoked — the role_id should have
        # been added to the dedup set (since position 15 >= bot top 10).
        assert _ROLE_ID_DAY1 in svc_module._hierarchy_loss_emitted
    finally:
        # Restore pre-test state so downstream tests are unaffected.
        svc_module._hierarchy_loss_emitted.clear()
        svc_module._hierarchy_loss_emitted.update(snapshot)


# ---------------------------------------------------------------------------
# Contract validation — assign with day_number=None
# ---------------------------------------------------------------------------


async def test_assign_with_none_day_number_raises_value_error(
    seeded_factory: sessionmaker[Session],
) -> None:
    """action="assign" with day_number=None is a programming error → ValueError.

    Verifies that omitting ``day_number`` for an assign operation raises
    ``ValueError`` immediately (before any DB or Discord call).

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
    """
    from mom_bot.roles.service import apply_day_role

    member = _make_member(held_role_ids=[])
    guild = _make_guild(
        member=member,
        roles=[],
        bot_top_role=_make_bot_role(position=10),
    )

    with pytest.raises(ValueError, match="day_number"):
        await apply_day_role(
            guild=guild,
            discord_id=_MEMBER_DISCORD_ID,
            action="assign",
            day_number=None,
            correlation_id=_CORRELATION_ID,
            session_factory=seeded_factory,
        )


# ---------------------------------------------------------------------------
# Preflight — all roles below bot top → INFO summary, no ConfigError
# ---------------------------------------------------------------------------


def test_preflight_all_ok_logs_summary(
    seeded_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preflight with all roles below bot top → INFO summary, no exception.

    Verifies that ``run_preflight`` completes without raising and emits
    exactly one INFO record containing ``role_preflight_complete`` with
    ``violations=0`` and a non-zero total.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
        caplog: pytest log-capture fixture.
    """
    from mom_bot.roles.service import run_preflight

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1", position=3)
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2", position=4)
    bot_top = _make_bot_role(position=10)
    guild = _make_guild(
        member=None,
        roles=[role_day1, role_day2],
        bot_top_role=bot_top,
    )

    with caplog.at_level(logging.INFO, logger="mom_bot.roles.service"):
        run_preflight(guild=guild, session_factory=seeded_factory)

    summary_msgs = [
        r.getMessage() for r in caplog.records if "role_preflight_complete" in r.getMessage()
    ]
    assert len(summary_msgs) == 1
    assert "violations=0" in summary_msgs[0]
    assert "total=2" in summary_msgs[0]


# ---------------------------------------------------------------------------
# Preflight — one mapped role at-or-above bot top → CRITICAL + ConfigError
# ---------------------------------------------------------------------------


def test_preflight_hierarchy_violation_raises_config_error(
    seeded_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preflight with a role ranked at-or-above bot top → CRITICAL + ConfigError.

    Verifies that when ``guild.get_role(role_id).position >= bot_top.position``
    the function logs CRITICAL with ``ROLE_HIERARCHY_MISCONFIGURED`` and
    raises ``ConfigError``.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
        caplog: pytest log-capture fixture.
    """
    from mom_bot.roles.service import run_preflight

    # Day 1 role is ranked ABOVE the bot's top role.
    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1", position=15)
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2", position=3)
    bot_top = _make_bot_role(position=10)
    guild = _make_guild(
        member=None,
        roles=[role_day1, role_day2],
        bot_top_role=bot_top,
    )

    with caplog.at_level(logging.CRITICAL, logger="mom_bot.roles.service"):
        with pytest.raises(ConfigError):
            run_preflight(guild=guild, session_factory=seeded_factory)

    critical_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.CRITICAL and "ROLE_HIERARCHY_MISCONFIGURED" in r.getMessage()
    ]
    assert len(critical_msgs) >= 1
    combined = " ".join(critical_msgs)
    assert str(_ROLE_ID_DAY1) in combined


# ---------------------------------------------------------------------------
# Preflight — one mapped role missing from guild → WARNING logged
# ---------------------------------------------------------------------------


def test_preflight_missing_role_logs_warning(
    seeded_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preflight with a guild-missing role → WARNING + counted as missing.

    Verifies that when ``guild.get_role`` returns ``None`` for a mapped
    role ID the function logs a WARNING (not CRITICAL) and includes the
    missing count in the summary log.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
        caplog: pytest log-capture fixture.
    """
    from mom_bot.roles.service import run_preflight

    # Only Day 2 exists in the guild; Day 1 is missing.
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2", position=3)
    bot_top = _make_bot_role(position=10)
    guild = _make_guild(
        member=None,
        roles=[role_day2],  # Day 1 absent from guild
        bot_top_role=bot_top,
    )

    # Capture at INFO so we see both the WARNING and the INFO summary.
    with caplog.at_level(logging.INFO, logger="mom_bot.roles.service"):
        run_preflight(guild=guild, session_factory=seeded_factory)

    warning_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "role_not_in_guild" in r.getMessage()
    ]
    assert len(warning_msgs) >= 1

    # Summary must record the missing count.
    summary_msgs = [
        r.getMessage() for r in caplog.records if "role_preflight_complete" in r.getMessage()
    ]
    assert len(summary_msgs) == 1
    assert "missing=1" in summary_msgs[0]


# ---------------------------------------------------------------------------
# Runtime hierarchy-loss detection
# ---------------------------------------------------------------------------


async def test_add_roles_forbidden_emits_hierarchy_lost_at_runtime(
    seeded_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """add_roles Forbidden triggers ROLE_HIERARCHY_LOST_AT_RUNTIME when bot rank dropped.

    Simulates the scenario where the startup preflight passed (bot top_role
    was above the target role) but an admin moved the role above the bot
    mid-process.  On receiving ``discord.Forbidden``, the service re-fetches
    positions, detects the inversion, and emits ``ROLE_HIERARCHY_LOST_AT_RUNTIME``
    at ERROR level.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
        caplog: pytest log-capture fixture.
    """
    from mom_bot.roles.service import apply_day_role

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1", position=15)
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2", position=3)
    member = _make_member(held_role_ids=[])
    # add_roles raises Forbidden — simulates hierarchy violation.
    member.add_roles = AsyncMock(
        side_effect=discord.Forbidden(
            MagicMock(status=403),
            "Missing Permissions",
        )
    )
    # Bot top role is NOW below the target role (admin moved it post-startup).
    bot_top = _make_bot_role(position=10)
    guild = _make_guild(
        member=member,
        roles=[role_day1, role_day2],
        bot_top_role=bot_top,
    )

    with caplog.at_level(logging.ERROR, logger="mom_bot.roles.service"):
        result = await apply_day_role(
            guild=guild,
            discord_id=_MEMBER_DISCORD_ID,
            action="assign",
            day_number=1,
            correlation_id=_CORRELATION_ID,
            session_factory=seeded_factory,
        )

    assert result.status == "failed"

    error_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and "ROLE_HIERARCHY_LOST_AT_RUNTIME" in r.getMessage()
    ]
    assert len(error_msgs) >= 1
    combined = " ".join(error_msgs)
    assert str(_ROLE_ID_DAY1) in combined
    assert str(_MEMBER_DISCORD_ID) in combined


# ---------------------------------------------------------------------------
# Runtime hierarchy-loss — deduplication (same role_id only logged once)
# ---------------------------------------------------------------------------


async def test_hierarchy_lost_event_deduplicated_per_role(
    seeded_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ROLE_HIERARCHY_LOST_AT_RUNTIME is only emitted once per role_id per process.

    Calls ``apply_day_role`` twice for the same guild/role scenario.
    Verifies the event is logged exactly once, not twice.

    Args:
        seeded_factory: Session factory pre-seeded with Day 1 and Day 2 rows.
        caplog: pytest log-capture fixture.
    """
    from mom_bot.roles import service as svc_module
    from mom_bot.roles.service import apply_day_role

    # Reset the in-process dedup set between test runs.
    svc_module._hierarchy_loss_emitted.clear()

    role_day1 = _make_role(_ROLE_ID_DAY1, "Attack Day 1", position=15)
    role_day2 = _make_role(_ROLE_ID_DAY2, "Attack Day 2", position=3)
    bot_top = _make_bot_role(position=10)

    async def _run() -> None:
        member = _make_member(held_role_ids=[])
        member.add_roles = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "Missing Permissions")
        )
        guild = _make_guild(
            member=member,
            roles=[role_day1, role_day2],
            bot_top_role=bot_top,
        )
        await apply_day_role(
            guild=guild,
            discord_id=_MEMBER_DISCORD_ID,
            action="assign",
            day_number=1,
            correlation_id=_CORRELATION_ID,
            session_factory=seeded_factory,
        )

    with caplog.at_level(logging.ERROR, logger="mom_bot.roles.service"):
        await _run()
        await _run()

    error_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and "ROLE_HIERARCHY_LOST_AT_RUNTIME" in r.getMessage()
    ]
    assert len(error_msgs) == 1, (
        f"Expected exactly 1 ROLE_HIERARCHY_LOST_AT_RUNTIME event, "
        f"got {len(error_msgs)}: {error_msgs}"
    )
