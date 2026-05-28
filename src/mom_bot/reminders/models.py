"""SQLAlchemy ORM models for the mom-bot reminder scheduler (Epic 1).

Defines the ``Reminder`` and ``ReminderSent`` tables used to schedule
Discord reminders and track per-day idempotency. Both models are registered
on ``mom_bot.db.Base.metadata`` so Alembic autogenerate can detect them.

Schema design rationale (plan § 4):

- ``Reminder`` stores the schedule: one row per recurring reminder (e.g.
  "Hydra" fires Tuesday 07:00 UTC, "Chimera" fires Wednesday 12:00 UTC).
  Channel and role are stored as Discord snowflakes to avoid per-send name
  lookups.
- ``ReminderSent`` is the idempotency log: at most one row per
  (reminder_id, fire_date_utc), enforced by a UNIQUE constraint. The
  database replaces the in-process ``RLock`` from the old JSON store.
- ``fire_time_utc`` is constrained to minute-boundary values (seconds = 0)
  to match the minute-level scheduler tick (plan § 3 row 4).
- The FK from ``reminder_sent.reminder_id`` to ``reminders.id`` uses
  ``ON DELETE CASCADE`` so that deleting a reminder automatically cleans
  its send history.

Single-replica assumption: ``maxReplicas = 1`` on the Container App makes
concurrent fires from overlapping replicas unlikely, but the UNIQUE
constraint on ``reminder_sent`` is cheap insurance — the loser of a race
sees ``IntegrityError`` and skips (plan § 4 Concurrency note).
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from mom_bot.db import Base

__all__ = ["Reminder", "ReminderSent"]

# ---------------------------------------------------------------------------
# Reminder (schedule table)
# ---------------------------------------------------------------------------


class Reminder(Base):
    """Stores a recurring reminder schedule row.

    Each row represents one Discord reminder: which channel to post in,
    which day of the week and time (UTC) to fire, the message body, and an
    optional role to mention.

    Attributes:
        id: Surrogate integer primary key, auto-incremented.
        name: Human-readable identifier (e.g. ``"Hydra"``). Must be unique
            across all reminders; used as the Epic 3 lookup key for
            ``/reminder remove <name>``.
        channel_id: Discord channel snowflake. Captured once at seed time so
            every scheduler tick can send directly without a name lookup.
        weekday: Python ``date.weekday()`` semantics — Mon=0, Sun=6. Matches
            the source system convention at ``clan_reminders.py:L17``.
        fire_time_utc: Wall-clock UTC time at which the reminder fires.
            Stored as ``HH:MM:SS``; constrained to minute-boundary values
            (seconds = 0) by the ``ck_fire_time_no_seconds`` CHECK constraint
            created in migration 0002. The constraint is dialect-aware
            (SQLite ``strftime`` vs Postgres ``EXTRACT``) and lives only in
            the database — there is no ORM-level mirror.
        message_template: The Discord message body. Currently a static string
            (Epic 3 may add template substitution).
        role_mention_id: Optional Discord role snowflake to ping at fire
            time. ``None`` means no role mention.
        created_at: Audit timestamp, set once at INSERT time.
        updated_at: Audit timestamp, updated automatically on every UPDATE
            via SQLAlchemy's ``onupdate`` hook.
    """

    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_weekday"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    fire_time_utc: Mapped[datetime.time] = mapped_column(Time, nullable=False)
    message_template: Mapped[str] = mapped_column(Text, nullable=False)
    role_mention_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


# ---------------------------------------------------------------------------
# ReminderSent (idempotency log)
# ---------------------------------------------------------------------------


class ReminderSent(Base):
    """Records that a reminder fired on a specific UTC calendar date.

    Acts as the idempotency log: at most one row is permitted per
    (reminder_id, fire_date_utc) via a UNIQUE constraint. This replaces the
    in-process ``RLock`` of the old JSON ``reminder_sent_store.py``.

    The send pattern is insert-then-send-with-drop-on-failure (plan § 5):
    insert this row first to claim the per-day slot, then send the Discord
    message. Permanent Discord failures leave the row in place (no retry).
    Transient failures delete the row before re-raising so the next tick
    can retry.

    Attributes:
        id: Surrogate integer primary key, auto-incremented.
        reminder_id: Foreign key to ``reminders.id`` with ``ON DELETE
            CASCADE`` so that deleting a reminder cleans its send history.
        fire_date_utc: The UTC calendar date the fire was attributed to.
        sent_at: Actual wall-clock timestamp of the send attempt, set at
            INSERT time.
    """

    __tablename__ = "reminder_sent"
    __table_args__ = (
        UniqueConstraint(
            "reminder_id",
            "fire_date_utc",
            name="uq_reminder_sent_per_day",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reminder_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("reminders.id", ondelete="CASCADE"),
        nullable=False,
    )
    fire_date_utc: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime.datetime] = mapped_column(
        nullable=False, server_default=func.current_timestamp()
    )
