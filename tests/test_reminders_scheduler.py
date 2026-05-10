"""Tests for ReminderScheduler.

Covers cold-start ordering, mid-day reconnects, real-tick smoke,
SIGTERM shutdown, revision-rollout overlap, Discord error taxonomy,
insert-collision short-circuit, fire-time predicate, and UTC-midnight
date-attribution.

Design notes
------------
- A custom ``FakeBot`` class replaces ``discord.Client`` in all tests so
  no gateway connection is attempted.
- ``FakeChannel`` replaces ``discord.TextChannel``; its ``send`` method
  is a coroutine that records calls and can be configured to raise.
- In-memory SQLite + ``Base.metadata.create_all`` is used for all DB
  fixtures (no Alembic in tests).
- ``time-machine`` is used for fake-clock tests where UTC datetime must be
  controlled without real wall-clock waits.
- The ``tick_seconds`` constructor parameter is injected with small values
  in tests to keep runtimes short.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
import time_machine
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.db import Base
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401
from mom_bot.reminders.scheduler import ReminderScheduler

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeChannel:
    """Minimal stand-in for a discord.TextChannel."""

    def __init__(self, channel_id: int) -> None:
        """Initialise with the channel snowflake."""
        self.id = channel_id
        self.send = AsyncMock()


class FakeBot:
    """Minimal stand-in for discord.Client.

    Controls ``is_ready()`` and ``get_channel()`` without a gateway.
    """

    def __init__(self, ready: bool = True) -> None:
        """Initialise with a readiness flag and a channel registry."""
        self._ready = ready
        self._channels: dict[int, FakeChannel] = {}

    def is_ready(self) -> bool:
        """Return the current readiness state."""
        return self._ready

    def set_ready(self, value: bool) -> None:
        """Flip the readiness flag (used in mid-day reconnect tests)."""
        self._ready = value

    def add_channel(self, channel: FakeChannel) -> None:
        """Register a fake channel so get_channel can find it."""
        self._channels[channel.id] = channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        """Return a registered fake channel by id."""
        return self._channels.get(channel_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CHANNEL_ID = 111111111111111111
_ROLE_ID = 222222222222222222

# Tuesday 07:00 UTC = Hydra fire-time; weekday=1
_TUESDAY_0700 = datetime.datetime(2026, 5, 5, 7, 0, 0, tzinfo=datetime.UTC)


def _make_session_factory(engine: Any) -> Any:
    """Return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine)


def _make_engine() -> Any:
    """Create an in-memory SQLite engine with both tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _seed_reminder(
    session: Session,
    *,
    weekday: int = 1,
    fire_time_utc: datetime.time = datetime.time(7, 0, 0),
    channel_id: int = _CHANNEL_ID,
    role_mention_id: int | None = _ROLE_ID,
    name: str = "Hydra",
) -> Reminder:
    """Insert a Reminder row and return it."""
    reminder = Reminder(
        name=name,
        channel_id=channel_id,
        weekday=weekday,
        fire_time_utc=fire_time_utc,
        message_template="Test message",
        role_mention_id=role_mention_id,
    )
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def _make_scheduler(
    bot: FakeBot,
    engine: Any,
    tick_seconds: float = 0.05,
) -> ReminderScheduler:
    """Convenience factory for a ReminderScheduler with a fast tick."""
    return ReminderScheduler(
        bot=bot,  # type: ignore[arg-type]
        session_factory=_make_session_factory(engine),
        tick_seconds=tick_seconds,
    )


# ---------------------------------------------------------------------------
# Cold-start regression (plan § 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_start_no_send_before_ready() -> None:
    """Scheduler skips tick and does not send if bot is not ready."""
    engine = _make_engine()
    with Session(engine) as s:
        reminder = _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=False)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)  # let two ticks pass with ready=False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    channel.send.assert_not_called()
    # Also verify no reminder_sent row was written.
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 0

    # Suppress unused variable warning.
    _ = reminder


@pytest.mark.asyncio
async def test_cold_start_sends_after_ready() -> None:
    """After bot becomes ready, the next tick fires the reminder."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=False)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)  # one tick; still not ready
        bot.set_ready(True)
        await asyncio.sleep(0.08)  # second tick fires
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    channel.send.assert_called_once()


