"""Tests for mom_bot.health — HTTP /healthz endpoint and heartbeat sentinel.

TDD: tests were written before the implementation.  Each test covers one
discrete behaviour of the heartbeat sentinel and the aiohttp /healthz handler.

Design notes
------------
- ``record_heartbeat()`` updates a module-level ``last_heartbeat`` float so
  tests can control the value without monkey-patching ``time``.
- The handler is tested by calling it directly with a minimal aiohttp
  ``web.Request`` mock — no actual server is started.
- The ``start_health_server`` coroutine is exercised for start/stop lifecycle
  only (binding to a random port so tests do not need port 8080).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mom_bot.health as health_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_heartbeat() -> None:
    """Reset the module-level last_heartbeat to its uninitialised sentinel."""
    health_mod.last_heartbeat = 0.0


# ---------------------------------------------------------------------------
# Test 1 — record_heartbeat updates last_heartbeat to a recent value
# ---------------------------------------------------------------------------


def test_record_heartbeat_updates_timestamp() -> None:
    """record_heartbeat() sets last_heartbeat to approximately now."""
    _reset_heartbeat()
    before = time.monotonic()
    health_mod.record_heartbeat()
    after = time.monotonic()

    assert (
        before <= health_mod.last_heartbeat <= after
    ), "last_heartbeat should be between the before and after monotonic readings"


# ---------------------------------------------------------------------------
# Test 2 — /healthz returns 200 when heartbeat is recent (within 60 s)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_returns_200_when_heartbeat_recent() -> None:
    """Handler returns 200 when last_heartbeat is less than 60 s old."""
    health_mod.last_heartbeat = time.monotonic() - 10.0  # 10 s ago

    request = MagicMock()
    response = await health_mod.healthz_handler(request)

    assert response.status == 200, f"Expected 200 when heartbeat is recent, got {response.status}"


# ---------------------------------------------------------------------------
# Test 3 — /healthz returns 503 when heartbeat is stale (> 60 s)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_returns_503_when_heartbeat_stale() -> None:
    """Handler returns 503 when last_heartbeat is more than 60 s old."""
    health_mod.last_heartbeat = time.monotonic() - 61.0  # 61 s ago

    request = MagicMock()
    response = await health_mod.healthz_handler(request)

    assert response.status == 503, f"Expected 503 when heartbeat is stale, got {response.status}"


# ---------------------------------------------------------------------------
# Test 4 — /healthz returns 503 when heartbeat was never set (value == 0.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_returns_503_when_never_set() -> None:
    """Handler returns 503 when last_heartbeat has never been recorded (0.0)."""
    _reset_heartbeat()
    assert health_mod.last_heartbeat == 0.0

    request = MagicMock()
    response = await health_mod.healthz_handler(request)

    assert response.status == 503, f"Expected 503 when heartbeat never set, got {response.status}"


# ---------------------------------------------------------------------------
# Test 5 — /healthz returns 200 at the boundary (exactly at threshold - 1 s)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_returns_200_at_boundary() -> None:
    """Handler returns 200 when heartbeat is 59 s old (just within threshold)."""
    health_mod.last_heartbeat = time.monotonic() - 59.0

    request = MagicMock()
    response = await health_mod.healthz_handler(request)

    assert response.status == 200, f"Expected 200 when heartbeat is 59 s old, got {response.status}"


# ---------------------------------------------------------------------------
# Test 6 — start_health_server starts and can be shut down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_health_server_starts_and_stops() -> None:
    """start_health_server runs a server that can be cleanly shut down.

    Exercises both the startup path (``runner.setup`` + ``site.start``) and
    the shutdown path (``runner.cleanup``), verifying all three are awaited.
    """
    site_mock = AsyncMock()
    runner_mock = AsyncMock()
    runner_mock.setup = AsyncMock()
    runner_mock.cleanup = AsyncMock()

    mock_site_cls = MagicMock(return_value=site_mock)
    mock_runner_cls = MagicMock(return_value=runner_mock)

    with (
        patch("mom_bot.health.web.AppRunner", mock_runner_cls),
        patch("mom_bot.health.web.TCPSite", mock_site_cls),
    ):
        runner, site = await health_mod.start_health_server(host="127.0.0.1", port=18080)

    runner_mock.setup.assert_awaited_once()
    site_mock.start.assert_awaited_once()
    assert runner is runner_mock
    assert site is site_mock

    # Exercise the shutdown path — caller is responsible for cleanup.
    await runner.cleanup()
    runner_mock.cleanup.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 7 — start_health_server cleans up runner if TCPSite.start() raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_health_server_cleans_up_runner_on_site_start_failure() -> None:
    """runner.cleanup() is called when TCPSite.start() raises.

    Verifies the error-path resource-management fix from PR #88: if
    ``TCPSite.start()`` raises (e.g. ``OSError: address already in use``),
    ``AppRunner`` resources must not be leaked.  The implementation must
    call ``await runner.cleanup()`` before re-raising the exception.
    """
    site_mock = AsyncMock()
    site_mock.start = AsyncMock(side_effect=OSError("address already in use"))
    runner_mock = AsyncMock()
    runner_mock.setup = AsyncMock()
    runner_mock.cleanup = AsyncMock()

    mock_site_cls = MagicMock(return_value=site_mock)
    mock_runner_cls = MagicMock(return_value=runner_mock)

    with (
        patch("mom_bot.health.web.AppRunner", mock_runner_cls),
        patch("mom_bot.health.web.TCPSite", mock_site_cls),
    ):
        with pytest.raises(OSError, match="address already in use"):
            await health_mod.start_health_server(host="127.0.0.1", port=18080)

    # runner.setup ran before the error; cleanup must have been awaited.
    runner_mock.setup.assert_awaited_once()
    runner_mock.cleanup.assert_awaited_once()
