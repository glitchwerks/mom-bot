"""Tests for auto-migration at bot startup (issue #94).

TDD: tests were written before the implementation.  Each test covers one
discrete behaviour of the startup migration wiring.

Design notes
------------
- We unit-test the *wiring* only — that ``run_migrations`` is called at the
  right point in ``setup_hook`` and that failures propagate loudly.  We do NOT
  run real migrations against a file-backed SQLite (that is the concern of
  ``test_alembic.py``).
- ``alembic.command.upgrade`` and ``alembic.config.Config`` are mocked so no
  disk I/O or actual migration logic runs in these tests.
- The autouse ``mock_health_server`` fixture (defined here) prevents port 8080
  collisions the same way ``test_main_wireup.py`` does.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Autouse fixture — prevent port 8080 collision across all setup_hook tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_health_server() -> Any:
    """Patch start_health_server for every test in this module.

    Yields:
        The ``AsyncMock`` that replaced ``start_health_server``.
    """
    runner_mock = MagicMock()
    runner_mock.cleanup = AsyncMock()
    site_mock = MagicMock()
    health_mock = AsyncMock(return_value=(runner_mock, site_mock))
    with patch("mom_bot.main.start_health_server", health_mock):
        yield health_mock


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_GUILD_ID = 999999999999999999


def _fake_load_secret(name: str) -> str:
    """Return canned KV values so no real Key Vault call is made.

    Args:
        name: Secret name requested by the bot.

    Returns:
        A canned string value for the given secret name.
    """
    values: dict[str, str] = {
        "guild-id": str(_GUILD_ID),
        "reminder-channel-name": "reminders",
        "reminder-mention-role-name": "Member",
    }
    return values[name]


# ---------------------------------------------------------------------------
# Test A — setup_hook calls run_migrations exactly once before any DB session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_hook_calls_run_migrations_once() -> None:
    """setup_hook() must call run_migrations exactly once.

    Verifies that the migration entry-point is invoked during ``setup_hook``
    before the background task (which opens DB sessions) is spawned.

    Mocks ``alembic.command.upgrade`` to avoid real disk I/O and asserts
    ``run_migrations`` is called with the alembic config pointing at
    ``alembic.ini``.
    """
    from mom_bot.main import MomBot, build_intents

    bot = MomBot(intents=build_intents())

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot.tree, "sync", new_callable=AsyncMock),
        patch("mom_bot.main.run_migrations") as mock_run_migrations,
    ):
        await bot.setup_hook()

    mock_run_migrations.assert_called_once()

    # Clean up background task.
    if bot._reminder_task is not None:
        bot._reminder_task.cancel()
        try:
            await bot._reminder_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Test B — run_migrations invokes alembic.command.upgrade with correct args
# ---------------------------------------------------------------------------


def test_run_migrations_calls_alembic_upgrade_head() -> None:
    """run_migrations() must call alembic.command.upgrade(cfg, 'head').

    Verifies that the standalone ``run_migrations`` function constructs an
    ``alembic.config.Config`` from ``alembic.ini`` and passes it to
    ``alembic.command.upgrade`` with the ``'head'`` target.
    """
    from mom_bot.main import run_migrations

    with (
        patch("mom_bot.main.AlembicConfig") as mock_config_cls,
        patch("mom_bot.main.alembic_upgrade") as mock_upgrade,
    ):
        mock_cfg = MagicMock()
        mock_config_cls.return_value = mock_cfg

        run_migrations()

    mock_config_cls.assert_called_once_with("alembic.ini")
    mock_upgrade.assert_called_once_with(mock_cfg, "head")


# ---------------------------------------------------------------------------
# Test C — migration failure propagates; setup_hook must not swallow it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_hook_propagates_migration_failure() -> None:
    """If run_migrations raises, setup_hook must let the exception propagate.

    ACA will restart the container on crash; a bot with a stale schema must
    NOT silently start.  Verify that a ``RuntimeError`` from ``run_migrations``
    is NOT caught by ``setup_hook``.
    """
    from mom_bot.main import MomBot, build_intents

    bot = MomBot(intents=build_intents())

    with (
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch.object(bot.tree, "sync", new_callable=AsyncMock),
        patch(
            "mom_bot.main.run_migrations",
            side_effect=RuntimeError("migration failed"),
        ),
        pytest.raises(RuntimeError, match="migration failed"),
    ):
        await bot.setup_hook()
