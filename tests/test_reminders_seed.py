"""Tests for _maybe_seed_reminders.

Verifies idempotent seed-on-boot from Key Vault values.
"""

from __future__ import annotations

import datetime
import logging
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from mom_bot.db import Base
from mom_bot.reminders.models import Reminder, ReminderSent  # noqa: F401
from mom_bot.reminders.seed import _maybe_seed_reminders

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CHANNEL = "123456789012345678"
_MENTION_ROLE = "345678901234567890"


@pytest.fixture()
def session() -> Session:
    """In-memory SQLite session with both reminder tables created."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _secret_side_effect(name: str) -> str:
    """Return a fake snowflake for each expected secret name."""
    secrets = {
        "reminder-channel-id": _CHANNEL,
        "reminder-mention-role-id": _MENTION_ROLE,
    }
    if name not in secrets:
        raise KeyError(f"Unexpected secret: {name!r}")
    return secrets[name]


# ---------------------------------------------------------------------------
# Seed-on-boot idempotency
# ---------------------------------------------------------------------------


def test_seed_empty_table_inserts_hydra_and_chimera(session: Session) -> None:
    """Empty reminders table + valid KV secrets → two rows inserted.

    Both rows share the same channel_id (sourced from ``reminder-channel-id``).
    Hydra: weekday=1, fire_time=07:00:00.
    Chimera: weekday=2, fire_time=12:00:00.
    """
    with patch(
        "mom_bot.reminders.seed.load_secret",
        side_effect=_secret_side_effect,
    ):
        _maybe_seed_reminders(session)

    count = session.scalar(select(func.count(Reminder.id)))
    assert count == 2

    hydra = session.execute(select(Reminder).where(Reminder.name == "Hydra")).scalar_one()
    assert hydra.weekday == 1
    assert hydra.fire_time_utc == datetime.time(7, 0, 0)
    assert hydra.channel_id == int(_CHANNEL)
    assert hydra.role_mention_id == int(_MENTION_ROLE)

    chimera = session.execute(select(Reminder).where(Reminder.name == "Chimera")).scalar_one()
    assert chimera.weekday == 2
    assert chimera.fire_time_utc == datetime.time(12, 0, 0)
    assert chimera.channel_id == int(_CHANNEL)
    assert chimera.role_mention_id == int(_MENTION_ROLE)

    # Both rows share the single channel secret — verify equality explicitly.
    assert hydra.channel_id == chimera.channel_id


def test_seed_non_empty_table_is_noop(session: Session) -> None:
    """Non-empty reminders table → _maybe_seed_reminders is a no-op."""
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
        return "0"

    with patch(
        "mom_bot.reminders.seed.load_secret",
        side_effect=counting_secret,
    ):
        _maybe_seed_reminders(session)

    # No KV calls should be made when table is non-empty.
    assert call_count == 0
    count = session.scalar(select(func.count(Reminder.id)))
    assert count == 1  # Only the pre-seeded row.


def test_seed_kv_failure_logs_critical_and_raises(
    session: Session,
) -> None:
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
                _maybe_seed_reminders(session)
    finally:
        seed_logger.removeHandler(handler)

    # At least one CRITICAL log message must have been emitted.
    critical_records = [r for r in captured if r.levelname == "CRITICAL"]
    assert len(critical_records) >= 1
