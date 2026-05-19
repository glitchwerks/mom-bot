"""Tests for mom_bot.roles.seed — day_role_map seed/refresh routine (Epic 2.6).

Covers the four observable behaviors called out in issue #62:

1. Insert when table is empty (both days seeded).
2. No-op when called twice with the same Discord state (snowflake unchanged).
3. Snowflake-changed log + row update when a role's ID differs from the DB.
4. Warning when a guild has no "Attack Day N" role.

All tests use an in-memory SQLite database so they are fast and hermetic.
Discord guild/role objects are mocked with ``unittest.mock.MagicMock`` using
``spec=`` constraints so ``.name`` and ``.id`` return proper values (not
nested MagicMocks).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import discord
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.db import Base
from mom_bot.roles.models import DayRoleMap

# ---------------------------------------------------------------------------
# Test constants — representative Discord snowflakes (safe 64-bit integers)
# ---------------------------------------------------------------------------

_GUILD_ID = 111_000_000_000_000_001
_ROLE_ID_DAY1 = 222_000_000_000_000_001
_ROLE_ID_DAY2 = 222_000_000_000_000_002
_ROLE_ID_DAY1_NEW = 333_000_000_000_000_001  # simulates snowflake change

_ROLE_NAME_DAY1 = "Attack Day 1"
_ROLE_NAME_DAY2 = "Attack Day 2"
_ROLE_NAME_DAY1_RENAMED = "Attack Day 1 (renamed)"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    """In-memory SQLite session factory with the day_role_map table created.

    Returns:
        A :class:`~sqlalchemy.orm.sessionmaker` bound to a fresh in-memory
        SQLite database with ``DayRoleMap`` schema applied.
    """
    engine = create_engine("sqlite:///:memory:", echo=False)
    # Import DayRoleMap so metadata is populated before create_all.
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _make_mock_client(
    guild_id: int,
    roles: list[tuple[int, str]],
) -> MagicMock:
    """Build a minimal ``discord.Client`` mock with one guild and given roles.

    Args:
        guild_id: The guild's Discord snowflake.
        roles: List of (role_id, role_name) tuples to attach to the guild.

    Returns:
        A :class:`~unittest.mock.MagicMock` with ``spec=discord.Client`` whose
        ``.guilds`` attribute contains one mock guild.
    """
    mock_roles = []
    for role_id, role_name in roles:
        r = MagicMock(spec=discord.Role)
        r.id = role_id
        r.name = role_name
        mock_roles.append(r)

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.id = guild_id
    mock_guild.roles = mock_roles

    client = MagicMock(spec=discord.Client)
    client.guilds = [mock_guild]
    return client


# ---------------------------------------------------------------------------
# Test 1: seed inserts when table is empty
# ---------------------------------------------------------------------------


async def test_seed_inserts_when_table_empty(
    session_factory: sessionmaker[Session],
) -> None:
    """Empty DB + guild with both Attack Day roles → two rows inserted.

    Verifies that ``seed_day_role_map`` creates one ``DayRoleMap`` row per
    attack day when the table contains no pre-existing rows.

    Args:
        session_factory: In-memory session factory from the fixture.
    """
    from mom_bot.roles.seed import seed_day_role_map

    client = _make_mock_client(
        _GUILD_ID,
        [(_ROLE_ID_DAY1, _ROLE_NAME_DAY1), (_ROLE_ID_DAY2, _ROLE_NAME_DAY2)],
    )

    await seed_day_role_map(client, session_factory)

    with session_factory() as session:
        rows = session.execute(select(DayRoleMap)).scalars().all()

    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"

    by_day = {r.day_number: r for r in rows}

    assert by_day[1].discord_role_id == _ROLE_ID_DAY1
    assert by_day[1].role_display_name == _ROLE_NAME_DAY1
    assert by_day[1].guild_id == _GUILD_ID

    assert by_day[2].discord_role_id == _ROLE_ID_DAY2
    assert by_day[2].role_display_name == _ROLE_NAME_DAY2
    assert by_day[2].guild_id == _GUILD_ID


# ---------------------------------------------------------------------------
# Test 2: no-op when snowflakes unchanged (idempotency)
# ---------------------------------------------------------------------------


async def test_seed_noop_when_snowflakes_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    """Running seed twice with no Discord changes produces zero writes.

    Pre-populates the table and then calls ``seed_day_role_map`` again.
    The ``updated_at`` timestamps must be identical after both calls —
    SQLAlchemy only bumps ``updated_at`` on an actual UPDATE statement,
    so an unchanged value proves the second call did not write to the DB.

    Args:
        session_factory: In-memory session factory from the fixture.
    """
    from mom_bot.roles.seed import seed_day_role_map

    client = _make_mock_client(
        _GUILD_ID,
        [(_ROLE_ID_DAY1, _ROLE_NAME_DAY1), (_ROLE_ID_DAY2, _ROLE_NAME_DAY2)],
    )

    # First seed — inserts two rows.
    await seed_day_role_map(client, session_factory)

    with session_factory() as session:
        rows_after_first = session.execute(select(DayRoleMap)).scalars().all()
        timestamps_first = {r.day_number: r.updated_at for r in rows_after_first}

    # Second seed — Discord state is identical; must be a no-op.
    await seed_day_role_map(client, session_factory)

    with session_factory() as session:
        rows_after_second = session.execute(select(DayRoleMap)).scalars().all()
        timestamps_second = {r.day_number: r.updated_at for r in rows_after_second}

    assert len(rows_after_second) == 2
    for day in (1, 2):
        assert timestamps_first[day] == timestamps_second[day], (
            f"Day {day}: updated_at changed on second seed "
            f"({timestamps_first[day]} → {timestamps_second[day]}); "
            "expected no-op when snowflake is unchanged."
        )


# ---------------------------------------------------------------------------
# Test 3: snowflake-changed event logged and row updated
# ---------------------------------------------------------------------------


async def test_seed_emits_snowflake_changed_on_rename(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Role snowflake changes between seeds → INFO log + row updated.

    Pre-populates day 1 with ``_ROLE_ID_DAY1``, then runs seed with a client
    that returns ``_ROLE_ID_DAY1_NEW`` for the same guild and day.  Expects:

    - An INFO record whose message contains ``DAY_ROLE_SNOWFLAKE_CHANGED``.
    - The record message also contains both the old and new role IDs.
    - The ``discord_role_id`` column in the DB reflects the new value.

    Args:
        session_factory: In-memory session factory from the fixture.
        caplog: pytest log-capture fixture for asserting log output.
    """
    from mom_bot.roles.seed import seed_day_role_map

    # Seed with original role IDs.
    client_before = _make_mock_client(
        _GUILD_ID,
        [(_ROLE_ID_DAY1, _ROLE_NAME_DAY1), (_ROLE_ID_DAY2, _ROLE_NAME_DAY2)],
    )
    await seed_day_role_map(client_before, session_factory)

    # Now present a new snowflake for day 1 (role was deleted + recreated).
    client_after = _make_mock_client(
        _GUILD_ID,
        [(_ROLE_ID_DAY1_NEW, _ROLE_NAME_DAY1), (_ROLE_ID_DAY2, _ROLE_NAME_DAY2)],
    )

    with caplog.at_level(logging.INFO, logger="mom_bot.roles.seed"):
        await seed_day_role_map(client_after, session_factory)

    # Assert the structured log event was emitted.
    info_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and "DAY_ROLE_SNOWFLAKE_CHANGED" in r.getMessage()
    ]
    assert len(info_msgs) >= 1, (
        "Expected at least one INFO record containing 'DAY_ROLE_SNOWFLAKE_CHANGED'; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )

    # The log message must reference both the old and new snowflakes.
    combined = " ".join(info_msgs)
    assert (
        str(_ROLE_ID_DAY1) in combined
    ), f"Old role ID {_ROLE_ID_DAY1} not found in log: {combined}"
    assert (
        str(_ROLE_ID_DAY1_NEW) in combined
    ), f"New role ID {_ROLE_ID_DAY1_NEW} not found in log: {combined}"

    # DB row must reflect the new snowflake.
    with session_factory() as session:
        row = session.execute(
            select(DayRoleMap).where(
                DayRoleMap.guild_id == _GUILD_ID,
                DayRoleMap.day_number == 1,
            )
        ).scalar_one()
    assert row.discord_role_id == _ROLE_ID_DAY1_NEW


# ---------------------------------------------------------------------------
# Test 4: warning logged when role is missing from guild
# ---------------------------------------------------------------------------


async def test_seed_logs_warning_when_role_missing(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Guild with no 'Attack Day 1' role → WARNING with enriched context.

    Confirms that when the guild's role list does not contain a role named
    ``"Attack Day {day}"``, the seed routine logs a WARNING containing:

    - The sentinel string ``DAY_ROLE_NOT_FOUND``.
    - ``expected=`` showing the literal role name being searched for.
    - ``available=`` showing the guild's actual role names.

    The routine must continue without crashing and still seed day 2.

    Args:
        session_factory: In-memory session factory from the fixture.
        caplog: pytest log-capture fixture for asserting log output.
    """
    from mom_bot.roles.seed import seed_day_role_map

    # Guild only has day 2; day 1 is absent.
    client = _make_mock_client(
        _GUILD_ID,
        [(_ROLE_ID_DAY2, _ROLE_NAME_DAY2)],
    )

    with caplog.at_level(logging.WARNING, logger="mom_bot.roles.seed"):
        await seed_day_role_map(client, session_factory)

    warning_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "DAY_ROLE_NOT_FOUND" in r.getMessage()
    ]
    assert len(warning_msgs) >= 1, (
        "Expected WARNING with 'DAY_ROLE_NOT_FOUND'; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )

    combined = " ".join(warning_msgs)

    # The searched name must appear under expected=.
    assert "expected=" in combined, (
        f"Expected 'expected=' in log message; got: {combined}"
    )
    assert "Attack Day 1" in combined, (
        f"Expected searched role name 'Attack Day 1' in log; got: {combined}"
    )

    # The available roles must appear under available=.
    assert "available=" in combined, (
        f"Expected 'available=' in log message; got: {combined}"
    )
    assert _ROLE_NAME_DAY2 in combined, (
        f"Expected guild role '{_ROLE_NAME_DAY2}' in available= list; "
        f"got: {combined}"
    )

    # Day 2 row should still be inserted even though day 1 was skipped.
    with session_factory() as session:
        rows = session.execute(select(DayRoleMap)).scalars().all()
    assert len(rows) == 1
    assert rows[0].day_number == 2


# ---------------------------------------------------------------------------
# Test 5: available= list reflects guild roles when names differ from expected
# ---------------------------------------------------------------------------


async def test_seed_warning_available_reflects_actual_guild_roles(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Guild with differently-named roles → available= shows those names sorted.

    Simulates the name-mismatch root cause from issue #127: the guild has
    roles but none matches ``"Attack Day {N}"``.  Verifies that
    ``available=`` in the WARNING log contains the guild's actual role names
    in sorted order, enabling operators to see the mismatch at a glance.

    Args:
        session_factory: In-memory session factory from the fixture.
        caplog: pytest log-capture fixture for asserting log output.
    """
    from mom_bot.roles.seed import seed_day_role_map

    # Guild has roles with non-matching names (e.g. "Day 1" / "Day 2").
    _WRONG_ROLE_ID_1 = 444_000_000_000_000_001
    _WRONG_ROLE_ID_2 = 444_000_000_000_000_002
    _WRONG_NAME_1 = "Day 1"
    _WRONG_NAME_2 = "Day 2"

    client = _make_mock_client(
        _GUILD_ID,
        [(_WRONG_ROLE_ID_1, _WRONG_NAME_1), (_WRONG_ROLE_ID_2, _WRONG_NAME_2)],
    )

    with caplog.at_level(logging.WARNING, logger="mom_bot.roles.seed"):
        await seed_day_role_map(client, session_factory)

    warning_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "DAY_ROLE_NOT_FOUND" in r.getMessage()
    ]
    assert len(warning_msgs) >= 1, (
        "Expected at least one WARNING with 'DAY_ROLE_NOT_FOUND'; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )

    combined = " ".join(warning_msgs)

    # Both wrong-named roles must appear in available= so the operator can
    # see the exact names present on the guild.
    assert _WRONG_NAME_1 in combined, (
        f"Expected '{_WRONG_NAME_1}' in available= list; got: {combined}"
    )
    assert _WRONG_NAME_2 in combined, (
        f"Expected '{_WRONG_NAME_2}' in available= list; got: {combined}"
    )

    # The available list must be sorted (repr of a sorted Python list).
    # "Day 1" < "Day 2" lexicographically, so Day 1 must appear first.
    assert combined.index(_WRONG_NAME_1) < combined.index(_WRONG_NAME_2), (
        f"Expected '{_WRONG_NAME_1}' to appear before '{_WRONG_NAME_2}' "
        f"(sorted order) in: {combined}"
    )

    # No rows should be inserted — none of the roles matched.
    with session_factory() as session:
        rows = session.execute(select(DayRoleMap)).scalars().all()
    assert len(rows) == 0
