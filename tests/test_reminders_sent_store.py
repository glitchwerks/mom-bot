"""Tests for ReminderSentStore.

Verifies the atomic INSERT-and-catch-IntegrityError primitive that
provides per-day idempotency for the reminder scheduler.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from mom_bot.db import Base
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401
from mom_bot.reminders.sent_store import ReminderSentStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TODAY = datetime.date(2026, 5, 6)  # fixed date; weekday=1 (Tuesday)
_TIME = datetime.time(7, 0, 0)  # Hydra fire-time


@pytest.fixture()
def session() -> Session:
    """In-memory SQLite session with both reminder tables created."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        # Seed one reminder row so FK is satisfiable.
        reminder = Reminder(
            name="Hydra",
            channel_id=111111111111111111,
            weekday=1,
            fire_time_utc=_TIME,
            message_template="test",
            role_mention_id=None,
        )
        s.add(reminder)
        s.commit()
        yield s


@pytest.fixture()
def store(session: Session) -> ReminderSentStore:
    """Return a ReminderSentStore backed by the in-memory session."""
    return ReminderSentStore(session)


@pytest.fixture()
def reminder_id(session: Session) -> int:
    """Return the id of the seeded Hydra reminder."""
    row = session.query(Reminder).filter_by(name="Hydra").one()
    return int(row.id)


# ---------------------------------------------------------------------------
# mark_sent
# ---------------------------------------------------------------------------


def test_mark_sent_empty_table_returns_true(
    store: ReminderSentStore,
    reminder_id: int,
) -> None:
    """mark_sent on an empty table should return True and persist a row."""
    result = store.mark_sent(reminder_id, _TODAY)

    assert result is True
    # Row must exist in the table.
    assert store.was_sent(reminder_id, _TODAY) is True


def test_mark_sent_duplicate_returns_false(
    store: ReminderSentStore,
    reminder_id: int,
) -> None:
    """Second mark_sent with the same key returns False, no exception raised."""
    first = store.mark_sent(reminder_id, _TODAY)
    second = store.mark_sent(reminder_id, _TODAY)

    assert first is True
    assert second is False
    # Still exactly one row (no double-insert).
    assert store.was_sent(reminder_id, _TODAY) is True


# ---------------------------------------------------------------------------
# unmark
# ---------------------------------------------------------------------------


def test_unmark_removes_row(
    store: ReminderSentStore,
    reminder_id: int,
) -> None:
    """unmark should delete the row so was_sent returns False."""
    store.mark_sent(reminder_id, _TODAY)
    assert store.was_sent(reminder_id, _TODAY) is True

    store.unmark(reminder_id, _TODAY)

    assert store.was_sent(reminder_id, _TODAY) is False


# ---------------------------------------------------------------------------
# was_sent
# ---------------------------------------------------------------------------


def test_was_sent_false_when_no_row(
    store: ReminderSentStore,
    reminder_id: int,
) -> None:
    """was_sent returns False for a date that has no row."""
    assert store.was_sent(reminder_id, _TODAY) is False


def test_was_sent_true_after_mark(
    store: ReminderSentStore,
    reminder_id: int,
) -> None:
    """was_sent returns True after a successful mark_sent."""
    store.mark_sent(reminder_id, _TODAY)
    assert store.was_sent(reminder_id, _TODAY) is True
