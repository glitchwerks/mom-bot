"""Tests for mom_bot.health.liveness — exec liveness probe script.

TDD: tests were written before the implementation.  Each test covers one
discrete exit-code boundary of the probe logic.

Design notes
------------
- ``_sentinel_path()`` is made patchable via the ``MOM_BOT_LIVENESS_SENTINEL``
  env var, so tests point the probe at a tmp-dir file without monkey-patching.
- ``_container_uptime_seconds()`` is exposed as a public-ish helper so tests
  can patch it directly via ``monkeypatch``.
- ``main()`` is called in a subprocess via ``pytest``'s ``monkeypatch`` of
  env vars and via direct return-value checks on the helpers to keep tests
  deterministic and fast.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helper to import the module under test freshly
# ---------------------------------------------------------------------------


def _import_liveness() -> object:
    """Import mom_bot.health.liveness, reloading to pick up env-var changes."""
    import importlib
    import sys

    # Drop cached copy so env-var changes are picked up each time.
    sys.modules.pop("mom_bot.health.liveness", None)
    import mom_bot.health.liveness as liveness

    return importlib.reload(liveness)


# ---------------------------------------------------------------------------
# Test 1 — sentinel exists and is fresh → exit 0
# ---------------------------------------------------------------------------


def test_fresh_sentinel_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentinel mtime < 90s old and uptime > 120s → probe exits 0.

    Even with a container that is clearly past cold-start grace, a recently
    touched sentinel means the scheduler is alive.
    """
    sentinel = tmp_path / "scheduler-heartbeat"
    sentinel.touch()
    # Set mtime to "now" (within 90 s).
    # touch() already does this; no further action needed.

    monkeypatch.setenv("MOM_BOT_LIVENESS_SENTINEL", str(sentinel))
    liveness = _import_liveness()  # type: ignore[assignment]

    # Patch uptime to be well past cold-start grace (200 s).
    with patch.object(liveness, "_container_uptime_seconds", return_value=200.0):
        result = liveness.is_alive()

    assert result is True, "Fresh sentinel should report alive"


# ---------------------------------------------------------------------------
# Test 2 — sentinel exists but is stale, uptime > 120s → exit 1
# ---------------------------------------------------------------------------


def test_stale_sentinel_past_grace_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentinel mtime > 90s old, uptime > 120s → probe exits 1 (dead).

    The 91-second threshold is one second over the freshness limit; combined
    with well-past cold-start grace, the probe must declare the scheduler dead.
    """
    sentinel = tmp_path / "scheduler-heartbeat"
    sentinel.touch()
    # Back-date mtime to 91 s ago.
    stale_mtime = time.time() - 91
    os.utime(sentinel, (stale_mtime, stale_mtime))

    monkeypatch.setenv("MOM_BOT_LIVENESS_SENTINEL", str(sentinel))
    liveness = _import_liveness()  # type: ignore[assignment]

    with patch.object(liveness, "_container_uptime_seconds", return_value=200.0):
        result = liveness.is_alive()

    assert result is False, "Stale sentinel past grace period should report dead"


# ---------------------------------------------------------------------------
# Test 3 — sentinel stale but within cold-start grace → exit 0
# ---------------------------------------------------------------------------


def test_stale_sentinel_within_grace_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentinel mtime > 90s but uptime < 120s → probe exits 0 (grace period).

    During cold start the scheduler may not have ticked yet; the probe should
    stay green until the grace period expires.
    """
    sentinel = tmp_path / "scheduler-heartbeat"
    sentinel.touch()
    stale_mtime = time.time() - 91
    os.utime(sentinel, (stale_mtime, stale_mtime))

    monkeypatch.setenv("MOM_BOT_LIVENESS_SENTINEL", str(sentinel))
    liveness = _import_liveness()  # type: ignore[assignment]

    # 60 s uptime — still within 120 s cold-start grace.
    with patch.object(liveness, "_container_uptime_seconds", return_value=60.0):
        result = liveness.is_alive()

    assert result is True, "Stale sentinel within cold-start grace should report alive"


# ---------------------------------------------------------------------------
# Test 4 — sentinel absent, uptime < 120s → exit 0 (cold-start grace)
# ---------------------------------------------------------------------------


def test_absent_sentinel_within_grace_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sentinel file, uptime < 120s → probe exits 0 (cold-start).

    Before the scheduler has ticked even once the sentinel does not yet exist;
    the grace period must keep the probe green.
    """
    sentinel = tmp_path / "scheduler-heartbeat"
    # Do not create the file.
    assert not sentinel.exists()

    monkeypatch.setenv("MOM_BOT_LIVENESS_SENTINEL", str(sentinel))
    liveness = _import_liveness()  # type: ignore[assignment]

    with patch.object(liveness, "_container_uptime_seconds", return_value=30.0):
        result = liveness.is_alive()

    assert result is True, "Absent sentinel within grace period should report alive"


# ---------------------------------------------------------------------------
# Test 5 — sentinel absent, uptime > 120s → exit 1
# ---------------------------------------------------------------------------


def test_absent_sentinel_past_grace_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sentinel file, uptime > 120s → probe exits 1.

    If the scheduler never wrote the sentinel and the grace period has
    expired, the container must be restarted.
    """
    sentinel = tmp_path / "scheduler-heartbeat"
    assert not sentinel.exists()

    monkeypatch.setenv("MOM_BOT_LIVENESS_SENTINEL", str(sentinel))
    liveness = _import_liveness()  # type: ignore[assignment]

    with patch.object(liveness, "_container_uptime_seconds", return_value=200.0):
        result = liveness.is_alive()

    assert result is False, "Absent sentinel past grace period should report dead"


# ---------------------------------------------------------------------------
# Test 6 — boundary: mtime exactly 89s old → still alive (< 90s threshold)
# ---------------------------------------------------------------------------


def test_sentinel_89s_old_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentinel 89 s old → exits 0 (just within 90 s freshness window).

    Verifies the threshold is strictly ``< 90`` (or ``<= 90``), not off-by-one.
    """
    sentinel = tmp_path / "scheduler-heartbeat"
    sentinel.touch()
    fresh_mtime = time.time() - 89
    os.utime(sentinel, (fresh_mtime, fresh_mtime))

    monkeypatch.setenv("MOM_BOT_LIVENESS_SENTINEL", str(sentinel))
    liveness = _import_liveness()  # type: ignore[assignment]

    with patch.object(liveness, "_container_uptime_seconds", return_value=200.0):
        result = liveness.is_alive()

    assert result is True, "89 s-old sentinel should still be considered fresh"
