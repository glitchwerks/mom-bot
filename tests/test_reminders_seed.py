"""Tests for _maybe_seed_reminders.

Verifies idempotent seed-on-boot from Key Vault values, including
guild resolution via the per-env ``guild-id`` KV secret (#49) and
channel-name-to-snowflake resolution via the discord.py client (#47).
"""

from __future__ import annotations

import datetime
import logging
from unittest.mock import MagicMock, patch

import discord
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from mom_bot.db import Base
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401
from mom_bot.reminders.seed import _maybe_seed_reminders

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CHANNEL_NAME = "reminders"
# Discord snowflakes are 18-19 digit integers; SQLite stores INTEGER as a
# signed 64-bit value (max ~9.2e18).  Use a value safely within that range.
_CHANNEL_ID = 987654321098765432
_GUILD_ID = 1234567890
_GUILD_NAME = "test-guild"


@pytest.fixture()
def session() -> Session:
    """In-memory SQLite session with both reminder tables created."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def mock_bot() -> MagicMock:
    """A discord.Client mock whose get_guild() returns a configured guild.

    Uses ``spec=`` on all mocks so attribute access is constrained to real
    discord.py attributes — critical for ``discord.utils.get``, which
    iterates ``text_channels`` and compares ``channel.name`` by value.
    Without ``spec=``, ``mock.name`` returns a fresh ``MagicMock`` that
    will never string-equal ``"reminders"``.

    The bot resolves guild by ID via ``bot.get_guild(int(guild_id))``, so
    we set ``bot.get_guild`` to return ``mock_guild`` when called with the
    expected integer ID (#49).  The old ``bot.guilds = [mock_guild]`` pattern
    is replaced — ``bot.guilds[0]`` was non-deterministic in multi-guild bots.
    """
    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.name = _CHANNEL_NAME
    mock_channel.id = _CHANNEL_ID

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.text_channels = [mock_channel]
    mock_guild.name = _GUILD_NAME
    mock_guild.id = _GUILD_ID

    bot = MagicMock(spec=discord.Client)
    bot.get_guild = MagicMock(return_value=mock_guild)
    return bot


def _secret_side_effect(name: str) -> str:
    """Return a fake value for each expected KV secret name.

    Covers both ``guild-id`` (needed for guild resolution, #49) and
    ``reminder-channel-name`` (needed for channel resolution, #47).
    """
    secrets = {
        "guild-id": str(_GUILD_ID),
        "reminder-channel-name": _CHANNEL_NAME,
    }
    if name not in secrets:
        raise KeyError(f"Unexpected secret: {name!r}")
    return secrets[name]


# ---------------------------------------------------------------------------
# Seed-on-boot idempotency
# ---------------------------------------------------------------------------


def test_seed_empty_table_inserts_hydra_and_chimera(session: Session, mock_bot: MagicMock) -> None:
    """Empty table + valid KV + matching channel → two rows with resolved ID.

    Both rows share the resolved ``channel_id`` (the snowflake from
    ``discord.utils.get``, NOT the channel name string).
    ``role_mention_id`` is intentionally ``None`` for both rows — reminders
    post without role pings (#45).

    Hydra: weekday=1, fire_time=07:00:00.
    Chimera: weekday=2, fire_time=12:00:00.
    """
    with patch(
        "mom_bot.reminders.seed.load_secret",
        side_effect=_secret_side_effect,
    ):
        _maybe_seed_reminders(session, mock_bot)

    count = session.scalar(select(func.count(Reminder.id)))
    assert count == 2

    hydra = session.execute(select(Reminder).where(Reminder.name == "Hydra")).scalar_one()
    assert hydra.weekday == 1
    assert hydra.fire_time_utc == datetime.time(7, 0, 0)
    assert hydra.channel_id == _CHANNEL_ID
    assert hydra.role_mention_id is None

    chimera = session.execute(select(Reminder).where(Reminder.name == "Chimera")).scalar_one()
    assert chimera.weekday == 2
    assert chimera.fire_time_utc == datetime.time(12, 0, 0)
    assert chimera.channel_id == _CHANNEL_ID
    assert chimera.role_mention_id is None

    # Both rows share the single resolved channel — verify equality.
    assert hydra.channel_id == chimera.channel_id


def test_seed_non_empty_table_is_noop(session: Session, mock_bot: MagicMock) -> None:
    """Non-empty reminders table → _maybe_seed_reminders is a no-op.

    Crucially, the bot argument must not be accessed when the table is
    already populated — the count check happens before any guild/channel
    resolution.
    """
    # Pre-seed one reminder.
    existing = Reminder(
        name="TestReminder",
        channel_id=999999999999999999,
        weekday=0,
        fire_time_utc=datetime.time(8, 0, 0),
        message_template="test",
        role_mention_id=None,
    )
    session.add(existing)
    session.commit()

    call_count = 0

    def counting_secret(name: str) -> str:
        nonlocal call_count
        call_count += 1
        return "reminders"

    with patch(
        "mom_bot.reminders.seed.load_secret",
        side_effect=counting_secret,
    ):
        _maybe_seed_reminders(session, mock_bot)

    # No KV calls should be made when table is non-empty.
    assert call_count == 0
    count = session.scalar(select(func.count(Reminder.id)))
    assert count == 1  # Only the pre-seeded row.


def test_seed_kv_failure_logs_critical_and_raises(session: Session, mock_bot: MagicMock) -> None:
    """KV load_secret failure → CRITICAL logged, exception re-raised."""
    error = RuntimeError("KV unreachable")

    # Alembic's fileConfig (run by test_alembic.py earlier in the suite)
    # calls logging.config.fileConfig which replaces root logger handlers.
    # Use a direct handler on the module logger to capture records reliably
    # regardless of root handler state.
    captured: list[logging.LogRecord] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _CapturingHandler()
    seed_logger = logging.getLogger("mom_bot.reminders.seed")
    # Alembic's fileConfig with disable_existing_loggers=True (the default)
    # can mark mom_bot loggers as disabled if they existed before the call.
    # Force re-enable so the logger emits in this test.
    seed_logger.disabled = False
    seed_logger.addHandler(handler)
    seed_logger.setLevel(logging.DEBUG)

    try:
        with patch(
            "mom_bot.reminders.seed.load_secret",
            side_effect=error,
        ):
            with pytest.raises(RuntimeError, match="KV unreachable"):
                _maybe_seed_reminders(session, mock_bot)
    finally:
        seed_logger.removeHandler(handler)

    # At least one CRITICAL log message must have been emitted.
    critical_records = [r for r in captured if r.levelname == "CRITICAL"]
    assert len(critical_records) >= 1


def test_seed_channel_not_found_logs_critical_and_raises(
    session: Session,
    mock_bot: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Channel name not in guild → ConfigError raised + CRITICAL logged.

    The mock bot has a guild whose only text channel is named "reminders",
    but KV returns "nonexistent-channel" — so discord.utils.get returns
    None, triggering the ConfigError path.

    Uses pytest's ``caplog`` fixture for log capture — this is robust
    across test-ordering effects (Alembic fileConfig, etc.).

    Imports ``ConfigError`` at call-time to avoid stale-class issues when
    ``test_config.py`` reloads ``mom_bot.config`` via ``sys.modules.pop``.
    """

    def bad_secret(name: str) -> str:
        # Return a valid guild-id so guild resolution proceeds; return a
        # non-existent channel name so discord.utils.get finds no match.
        if name == "guild-id":
            return str(_GUILD_ID)
        return "nonexistent-channel"

    with caplog.at_level(logging.CRITICAL, logger="mom_bot.reminders.seed"):
        with patch(
            "mom_bot.reminders.seed.load_secret",
            side_effect=bad_secret,
        ):
            with pytest.raises(Exception) as exc_info:
                _maybe_seed_reminders(session, mock_bot)

    # Verify exception type by name to survive module-reload class divergence.
    assert exc_info.type.__name__ == "ConfigError"
    critical_records = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(critical_records) >= 1


def test_seed_bot_not_in_configured_guild_raises_config_error(
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """bot.get_guild(guild_id) returns None → ConfigError raised + CRITICAL logged.

    Covers both "bot is in zero guilds" and "bot is in other guilds but not
    the one named in KV" — ``get_guild()`` returning None is the single
    authoritative signal for both cases.  Replacing the old ``bot.guilds``
    empty-list guard with this check makes the error message actionable:
    it names the expected guild ID and the KV secret to fix (#49).

    Uses pytest's ``caplog`` fixture for log capture — robust across
    test-ordering effects (Alembic fileConfig, etc.).

    Imports ``ConfigError`` at call-time to avoid stale-class issues when
    ``test_config.py`` reloads ``mom_bot.config`` via ``sys.modules.pop``.
    """
    # Build a bot mock where get_guild() returns None — bot is not a member
    # of the guild whose ID is stored in the "guild-id" KV secret.
    absent_bot = MagicMock(spec=discord.Client)
    absent_bot.get_guild = MagicMock(return_value=None)

    with caplog.at_level(logging.CRITICAL, logger="mom_bot.reminders.seed"):
        with patch(
            "mom_bot.reminders.seed.load_secret",
            side_effect=_secret_side_effect,
        ):
            with pytest.raises(Exception) as exc_info:
                _maybe_seed_reminders(session, absent_bot)

    # Verify exception type by name to survive module-reload class divergence.
    assert exc_info.type.__name__ == "ConfigError"
    critical_records = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(critical_records) >= 1
