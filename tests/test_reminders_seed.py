"""Tests for _maybe_seed_reminders.

Verifies idempotent seed-on-boot from Key Vault values, including
guild resolution via the per-env ``guild-id`` KV secret (#49),
channel-name-to-snowflake resolution via the discord.py client (#47),
and role-name-to-snowflake resolution (#51).
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
_ROLE_NAME = "Member"
# Discord snowflakes are 18-19 digit integers; SQLite stores INTEGER as a
# signed 64-bit value (max ~9.2e18).  Use values safely within that range.
_CHANNEL_ID = 987654321098765432
_ROLE_ID = 111222333444555666
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
    iterates ``text_channels`` / ``roles`` and compares ``.name`` by value.
    Without ``spec=``, ``mock.name`` returns a fresh ``MagicMock`` that
    will never string-equal the expected name.

    The bot resolves guild by ID via ``bot.get_guild(int(guild_id))``, so
    we set ``bot.get_guild`` to return ``mock_guild`` when called with the
    expected integer ID (#49).  The old ``bot.guilds = [mock_guild]`` pattern
    is replaced — ``bot.guilds[0]`` was non-deterministic in multi-guild bots.

    The guild exposes both ``text_channels`` (for channel resolution, #47)
    and ``roles`` (for role resolution, #51).
    """
    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.name = _CHANNEL_NAME
    mock_channel.id = _CHANNEL_ID

    mock_role = MagicMock(spec=discord.Role)
    mock_role.name = _ROLE_NAME
    mock_role.id = _ROLE_ID

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.text_channels = [mock_channel]
    mock_guild.roles = [mock_role]
    mock_guild.name = _GUILD_NAME
    mock_guild.id = _GUILD_ID

    bot = MagicMock(spec=discord.Client)
    bot.get_guild = MagicMock(return_value=mock_guild)
    return bot


def _secret_side_effect(name: str) -> str:
    """Return a fake value for each expected KV secret name.

    Covers ``guild-id`` (guild resolution, #49),
    ``reminder-channel-name`` (channel resolution, #47), and
    ``reminder-mention-role-name`` (role resolution, #51).
    """
    secrets = {
        "guild-id": str(_GUILD_ID),
        "reminder-channel-name": _CHANNEL_NAME,
        "reminder-mention-role-name": _ROLE_NAME,
    }
    if name not in secrets:
        raise KeyError(f"Unexpected secret: {name!r}")
    return secrets[name]


# ---------------------------------------------------------------------------
# Seed-on-boot idempotency
# ---------------------------------------------------------------------------


