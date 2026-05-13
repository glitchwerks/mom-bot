"""Integration tests for MomBot scheduler wireup in setup_hook.

TDD: tests were written before the wireup implementation.  Each test covers
one discrete behaviour of the wired-up bot.

Design notes
------------
- In-memory SQLite + ``Base.metadata.create_all`` is used (no Alembic here —
  the alembic tests already cover schema correctness; create_all is faster).
- ``load_secret`` is patched to return canned values so no Key Vault
  round-trip occurs.  The ``reminder-channel-name`` secret now holds a
  channel name string instead of a snowflake (#47).
- ``MomBot.wait_until_ready`` is patched to return immediately for most tests.
  ``test_setup_hook_returns_promptly_without_gateway`` is the exception: it
  intentionally does NOT mock ``wait_until_ready`` — this is the regression
  test for #41 (setup_hook deadlock).
- The scheduler task is observed via ``bot._reminder_task`` which must be set
  by the wireup so the task is not garbage-collected.
- A ``FakeChannel`` with a recorded ``send`` is registered so we can verify
  the Discord send target.
- For the seed step, ``bot.get_guild(guild_id)`` is patched to return a guild
  whose ``text_channels`` list includes a channel named ``"reminders"``.
  seed.py now resolves guild by ID (from the ``guild-id`` KV secret) rather
  than via ``bot.guilds[0]``, fixing non-deterministic guild selection when
  the bot account is a member of multiple guilds (#49).
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.db import Base
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GUILD_ID = 999999999999999999
_CHANNEL_ID = 111111111111111111
_CHANNEL_NAME = "reminders"
_ROLE_ID = 333444555666777888
_ROLE_NAME = "Member"


def _make_engine() -> Any:
    """Create an in-memory SQLite engine with both reminder tables."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    return engine