# ---------------------------------------------------------------------------
# Mid-day reconnect (plan § 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_day_reconnect_skips_tick_when_not_ready() -> None:
    """Mid-loop not-ready guard: tick is skipped, no insert, no send."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)  # first tick fires (ready)
        assert channel.send.call_count == 1

        # Simulate gateway disconnect.
        bot.set_ready(False)
        await asyncio.sleep(0.08)  # second tick skipped (not ready)
        assert channel.send.call_count == 1  # no second send

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_mid_day_reconnect_resumes_after_ready() -> None:
    """After reconnect, a new (different) reminder fires on next tick."""
    engine = _make_engine()
    with Session(engine) as s:
        # Reminder at 07:00 UTC fires first.
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday(), name="Hydra")
        # Second reminder at 08:00 UTC fires later.
        _seed_reminder(
            s,
            weekday=_TUESDAY_0700.weekday(),
            fire_time_utc=datetime.time(8, 0, 0),
            channel_id=_CHANNEL_ID,
            name="Chimera",
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    # Start at 07:00 — Hydra fires.
    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)
        assert channel.send.call_count == 1  # Hydra fired

        # Disconnect, then advance to 08:00 — skip one tick.
        bot.set_ready(False)
        await asyncio.sleep(0.08)
        assert channel.send.call_count == 1  # still 1

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Advance to 08:00 and reconnect — Chimera should fire.
    _0800 = _TUESDAY_0700 + datetime.timedelta(hours=1)
    with time_machine.travel(_0800, tick=False):
        task2 = asyncio.create_task(scheduler.run())
        bot.set_ready(True)
        await asyncio.sleep(0.08)
        task2.cancel()
        try:
            await task2
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 2  # Hydra + Chimera


# ---------------------------------------------------------------------------
# Real-tick smoke (plan § 6) — uses wall-clock time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_tick_smoke() -> None:
    """Scheduler fires once after fire-time passes and does not re-fire.

    Uses tick_seconds=0.1 and time_machine to advance 200ms past the
    fire-time boundary.  The fire_time_utc is set to a minute boundary
    one minute in the future, then time_machine jumps past it so the
    second tick fires.  The third tick sees the UNIQUE row and does not
    re-send.

    This tests the real tick-cycle semantics: t=0 (before fire-time) →
    no send; t=1 (after fire-time) → send once; t=2 → no re-send.
    """
    # Anchor: Tuesday 07:00 UTC — fire_time at 07:01 UTC.
    start = datetime.datetime(2026, 5, 5, 7, 0, 0, tzinfo=datetime.UTC)
    fire_at = datetime.time(7, 1, 0)  # minute boundary, seconds=0

    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(
            s,
            weekday=start.weekday(),
            fire_time_utc=fire_at,
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.1)

    # Phase 1: sit at 07:00 — fire_time (07:01) is not yet reached.
    with time_machine.travel(start, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.15)  # one tick
        assert channel.send.call_count == 0  # not fired yet
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Phase 2: advance to 07:01 — fire_time reached; fires once.
    after_fire = start + datetime.timedelta(minutes=1)
    with time_machine.travel(after_fire, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.15)  # one tick
        assert channel.send.call_count == 1  # fired exactly once
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Phase 3: still at 07:01 — UNIQUE constraint blocks re-send.
    with time_machine.travel(after_fire, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.15)  # one tick
        assert channel.send.call_count == 1  # still exactly once
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# SIGTERM during sleep (plan § 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sigterm_during_sleep_shuts_down_cleanly() -> None:
    """task.cancel() while sleeping resolves within 100ms."""
    engine = _make_engine()
    bot = FakeBot(ready=True)
    scheduler = _make_scheduler(bot, engine, tick_seconds=10.0)

    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)  # let it reach asyncio.sleep(10)
    task.cancel()

    try:
        await asyncio.wait_for(task, timeout=0.2)
    except asyncio.CancelledError:
        pass
    # If we get here, shutdown completed within 0.2s — test passes.
    assert task.done()


# ---------------------------------------------------------------------------
# Revision-rollout overlap (defensive, plan § 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revision_rollout_exactly_one_send() -> None:
    """Two concurrent scheduler instances send the reminder exactly once."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    bot1 = FakeBot(ready=True)
    bot1.add_channel(channel)
    bot2 = FakeBot(ready=True)
    bot2.add_channel(channel)

    scheduler1 = _make_scheduler(bot1, engine, tick_seconds=0.05)
    scheduler2 = _make_scheduler(bot2, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task1 = asyncio.create_task(scheduler1.run())
        task2 = asyncio.create_task(scheduler2.run())
        await asyncio.sleep(0.12)
        task1.cancel()
        task2.cancel()
        for t in [task1, task2]:
            try:
                await t
            except asyncio.CancelledError:
                pass

    # Exactly one send across both schedulers.
    assert channel.send.call_count == 1


# ---------------------------------------------------------------------------
# Discord error taxonomy (plan § 5) — one test per row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_forbidden_row_stays_no_retry() -> None:
    """discord.Forbidden → row stays, no re-send on second tick."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = discord.Forbidden(
        MagicMock(status=403, reason="Missing Access"), "test"
    )
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.18)  # three ticks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # send was attempted once; subsequent ticks see UNIQUE collision.
    assert channel.send.call_count == 1
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_discord_not_found_row_stays_no_retry() -> None:
    """discord.NotFound → row stays, no re-send on second tick."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = discord.NotFound(
        MagicMock(status=404, reason="Unknown Channel"), "test"
    )
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.18)  # three ticks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 1
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_discord_http_5xx_row_deleted_retried() -> None:
    """discord.HTTPException with status>=500 → row deleted, retry next tick."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    # First call raises 503; second call succeeds.
    channel.send.side_effect = [
        discord.HTTPException(MagicMock(status=503, reason="Service Unavailable"), "test"),
        None,
    ]
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.18)  # three ticks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Two send attempts: first failed+retried, second succeeded.
    assert channel.send.call_count == 2
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_discord_rate_limited_treated_as_5xx() -> None:
    """discord.RateLimited → row deleted, retry next tick (treated as 5xx)."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = [
        discord.RateLimited(retry_after=1.0),
        None,
    ]
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.18)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 2
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_aiohttp_client_error_row_deleted_retried() -> None:
    """aiohttp.ClientError → row deleted, retry next tick."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = [
        aiohttp.ClientError("connection reset"),
        None,
    ]
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.18)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 2
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_asyncio_timeout_error_row_deleted_retried() -> None:
    """asyncio.TimeoutError → row deleted, retry next tick."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = [
        TimeoutError(),
        None,
    ]
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.18)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert channel.send.call_count == 2
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1


