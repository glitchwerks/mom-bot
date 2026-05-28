"""ReminderSentStore — atomic per-day idempotency log for the scheduler.

Wraps the ``reminder_sent`` database table and provides three primitives:

- :meth:`ReminderSentStore.mark_sent` — claim a (reminder_id, date) slot
  via INSERT; returns ``True`` on success, ``False`` on UNIQUE collision.
- :meth:`ReminderSentStore.unmark` — delete a row so the next scheduler
  tick can retry (used only for transient Discord errors, per plan § 5).
- :meth:`ReminderSentStore.was_sent` — read-only membership check.

The atomic primitive is "INSERT and catch IntegrityError".  The database
UNIQUE constraint on ``(reminder_id, fire_date_utc)`` serialises concurrent
inserts from any overlapping scheduler instances — the loser sees
``IntegrityError`` and skips the Discord send (plan § 4 Concurrency note).

Each mutating method commits immediately (no batching) so the row is
visible to other processes before the Discord send is attempted.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mom_bot.reminders.models import ReminderSent

__all__ = ["ReminderSentStore"]

_logger = logging.getLogger(__name__)


class ReminderSentStore:
    """Reads and writes the ``reminder_sent`` idempotency table.

    Attributes:
        _session: The SQLAlchemy ``Session`` used for all database
            operations. Callers must ensure it is bound to the correct
            database engine.
    """

    def __init__(self, session: Session) -> None:
        """Initialise with an active SQLAlchemy session.

        Args:
            session: An open :class:`~sqlalchemy.orm.Session` bound to the
                mom-bot database.
        """
        self._session = session

    def mark_sent(self, reminder_id: int, fire_date_utc: datetime.date) -> bool:
        """Claim the per-day slot for *reminder_id* on *fire_date_utc*.

        Attempts an INSERT into ``reminder_sent``.  If the UNIQUE constraint
        fires (another scheduler instance or a previous call beat us to it),
        the transaction is rolled back and the method returns ``False``
        without raising.

        The INSERT is committed immediately so the row is visible to other
        processes before the Discord send is attempted (plan § 5).

        Args:
            reminder_id: The integer primary key of the
                :class:`~mom_bot.reminders.models.Reminder` row.
            fire_date_utc: The UTC calendar date to attribute the fire to.

        Returns:
            ``True`` if a new row was inserted (slot claimed).
            ``False`` if the slot was already taken (UNIQUE collision).
        """
        row = ReminderSent(
            reminder_id=reminder_id,
            fire_date_utc=fire_date_utc,
        )
        try:
            self._session.add(row)
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            _logger.debug(
                "mark_sent: UNIQUE collision for reminder_id=%d date=%s",
                reminder_id,
                fire_date_utc,
            )
            return False
        return True

    def unmark(self, reminder_id: int, fire_date_utc: datetime.date) -> None:
        """Delete the per-day row so the next tick can retry.

        Called on transient Discord errors (plan § 5: 5xx, RateLimited,
        aiohttp.ClientError, asyncio.TimeoutError) to undo a previously
        successful :meth:`mark_sent` before re-raising the error.  If the
        row does not exist, the method is a no-op.

        Args:
            reminder_id: The integer primary key of the
                :class:`~mom_bot.reminders.models.Reminder` row.
            fire_date_utc: The UTC calendar date to remove.
        """
        deleted = (
            self._session.query(ReminderSent)
            .filter_by(reminder_id=reminder_id, fire_date_utc=fire_date_utc)
            .first()
        )
        if deleted is not None:
            self._session.delete(deleted)
            self._session.commit()
        _logger.debug(
            "unmark: removed row for reminder_id=%d date=%s",
            reminder_id,
            fire_date_utc,
        )

    def was_sent(self, reminder_id: int, fire_date_utc: datetime.date) -> bool:
        """Return ``True`` if a sent row exists for *reminder_id* on *fire_date_utc*.

        Args:
            reminder_id: The integer primary key of the
                :class:`~mom_bot.reminders.models.Reminder` row.
            fire_date_utc: The UTC calendar date to check.

        Returns:
            ``True`` if a matching row exists in ``reminder_sent``.
            ``False`` otherwise.
        """
        exists = (
            self._session.query(ReminderSent)
            .filter_by(reminder_id=reminder_id, fire_date_utc=fire_date_utc)
            .first()
        )
        return exists is not None
