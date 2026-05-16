"""Minute-level reminder scheduler for mom-bot (plan §§ 3-6).

Provides :class:`ReminderScheduler`, an async loop that wakes once per
minute, queries eligible reminders, and delivers them to Discord.

Architecture summary
--------------------
- **Single-replica assumption**: ``maxReplicas = 1`` on the Container App
  makes concurrent fires unlikely.  The UNIQUE constraint on
  ``reminder_sent(reminder_id, fire_date_utc)`` is cheap insurance — the
  loser of any race sees ``IntegrityError`` and skips (plan § 4 Concurrency
  note; revision-rollout overlap test in ``tests/test_reminders_scheduler.py``
  validates this path explicitly).
- **Send ordering**: INSERT the ``reminder_sent`` row first to claim the
  per-day slot, then attempt the Discord send (plan § 3 row 8).  Permanent
  errors leave the row in place; transient errors delete the row before
  re-raising so the next tick retries.
- **Gateway readiness gate**: two levels:
  1. Coarse (cold start) — caller must ``await bot.wait_until_ready()``
     before calling :meth:`~ReminderScheduler.run`.
  2. Per-tick — each iteration checks ``bot.is_ready()``; if False the
     tick is skipped (no insert, no send) to handle mid-day gateway
     reconnects.  ``wait_until_ready()`` is one-shot and does not re-block
     on reconnect (plan § 6).
- **No ``time.sleep``**: all waits are ``await asyncio.sleep(...)`` so the
  event loop stays responsive and ``time-machine`` interacts correctly in
  tests.
- **Liveness sentinel**: the scheduler calls
  :func:`mom_bot.health.record_heartbeat` at the start of every tick
  (before the readiness guard) so the Container Apps httpGet probe against
  ``GET /healthz`` can confirm the loop is alive even when the gateway is
  disconnected.  The legacy sentinel file (``/tmp/scheduler-heartbeat``) is
  also touched for backward compatibility during any transition period.

Discord error taxonomy (plan § 5)
----------------------------------
Each send error falls into one of two buckets:

Drop (row stays — permanent failure, no retry)
  - ``discord.Forbidden`` (HTTP 403): bot lacks channel permission.
  - ``discord.NotFound`` (HTTP 404): channel was deleted.
  - Any other ``Exception``: unknown failure mode; logged CRITICAL.

Retry (row deleted before re-raise — transient failure)
  - ``discord.HTTPException`` with ``status >= 500``: server-side transient.
  - ``discord.RateLimited`` (HTTP 429): treated as transient.
  - ``aiohttp.ClientError``: network-layer transient.
  - ``asyncio.TimeoutError``: network-layer transient.

The deleting-then-raising pattern means the next scheduler tick (≤60 s
later) retries the send via the same fire-time predicate (``<=``, not
``==``), which catches the retry within the same calendar day.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from pathlib import Path

import aiohttp
import discord
from sqlalchemy import select
from sqlalchemy.orm import Session

import mom_bot.health as health
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401
from mom_bot.reminders.sent_store import ReminderSentStore

__all__ = ["ReminderScheduler"]

_logger = logging.getLogger(__name__)

_HEARTBEAT_PATH = Path("/tmp/scheduler-heartbeat")


class ReminderScheduler:
    """Async loop that fires Discord reminders on a minute-level tick.

    Attributes:
        _bot: The discord.py client (or a test double implementing
            ``is_ready()`` and ``get_channel()``).
        _session_factory: A zero-argument callable that returns a new
            :class:`~sqlalchemy.orm.Session` bound to the bot database.
        _tick_seconds: Seconds to sleep between ticks.  Defaults to 60.0
            for production; tests inject smaller values.
    """

    def __init__(
        self,
        bot: discord.Client,
        session_factory: Callable[[], Session],
        tick_seconds: float = 60.0,
    ) -> None:
        """Initialise the scheduler.

        Args:
            bot: The discord.py client instance.  Must implement
                ``is_ready()`` and ``get_channel(channel_id)``.
            session_factory: A zero-argument callable that returns a fresh
                :class:`~sqlalchemy.orm.Session` for each tick.  Typically
                a :class:`~sqlalchemy.orm.sessionmaker` instance.
            tick_seconds: Seconds to sleep between scheduler ticks.
                Default 60.0; inject a smaller value in tests.
        """
        self._bot = bot
        self._session_factory = session_factory
        self._tick_seconds = tick_seconds

    async def run(self) -> None:
        """Run the scheduler loop until the task is cancelled.

        Each iteration:

        1. Touch the liveness sentinel file at ``/tmp/scheduler-heartbeat``.
        2. If ``bot.is_ready()`` is False: sleep and continue (per-tick
           readiness guard for mid-day reconnects).
        3. Query eligible reminders using the fire-time predicate (plan § 4).
        4. For each eligible reminder: attempt to INSERT a ``reminder_sent``
           row.  If the INSERT collides (UNIQUE), skip this reminder (another
           scheduler won the race).
        5. Attempt the Discord send.  Handle errors per the taxonomy in the
           module docstring.
        6. ``await asyncio.sleep(tick_seconds)``.

        Raises:
            asyncio.CancelledError: Propagated cleanly when the task is
                cancelled (e.g. on SIGTERM).
        """
        while True:
            # Step 1 — liveness sentinel (before readiness guard so a
            # healthy-but-disconnected scheduler still updates the heartbeat).
            # The in-process heartbeat is the primary signal; the legacy file
            # touch is kept for operator debugging convenience.
            health.record_heartbeat()
            try:
                _HEARTBEAT_PATH.touch()
            except OSError:
                # Non-fatal — the probe will eventually fail if this persists.
                _logger.warning("scheduler: could not touch %s", _HEARTBEAT_PATH)

            # Step 2 — per-tick readiness guard.
            if not self._bot.is_ready():
                _logger.debug("scheduler: bot not ready; skipping tick")
                await asyncio.sleep(self._tick_seconds)
                continue

            # Step 3 — compute fire-time predicate parameters once per tick.
            now_utc = datetime.datetime.now(datetime.UTC)
            today_weekday = now_utc.weekday()
            now_time = now_utc.time().replace(microsecond=0)
            today_date = now_utc.date()

            try:
                await self._process_tick(today_weekday, now_time, today_date)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Transient per-tick errors (e.g. 5xx, RateLimited,
                # aiohttp, timeout) are logged inside _handle_reminder and
                # the row is already deleted for retry.  The loop continues
                # so the next tick retries; we do not crash the scheduler.
                _logger.debug("scheduler: per-tick error suppressed; will retry next tick")

            # Step 6 — sleep before the next tick.
            await asyncio.sleep(self._tick_seconds)

    async def _process_tick(
        self,
        today_weekday: int,
        now_time: datetime.time,
        today_date: datetime.date,
    ) -> None:
        """Query and fire all eligible reminders for this tick.

        Opens a fresh session for the tick, queries eligible reminders,
        and handles each one via :meth:`_handle_reminder`.

        Args:
            today_weekday: UTC weekday integer (Mon=0, Sun=6).
            now_time: UTC time with microseconds zeroed, used for the
                ``fire_time_utc <= now_time`` predicate.
            today_date: UTC calendar date used for idempotency attribution.
        """
        with self._session_factory() as session:
            # Build the "already sent today" sub-query.
            sent_today_ids = (
                select(ReminderSent.reminder_id).where(ReminderSent.fire_date_utc == today_date)
            ).scalar_subquery()

            eligible = (
                session.execute(
                    select(Reminder)
                    .where(Reminder.weekday == today_weekday)
                    .where(Reminder.fire_time_utc <= now_time)
                    .where(Reminder.id.not_in(sent_today_ids))
                )
                .scalars()
                .all()
            )

            for reminder in eligible:
                await self._handle_reminder(reminder, today_date, session)

    async def _handle_reminder(
        self,
        reminder: Reminder,
        today_date: datetime.date,
        session: Session,
    ) -> None:
        """Attempt to fire one reminder: INSERT then send.

        Follows the insert-then-send-with-drop-on-failure pattern (plan
        § 5).  If the INSERT collides (UNIQUE), another scheduler won the
        race and this method returns without attempting a send.

        Args:
            reminder: The :class:`~mom_bot.reminders.models.Reminder` row
                to fire.
            today_date: The UTC calendar date to attribute the fire to.
            session: The active SQLAlchemy session for this tick.
        """
        store = ReminderSentStore(session)

        # Step 4 — INSERT the idempotency row.
        inserted = store.mark_sent(reminder.id, today_date)
        if not inserted:
            # UNIQUE collision — another scheduler claimed this slot.
            _logger.debug(
                "scheduler: UNIQUE collision for reminder %r on %s; skipping",
                reminder.name,
                today_date,
            )
            return

        # Step 5 — attempt the Discord send.
        channel = self._bot.get_channel(reminder.channel_id)
        if channel is None:
            _logger.error(
                "scheduler: channel %d not found for reminder %r; dropping",
                reminder.channel_id,
                reminder.name,
            )
            return

        message = reminder.message_template
        allowed_mentions: discord.AllowedMentions | None = None
        if reminder.role_mention_id is not None:
            message = f"<@&{reminder.role_mention_id}> {message}"
            # Without AllowedMentions(roles=True), Discord renders the
            # <@&id> markup as a clickable mention visually, but suppresses
            # the actual ping/notification to role members — this is
            # Discord's safe-default behavior to prevent runaway role pings.
            # We explicitly opt in only when role_mention_id IS NOT NULL,
            # matching the per-env configuration intent from KV (#51).
            allowed_mentions = discord.AllowedMentions(roles=True)

        try:
            if allowed_mentions is not None:
                await channel.send(  # type: ignore[union-attr]
                    message, allowed_mentions=allowed_mentions
                )
            else:
                await channel.send(message)  # type: ignore[union-attr]
            _logger.info(
                "scheduler: fired reminder %r to channel %d on %s",
                reminder.name,
                reminder.channel_id,
                today_date,
            )
        except (discord.Forbidden, discord.NotFound) as exc:
            # Permanent failure — row stays, no retry.
            _logger.error(
                "scheduler: permanent Discord error for reminder %r on %s: "
                "%s — dropping (row stays)",
                reminder.name,
                today_date,
                exc,
            )
        except (TimeoutError, discord.RateLimited, aiohttp.ClientError) as exc:
            # Transient failure — delete row so next tick retries.
            store.unmark(reminder.id, today_date)
            _logger.error(
                "scheduler: transient error for reminder %r on %s: " "%s — row deleted for retry",
                reminder.name,
                today_date,
                exc,
            )
            raise
        except discord.HTTPException as exc:
            if exc.status >= 500:
                # Transient server-side error — delete row so next tick retries.
                store.unmark(reminder.id, today_date)
                _logger.error(
                    "scheduler: HTTPException status=%d for reminder %r "
                    "on %s — row deleted for retry: %s",
                    exc.status,
                    reminder.name,
                    today_date,
                    exc,
                )
                raise
            # Other HTTP errors (e.g. 400, 401) — treat as permanent drop.
            _logger.error(
                "scheduler: HTTPException status=%d for reminder %r on %s: "
                "%s — dropping (row stays)",
                exc.status,
                reminder.name,
                today_date,
                exc,
            )
        except Exception as exc:
            # Unknown failure mode — log CRITICAL, leave row, do not retry.
            _logger.critical(
                "scheduler: unexpected error for reminder %r on %s: " "%s — dropping (row stays)",
                reminder.name,
                today_date,
                exc,
            )