@pytest.mark.asyncio
async def test_uncaught_exception_row_stays_critical_logged() -> None:
    """Uncaught Exception → row stays, CRITICAL logged, no re-send."""
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(s, weekday=_TUESDAY_0700.weekday())

    channel = FakeChannel(_CHANNEL_ID)
    channel.send.side_effect = ValueError("unexpected error")
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    # Alembic's fileConfig (run by test_alembic.py earlier in the suite)
    # calls logging.config.fileConfig which replaces root logger handlers.
    # Use a direct handler on the module logger to capture records reliably
    # regardless of root handler state.
    captured: list[logging.LogRecord] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _CapturingHandler()
    sched_logger = logging.getLogger("mom_bot.reminders.scheduler")
    # Alembic's fileConfig with disable_existing_loggers=True (the default)
    # can mark mom_bot loggers as disabled if they existed before the call.
    # Force re-enable so the logger emits in this test.
    sched_logger.disabled = False
    sched_logger.addHandler(handler)
    sched_logger.setLevel(logging.DEBUG)

    try:
        with time_machine.travel(_TUESDAY_0700, tick=False):
            task = asyncio.create_task(scheduler.run())
            await asyncio.sleep(0.18)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        sched_logger.removeHandler(handler)

    # Exactly one send attempt; subsequent ticks see UNIQUE collision.
    assert channel.send.call_count == 1
    with Session(engine) as s:
        assert s.query(ReminderSent).count() == 1
    critical_records = [r for r in captured if r.levelname == "CRITICAL"]
    assert len(critical_records) >= 1


# ---------------------------------------------------------------------------
# Insert-collision short-circuit (plan § 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_collision_skips_discord_call() -> None:
    """Pre-existing reminder_sent row → no Discord send on tick."""
    engine = _make_engine()
    today = _TUESDAY_0700.date()

    with Session(engine) as s:
        reminder = _seed_reminder(s, weekday=_TUESDAY_0700.weekday())
        # Pre-insert the idempotency row.
        sent = ReminderSent(reminder_id=reminder.id, fire_date_utc=today)
        s.add(sent)
        s.commit()

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    channel.send.assert_not_called()