def test_seed_empty_table_inserts_hydra_and_chimera(session: Session, mock_bot: MagicMock) -> None:
    """Empty table + valid KV + matching channel + matching role → two rows.

    Both rows share the resolved ``channel_id`` (snowflake from
    ``discord.utils.get`` on text_channels, #47) and have a non-NULL
    ``role_mention_id`` equal to the mocked role's snowflake (#51).

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
    # role_mention_id must be resolved to the mocked role's snowflake (#51).
    assert hydra.role_mention_id is not None
    assert hydra.role_mention_id == _ROLE_ID

    chimera = session.execute(select(Reminder).where(Reminder.name == "Chimera")).scalar_one()
    assert chimera.weekday == 2
    assert chimera.fire_time_utc == datetime.time(12, 0, 0)
    assert chimera.channel_id == _CHANNEL_ID
    # role_mention_id must be resolved to the mocked role's snowflake (#51).
    assert chimera.role_mention_id is not None
    assert chimera.role_mention_id == _ROLE_ID

    # Both rows share the single resolved channel — verify equality.
    assert hydra.channel_id == chimera.channel_id
    # Both rows share the single resolved role — verify equality.
    assert hydra.role_mention_id == chimera.role_mention_id


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


# ---------------------------------------------------------------------------
# Role resolution (#51) — mirrors channel-not-found tests above
# ---------------------------------------------------------------------------


def test_seed_role_mention_id_equals_mocked_role_snowflake(
    session: Session,
    mock_bot: MagicMock,
) -> None:
    """Resolved role snowflake is stored in role_mention_id on both rows.

    Specifically verifies the resolved value equals the mocked role's
    ``.id`` attribute (not just any non-None value), mirroring the channel
    test that checks ``channel_id == _CHANNEL_ID`` (#47).
    """
    with patch(
        "mom_bot.reminders.seed.load_secret",
        side_effect=_secret_side_effect,
    ):
        _maybe_seed_reminders(session, mock_bot)

    rows = session.execute(select(Reminder)).scalars().all()
    assert len(rows) == 2
    for row in rows:
        assert (
            row.role_mention_id is not None
        ), f"Expected role_mention_id to be resolved for {row.name!r}, got None"
        assert (
            row.role_mention_id == _ROLE_ID
        ), f"Expected role_mention_id={_ROLE_ID}, got {row.role_mention_id}"


def test_seed_role_kv_secret_missing_logs_critical_and_raises(
    session: Session,
    mock_bot: MagicMock,
) -> None:
    """KV missing reminder-mention-role-name → CRITICAL logged + exception.

    Mirrors ``test_seed_kv_failure_logs_critical_and_raises`` for the new
    role-name secret.  The error must fire before any DB rows are written.
    """
    error = RuntimeError("KV unreachable")

    captured: list[logging.LogRecord] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _CapturingHandler()
    seed_logger = logging.getLogger("mom_bot.reminders.seed")
    seed_logger.disabled = False
    seed_logger.addHandler(handler)
    seed_logger.setLevel(logging.DEBUG)

    def _secret_missing_role(name: str) -> str:
        """Succeed for guild-id and channel-name; fail for role-name."""
        if name == "guild-id":
            return str(_GUILD_ID)
        if name == "reminder-channel-name":
            return _CHANNEL_NAME
        raise error

    try:
        with patch(
            "mom_bot.reminders.seed.load_secret",
            side_effect=_secret_missing_role,
        ):
            with pytest.raises(RuntimeError, match="KV unreachable"):
                _maybe_seed_reminders(session, mock_bot)
    finally:
        seed_logger.removeHandler(handler)

    # At least one CRITICAL log must have been emitted.
    critical_records = [r for r in captured if r.levelname == "CRITICAL"]
    assert len(critical_records) >= 1

    # No rows should have been inserted.
    count = session.scalar(select(func.count(Reminder.id)))
    assert count == 0


def test_seed_role_not_found_in_guild_logs_critical_and_raises(
    session: Session,
    mock_bot: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """discord.utils.get(guild.roles, name=...) returns None → ConfigError.

    The mock bot's guild has a role named ``_ROLE_NAME`` by default.  This
    test overrides the KV secret to return a name that does not match any
    guild role, causing the resolver to raise ConfigError and log CRITICAL.
    No rows should be inserted.

    Mirrors ``test_seed_channel_not_found_logs_critical_and_raises`` for
    role resolution (#51).
    """

    def _secret_bad_role(name: str) -> str:
        if name == "guild-id":
            return str(_GUILD_ID)
        if name == "reminder-channel-name":
            return _CHANNEL_NAME
        # Return a non-existent role name.
        return "nonexistent-role"

    with caplog.at_level(logging.CRITICAL, logger="mom_bot.reminders.seed"):
        with patch(
            "mom_bot.reminders.seed.load_secret",
            side_effect=_secret_bad_role,
        ):
            with pytest.raises(Exception) as exc_info:
                _maybe_seed_reminders(session, mock_bot)

    # Verify exception type by name to survive module-reload class divergence.
    assert exc_info.type.__name__ == "ConfigError"
    critical_records = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(critical_records) >= 1

    # No rows should have been inserted.
    count = session.scalar(select(func.count(Reminder.id)))
    assert count == 0


# ---------------------------------------------------------------------------
# KV secret value validation — ValueError vs ConfigError distinction (#40)
# ---------------------------------------------------------------------------


def test_seed_guild_id_non_numeric_raises_config_error_not_failed_to_load(
    session: Session,
    mock_bot: MagicMock,
) -> None:
    """guild-id KV loads OK but value is non-numeric → ConfigError, not
    the generic 'failed to load' wording.

    Distinguishes two failure modes for ``int(load_secret("guild-id"))``:
    1. ``load_secret`` itself raises → "failed to load" message (existing).
    2. ``int(...)`` raises ``ValueError`` → secret WAS loaded; data is bad.
       The error message must name the secret and the bad value, NOT claim
       the secret could not be loaded (PR #40 feedback).
    """
    captured: list[logging.LogRecord] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _CapturingHandler()
    seed_logger = logging.getLogger("mom_bot.reminders.seed")
    seed_logger.disabled = False
    seed_logger.addHandler(handler)
    seed_logger.setLevel(logging.DEBUG)

    bad_value = "not-a-snowflake"

    def _secret_bad_guild_id(name: str) -> str:
        if name == "guild-id":
            return bad_value
        return _secret_side_effect(name)

    try:
        with patch(
            "mom_bot.reminders.seed.load_secret",
            side_effect=_secret_bad_guild_id,
        ):
            with pytest.raises(Exception) as exc_info:
                _maybe_seed_reminders(session, mock_bot)
    finally:
        seed_logger.removeHandler(handler)

    # Must raise ConfigError, not ValueError.
    assert exc_info.type.__name__ == "ConfigError"

    # The message must name the secret and the bad value.
    error_str = str(exc_info.value)
    assert "guild-id" in error_str
    assert bad_value in error_str

    # The message must NOT use the "failed to load" wording (that phrase
    # belongs to the KV-unreachable path, not the bad-value path).
    assert "failed to load" not in error_str.lower()

    # A CRITICAL log must have been emitted.
    critical_records = [r for r in captured if r.levelname == "CRITICAL"]
    assert len(critical_records) >= 1

    # No rows should have been inserted.
    count = session.scalar(select(func.count(Reminder.id)))
    assert count == 0
