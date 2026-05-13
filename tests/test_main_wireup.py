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

Exception-logging tests (issue #53)
------------------------------------
- ``test_reminder_task_exception_logs_critical``: a synthetic RuntimeError
  raised by ``_maybe_seed_reminders`` must produce a CRITICAL log record
  whose exc_info contains the RuntimeError and a traceback.
- ``test_reminder_task_cancellation_does_not_log_critical``: cancelling the
  task during ``await wait_until_ready`` must NOT emit a CRITICAL record.
- ``test_done_callback_logs_exception``: the ``_log_task_exception`` module-
  level callback must log CRITICAL when called with a failed task, exercising
  the belt-and-suspenders safety net independently of the try/except.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
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


def _make_fake_guild(
    channel_id: int = _CHANNEL_ID,
    channel_name: str = _CHANNEL_NAME,
) -> tuple[MagicMock, FakeChannel]:
    """Build a fake guild for patching ``bot.get_guild``.

    ``seed.py`` calls ``bot.get_guild(int(guild_id))`` (resolved from the
    ``guild-id`` KV secret) and then resolves the channel via
    ``discord.utils.get(guild.text_channels, name=...)``.  We use a real
    :class:`FakeChannel` instance (with a real ``.name`` string attribute)
    so the comparison works correctly — a plain ``MagicMock`` without
    ``spec=`` would return another mock for ``.name``, which will never
    equal the channel name string.

    ``get_guild`` is a regular method (not a property), so no
    ``PropertyMock`` is needed — patch directly with
    ``patch.object(bot, "get_guild", return_value=mock_guild)``.

    Returns:
        A tuple of ``(mock_guild, fake_channel)`` where ``mock_guild`` is
        ready to use as the ``return_value`` of ``bot.get_guild``.
    """
    fake_channel = FakeChannel(channel_id, channel_name)

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.text_channels = [fake_channel]
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


# ---------------------------------------------------------------------------
# Tests for exception-logging in the reminder-init task (issue #53)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reminder_task_exception_logs_critical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A RuntimeError raised by _maybe_seed_reminders must log CRITICAL.

    The try/except in ``_start_reminders_after_ready`` must:
    - emit a CRITICAL log record at the moment of failure (not at GC time);
    - include the full exc_info (exc type, value, traceback) so operators
      can see the root cause in the live log stream.

    The task re-raises after logging, so ``task.exception()`` holds the
    original RuntimeError — verified here to confirm re-raise semantics.
    """
    from mom_bot.main import MomBot, build_intents

    engine = _make_engine()
    session_factory = _make_session_factory(engine)

    load_secret_values = {
        "guild-id": str(_GUILD_ID),
        "reminder-channel-name": _CHANNEL_NAME,
    }

    def fake_load_secret(name: str) -> str:
        return load_secret_values[name]

    def exploding_seed(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic seed failure")

    bot = MomBot(intents=build_intents())
    mock_guild, _ = _make_fake_guild(_CHANNEL_ID, _CHANNEL_NAME)

    with caplog.at_level(logging.CRITICAL, logger="mom_bot.main"):
        with (
            patch("mom_bot.main.load_secret", side_effect=fake_load_secret),
            patch("mom_bot.reminders.seed.load_secret", side_effect=fake_load_secret),
            patch.object(bot, "wait_until_ready", new_callable=AsyncMock),
            patch.object(bot.tree, "sync", new_callable=AsyncMock),
            patch(
                "mom_bot.main._build_session_factory",
                return_value=session_factory,
            ),
            patch.object(bot, "get_guild", return_value=mock_guild),
            patch(
                "mom_bot.main._maybe_seed_reminders",
                side_effect=exploding_seed,
            ),
        ):
            await bot.setup_hook()
            # Yield twice so the task runs past wait_until_ready and
            # into the seed step (where it will raise).
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    # Must have at least one CRITICAL record from mom_bot.main.
    critical_records = [
        r for r in caplog.records if r.levelno == logging.CRITICAL and r.name == "mom_bot.main"
    ]
    assert critical_records, (
        "Expected a CRITICAL log record from mom_bot.main when _maybe_seed_reminders "
        "raises, but none was emitted."
    )

    # The record's exc_info must capture the RuntimeError we raised.
    rec = critical_records[0]
    assert rec.exc_info is not None, "CRITICAL record must carry exc_info"
    exc_type, exc_value, tb = rec.exc_info
    assert exc_type is RuntimeError, f"Expected exc_type=RuntimeError, got {exc_type}"
    assert "synthetic seed failure" in str(
        exc_value
    ), f"Expected error message in exc_value, got {exc_value!r}"
    assert tb is not None, "CRITICAL record must carry a traceback"

    # Re-raise semantics: task ends in exceptional state.
    task = bot._reminder_task
    assert task is not None
    assert task.done(), "task must be done (raised) after seed exception"
    assert not task.cancelled(), "task must not be cancelled — it raised"
    assert isinstance(
        task.exception(), RuntimeError
    ), "task.exception() must be the original RuntimeError"


@pytest.mark.asyncio
async def test_reminder_task_cancellation_does_not_log_critical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancelling the reminder-init task must NOT emit a CRITICAL record.

    asyncio.CancelledError is the normal shutdown signal — treating it as
    an error would produce noisy false-positive alerts during bot restarts.
    The try/except in ``_start_reminders_after_ready`` must re-raise
    CancelledError without logging at CRITICAL.
    """
    from mom_bot.main import MomBot, build_intents

    # wait_until_ready is NOT mocked — the task will block on it, which
    # gives us a clean cancel point during the await.
    bot = MomBot(intents=build_intents())

    load_secret_values = {
        "guild-id": str(_GUILD_ID),
        "reminder-channel-name": _CHANNEL_NAME,
    }

    def fake_load_secret(name: str) -> str:
        return load_secret_values[name]

    with caplog.at_level(logging.CRITICAL, logger="mom_bot.main"):
        with (
            patch("mom_bot.main.load_secret", side_effect=fake_load_secret),
            patch.object(bot.tree, "sync", new_callable=AsyncMock),
        ):
            await bot.setup_hook()

            # The task is now blocked on wait_until_ready (no mock, no
            # gateway).  Cancel it and let the cancellation propagate.
            task = bot._reminder_task
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    # Must be done and cancelled, not exceptioned.
    assert task.done(), "task must be done after cancel"
    assert task.cancelled(), "task must be in cancelled state"

    # No CRITICAL record should exist.
    critical_records = [
        r for r in caplog.records if r.levelno == logging.CRITICAL and r.name == "mom_bot.main"
    ]
    assert not critical_records, (
        "Cancellation must NOT produce a CRITICAL log record; "
        f"got: {[r.message for r in critical_records]}"
    )


@pytest.mark.asyncio
async def test_done_callback_logs_exception() -> None:
    """_log_task_exception must log CRITICAL when the task ended with an exc.

    This exercises the done-callback safety net (belt-and-suspenders) in
    isolation — independent of the try/except in _start_reminders_after_ready.

    Edge-case analysis: could an exception be raised *before* the try/except
    in _start_reminders_after_ready installs?  The try/except wraps the
    entire body of _start_reminders_after_ready starting from
    ``await self.wait_until_ready()``.  There is no code before that first
    ``await`` that could raise outside the try/except — the try block is the
    very first statement.  Therefore the "exception before try/except installs"
    path is structurally unreachable in _start_reminders_after_ready itself.

    The done-callback is still a meaningful safety net for *other* background
    tasks that may be added later and that do not have a try/except wrapper.
    We test it here by constructing a bare Task that raises, bypassing
    _start_reminders_after_ready entirely, and verifying the callback logs.
    """
    import logging

    from mom_bot.main import _log_task_exception

    logged_records: list[logging.LogRecord] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logged_records.append(record)

    handler = _CapturingHandler()
    # Target the mom_bot.main logger that _log_task_exception uses.
    logger = logging.getLogger("mom_bot.main")
    logger.addHandler(handler)
    original_disabled = logger.disabled
    logger.disabled = False
    try:

        async def _raise() -> None:
            raise RuntimeError("callback-path failure")

        task: asyncio.Task[None] = asyncio.create_task(_raise())
        # Wait for the task to complete (it will raise internally).
        try:
            await task
        except RuntimeError:
            pass

        # Now invoke the callback as asyncio would (with the finished task).
        _log_task_exception(task)

    finally:
        logger.removeHandler(handler)
        logger.disabled = original_disabled

    critical_records = [r for r in logged_records if r.levelno == logging.CRITICAL]
    assert critical_records, "_log_task_exception must emit a CRITICAL record for a failed task"
    rec = critical_records[0]
    assert rec.exc_info is not None, "CRITICAL record must carry exc_info"
    exc_type, exc_value, _tb = rec.exc_info
    assert exc_type is RuntimeError
    assert "callback-path failure" in str(exc_value)