# ---------------------------------------------------------------------------
# Fire-time predicate (plan § 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_time_predicate_only_fires_eligible() -> None:
    """Only the reminder matching weekday + fire_time fires; others skip."""
    engine = _make_engine()
    now = _TUESDAY_0700  # Tuesday 07:00 UTC

    with Session(engine) as s:
        # Should fire: weekday=1 (Tuesday), fire_time=07:00 <= 07:00.
        _seed_reminder(
            s,
            weekday=1,
            fire_time_utc=datetime.time(7, 0, 0),
            channel_id=_CHANNEL_ID,
            name="ShouldFire",
        )
        # Should skip: wrong weekday (Wednesday=2).
        _seed_reminder(
            s,
            weekday=2,
            fire_time_utc=datetime.time(7, 0, 0),
            channel_id=_CHANNEL_ID,
            name="WrongWeekday",
        )
        # Should skip: fire_time > now (08:00 > 07:00).
        _seed_reminder(
            s,
            weekday=1,
            fire_time_utc=datetime.time(8, 0, 0),
            channel_id=_CHANNEL_ID,
            name="TooLate",
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(now, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Exactly one send for "ShouldFire".
    assert channel.send.call_count == 1


# ---------------------------------------------------------------------------
# NULL role_mention_id — no ping prefix (scheduler.py:L243-L244)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_role_mention_id_sends_bare_message() -> None:
    """Reminder with role_mention_id=None sends message without <@& prefix.

    Exercises the scheduler.py:L243-L244 NULL-check branch introduced in
    #28 and made the default by #45.  When ``role_mention_id`` is ``None``
    the scheduler must deliver the bare ``message_template`` string without
    prepending a role mention.
    """
    engine = _make_engine()
    with Session(engine) as s:
        _seed_reminder(
            s,
            weekday=_TUESDAY_0700.weekday(),
            role_mention_id=None,
            name="NoMention",
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(_TUESDAY_0700, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    channel.send.assert_called_once()
    sent_message: str = channel.send.call_args.args[0]
    assert "<@&" not in sent_message, (
        "Expected no role-mention prefix when role_mention_id is None, "
        f"but got: {sent_message!r}"
    )
    assert "Test message" in sent_message


# ---------------------------------------------------------------------------
# UTC-midnight date-attribution (plan § 12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fire_moment,expected_fire_date",
    [
        # 23:59:00 UTC on Tuesday → attributed to Tuesday.
        (
            datetime.datetime(2026, 5, 5, 23, 59, 0, tzinfo=datetime.UTC),
            datetime.date(2026, 5, 5),
        ),
        # 23:59:59 UTC on Tuesday → attributed to Tuesday.
        (
            datetime.datetime(2026, 5, 5, 23, 59, 59, tzinfo=datetime.UTC),
            datetime.date(2026, 5, 5),
        ),
        # 00:00:00 UTC on Wednesday → attributed to Wednesday.
        (
            datetime.datetime(2026, 5, 6, 0, 0, 0, tzinfo=datetime.UTC),
            datetime.date(2026, 5, 6),
        ),
        # 00:00:01 UTC on Wednesday → attributed to Wednesday.
        (
            datetime.datetime(2026, 5, 6, 0, 0, 1, tzinfo=datetime.UTC),
            datetime.date(2026, 5, 6),
        ),
    ],
)
async def test_utc_midnight_date_attribution(
    fire_moment: datetime.datetime,
    expected_fire_date: datetime.date,
) -> None:
    """fire_date_utc on reminder_sent row matches the UTC calendar date."""
    engine = _make_engine()
    weekday = fire_moment.weekday()
    # Truncate to minute boundary for fire_time_utc column.
    fire_time = fire_moment.time().replace(second=0, microsecond=0)

    with Session(engine) as s:
        _seed_reminder(
            s,
            weekday=weekday,
            fire_time_utc=fire_time,
        )

    channel = FakeChannel(_CHANNEL_ID)
    bot = FakeBot(ready=True)
    bot.add_channel(channel)
    scheduler = _make_scheduler(bot, engine, tick_seconds=0.05)

    with time_machine.travel(fire_moment, tick=False):
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with Session(engine) as s:
        sent_rows = s.query(ReminderSent).all()

    assert len(sent_rows) == 1
    assert sent_rows[0].fire_date_utc == expected_fire_date