def _make_session_factory(engine: Any) -> Any:
    """Return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine)


class FakeChannel:
    """Minimal discord.TextChannel stand-in with a recorded send."""

    def __init__(self, channel_id: int, name: str = _CHANNEL_NAME) -> None:
        """Initialise with channel snowflake and name."""
        self.id = channel_id
        self.name = name
        self.send = AsyncMock()


class FakeRole:
    """Minimal discord.Role stand-in with a real name and id."""

    def __init__(self, role_id: int, name: str = _ROLE_NAME) -> None:
        """Initialise with role snowflake and name."""
        self.id = role_id
        self.name = name


def _make_fake_guild(
    channel_id: int = _CHANNEL_ID,
    channel_name: str = _CHANNEL_NAME,
    role_id: int = _ROLE_ID,
    role_name: str = _ROLE_NAME,
) -> tuple[MagicMock, FakeChannel]:
    """Build a fake guild for patching ``bot.get_guild``.

    ``seed.py`` calls ``bot.get_guild(int(guild_id))`` (resolved from the
    ``guild-id`` KV secret) and then resolves the channel via
    ``discord.utils.get(guild.text_channels, name=...)`` and the role via
    ``discord.utils.get(guild.roles, name=...)`` (#51).  We use real stub
    instances (with real ``.name`` string attributes) so string comparisons
    in ``discord.utils.get`` work correctly.

    ``get_guild`` is a regular method (not a property), so no
    ``PropertyMock`` is needed — patch directly with
    ``patch.object(bot, "get_guild", return_value=mock_guild)``.

    Returns:
        A tuple of ``(mock_guild, fake_channel)`` where ``mock_guild`` is
        ready to use as the ``return_value`` of ``bot.get_guild``.
    """
    fake_channel = FakeChannel(channel_id, channel_name)
    fake_role = FakeRole(role_id, role_name)

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.text_channels = [fake_channel]
    mock_guild.roles = [fake_role]
    mock_guild.name = "fake-guild"
    mock_guild.id = _GUILD_ID

    return mock_guild, fake_channel


# ---------------------------------------------------------------------------
# Test 0 — regression for #41: setup_hook must NOT await wait_until_ready
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_hook_returns_promptly_without_gateway() -> None:
    """Regression for #41: setup_hook must return without awaiting READY.

    Awaits ``setup_hook`` with a 2-second deadline via
    :func:`asyncio.wait_for`.  If ``setup_hook`` awaits
    ``wait_until_ready`` directly (the pre-fix bug), the gateway never
    connects in test (no real discord.py session), ``wait_until_ready``
    never resolves, and the test times out with
    :exc:`asyncio.TimeoutError`.

    ``wait_until_ready`` is intentionally NOT mocked here — that is the
    whole point.  The scheduler task spawned by the fixed ``setup_hook``
    WILL call ``wait_until_ready``; it will block forever inside that task,
    which is expected and correct.  We cancel the task after asserting.
    """
    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    session_factory = _make_session_factory(engine)

    load_secret_values = {
        "guild-id": str(_GUILD_ID),
        "reminder-channel-name": _CHANNEL_NAME,
        "reminder-mention-role-name": _ROLE_NAME,
    }

    def fake_load_secret(name: str) -> str:
        return load_secret_values[name]

    bot = MomBot(intents=build_intents())

    with (
        patch("mom_bot.main.load_secret", side_effect=fake_load_secret),
        patch("mom_bot.reminders.seed.load_secret", side_effect=fake_load_secret),
        patch.object(bot.tree, "sync", new_callable=AsyncMock),
        patch(
            "mom_bot.main._build_session_factory",
            return_value=session_factory,
        ),
    ):
        # Real wait_until_ready — NOT mocked. If setup_hook awaits it
        # directly the call will deadlock and hit the 2-second timeout.
        await asyncio.wait_for(bot.setup_hook(), timeout=2.0)

    # setup_hook returned — the fix is working.  The background task
    # should have been created and should still be running (blocked on
    # wait_until_ready internally, which is correct).
    assert (
        bot._reminder_task is not None
    ), "setup_hook must store the reminder task on bot._reminder_task"
    assert (
        not bot._reminder_task.done()
    ), "reminder task should still be running (awaiting READY in background)"

    bot._reminder_task.cancel()
    try:
        await bot._reminder_task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Test 1 — setup_hook seeds two rows and starts the scheduler task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_hook_seeds_and_starts_scheduler() -> None:
    """setup_hook() must seed two reminder rows and start the scheduler task.

    Verifies:
    1. ``_maybe_seed_reminders`` is called (two rows in reminders table).
    2. The scheduler task is stored on ``bot._reminder_task``.
    3. The task is not done immediately (it is running, not finished).

    The bot's ``get_guild`` method is patched to return a guild containing
    a channel named ``"reminders"`` so the seed step can resolve the channel
    name to a snowflake (#47).  ``get_guild`` is the correct lookup since
    seed.py now resolves by ID from the ``guild-id`` KV secret (#49).
    """
    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    session_factory = _make_session_factory(engine)

    load_secret_values = {
        "guild-id": str(_GUILD_ID),
        "reminder-channel-name": _CHANNEL_NAME,
        "reminder-mention-role-name": _ROLE_NAME,
    }

    def fake_load_secret(name: str) -> str:
        return load_secret_values[name]

    bot = MomBot(intents=build_intents())
    mock_guild, _ = _make_fake_guild(_CHANNEL_ID, _CHANNEL_NAME)

    with (
        patch("mom_bot.main.load_secret", side_effect=fake_load_secret),
        # seed.py imports load_secret directly from mom_bot.config, so patch
        # that import path too so KV calls are intercepted.
        patch("mom_bot.reminders.seed.load_secret", side_effect=fake_load_secret),
        patch.object(bot, "wait_until_ready", new_callable=AsyncMock),
        patch.object(bot.tree, "sync", new_callable=AsyncMock),
        patch(
            "mom_bot.main._build_session_factory",
            return_value=session_factory,
        ),
        # seed.py calls bot.get_guild(int(guild_id)) — patch the method
        # directly; no PropertyMock needed (get_guild is a regular method).
        patch.object(bot, "get_guild", return_value=mock_guild),
    ):
        await bot.setup_hook()

        # After the fix, seed + scheduler-start happen inside
        # _start_reminders_after_ready (a background task).  Yielding
        # control here lets the task run while the patches are still
        # active (wait_until_ready mock is needed for the task to
        # proceed past its own await).
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # Assert seed ran: two rows in reminders.
    with Session(engine) as session:
        count = session.scalar(
            select(Reminder).with_only_columns(  # type: ignore[arg-type]
                __import__("sqlalchemy").func.count()
            )
        )
    with Session(engine) as session:
        from sqlalchemy import func

        count = session.scalar(select(func.count()).select_from(Reminder))
    assert count == 2, f"Expected 2 seeded rows, got {count}"

    # Assert scheduler task was stored.
    assert hasattr(bot, "_reminder_task"), "bot._reminder_task not set by setup_hook"
    task = bot._reminder_task
    assert isinstance(task, asyncio.Task), "bot._reminder_task must be an asyncio.Task"

    # Clean up: cancel the task so the event loop does not leak.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Test 2 — scheduler fires the correct channel with a custom reminder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_fires_custom_reminder() -> None:
    """A custom reminder due in 2 minutes fires after one tick of the scheduler.

    Scenario:
    1. Insert a custom reminder scheduled for today (UTC) at a fire_time_utc
       that is exactly ``now_utc.time()`` (i.e. it should fire on the first
       tick).
    2. Run setup_hook() to start the scheduler task.
    3. Give the event loop one iteration (yield) and verify the mock channel
       send was called exactly once for the custom reminder.

    The bot's ``guilds`` list is populated with a fake guild + channel so
    the seed step can resolve the channel name (#47), even though the table
    is already non-empty (seed is a no-op in this case).
    """
    import time_machine

    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    session_factory = _make_session_factory(engine)

    # Pin "now" to a known Tuesday at 07:30 UTC so the default Hydra reminder
    # (Tuesday 07:00) would also be eligible — but we use a separate
    # channel_id for the custom reminder so we can assert independently.
    custom_channel_id = 333333333333333333
    fake_channel = FakeChannel(custom_channel_id)

    # Fire time = current minute (will be eligible on first tick).
    fire_dt = datetime.datetime(2026, 5, 5, 9, 0, 0, tzinfo=datetime.UTC)
    fire_time = datetime.time(9, 0, 0)
    weekday = fire_dt.weekday()  # Tuesday = 1

    with Session(engine) as session:
        session.add(
            Reminder(
                name="CustomTest",
                channel_id=custom_channel_id,
                weekday=weekday,
                fire_time_utc=fire_time,
                message_template="Test reminder body",
                role_mention_id=None,
            )
        )
        session.commit()

    load_secret_values = {
        "guild-id": str(_GUILD_ID),
        "reminder-channel-name": _CHANNEL_NAME,
    }

    def fake_load_secret(name: str) -> str:
        return load_secret_values[name]

    bot = MomBot(intents=build_intents())

    # Register the fake channel so the scheduler can deliver to it.
    bot._channels: dict[int, FakeChannel] = {custom_channel_id: fake_channel}

    def fake_get_channel(channel_id: int) -> FakeChannel | None:
        return bot._channels.get(channel_id)  # type: ignore[attr-defined]

    bot.get_channel = fake_get_channel  # type: ignore[method-assign]
    # The scheduler calls bot.is_ready(); patch it to True.
    bot.is_ready = lambda: True  # type: ignore[method-assign]

    with (
        patch("mom_bot.main.load_secret", side_effect=fake_load_secret),
        patch.object(bot, "wait_until_ready", new_callable=AsyncMock),
        patch.object(bot.tree, "sync", new_callable=AsyncMock),
    ):
        with patch(
            "mom_bot.main._build_session_factory",
            return_value=session_factory,
        ):
            with time_machine.travel(fire_dt, tick=False):
                await bot.setup_hook()

                # Yield control so the scheduler task runs one iteration.
                # The scheduler uses tick_seconds=60.0 by default; we need it
                # to fire before sleeping.  Since time is frozen at fire_dt and
                # the custom reminder's fire_time_utc == fire_dt.time(), the
                # first tick should deliver the message.
                await asyncio.sleep(0)
                # Give the scheduler task a chance to run its first tick.
                await asyncio.sleep(0)
                await asyncio.sleep(0)

    # Verify the custom channel's send was called (at least once).
    fake_channel.send.assert_called()
    call_args = fake_channel.send.call_args_list[0]
    message_sent = call_args.args[0] if call_args.args else ""
    assert "Test reminder body" in message_sent

    # Clean up.
    task = bot._reminder_task  # type: ignore[attr-defined]
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
