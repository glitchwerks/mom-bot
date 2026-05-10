"""Container Apps exec liveness probe for the reminder scheduler.

Exits 0 if the scheduler is alive; 1 otherwise.  "Alive" means either:

- The sentinel file (default ``/tmp/scheduler-heartbeat``) was touched
  within the last 90 seconds, OR
- The container has been running for less than 120 seconds (cold-start
  grace period, so the probe does not kill the container before the
  scheduler has had a chance to produce its first heartbeat).

Sentinel path override
----------------------
The sentinel path is read from the ``MOM_BOT_LIVENESS_SENTINEL`` environment
variable so tests can redirect the probe at a tmp-dir file without
monkey-patching the module directly.

Uptime detection
----------------
Container uptime is derived from ``/proc/1/stat`` on Linux (Container Apps
runs Linux containers).  Field 22 of that file is the process start time in
clock ticks since boot; combined with ``/proc/uptime`` (seconds since boot),
the container age can be computed without ``psutil``.

On non-Linux hosts (e.g. Windows developer machines running the test suite),
``/proc`` does not exist.  In that case the uptime helper returns 0.0, which
means the cold-start grace is never used — tests control liveness via the
sentinel mtime directly.

Invocation
----------
::

    python -m mom_bot.health.liveness

Exits 0 (alive) or 1 (dead).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

__all__ = ["is_alive", "main"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FRESHNESS_THRESHOLD_SECONDS: float = 90.0
_COLD_START_GRACE_SECONDS: float = 120.0


# ---------------------------------------------------------------------------
# Patchable helpers
# ---------------------------------------------------------------------------


def _sentinel_path() -> Path:
    """Return the sentinel file path, honouring the env-var override.

    Returns:
        Path to the scheduler heartbeat sentinel file.
    """
    return Path(
        os.environ.get(
            "MOM_BOT_LIVENESS_SENTINEL",
            "/tmp/scheduler-heartbeat",
        )
    )


def _container_uptime_seconds() -> float:
    """Return the container (PID 1) uptime in seconds.

    Reads ``/proc/1/stat`` and ``/proc/uptime`` to compute how long the
    container has been running.  Field 22 of ``/proc/1/stat`` is the start
    time of the process in clock ticks since boot; ``/proc/uptime`` gives
    seconds since boot.

    On non-Linux platforms where ``/proc`` does not exist, returns ``0.0``
    so the cold-start grace does not interfere with test-controlled sentinel
    mtime assertions.

    Returns:
        Container uptime in seconds, or 0.0 if ``/proc`` is unavailable.
    """
    try:
        # /proc/uptime: "seconds_since_boot idle_time"
        uptime_text = Path("/proc/uptime").read_text()
        boot_uptime_seconds = float(uptime_text.split()[0])

        # /proc/1/stat: whitespace-delimited; field 22 (0-indexed: 21) is
        # the process starttime in clock ticks since system boot.
        stat_text = Path("/proc/1/stat").read_text()
        fields = stat_text.split()
        starttime_ticks = int(fields[21])

        # os.sysconf is POSIX-only; AttributeError is caught below on Windows.
        clk_tck: int = os.sysconf("SC_CLK_TCK")  # type: ignore[attr-defined, unused-ignore]
        starttime_seconds: float = starttime_ticks / clk_tck

        # Age of PID 1 = boot_uptime - starttime_from_boot
        uptime: float = boot_uptime_seconds - starttime_seconds
        return max(uptime, 0.0)
    except (OSError, IndexError, ValueError, AttributeError):
        return 0.0


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def is_alive() -> bool:
    """Determine whether the scheduler is alive.

    Returns:
        ``True`` if the scheduler is alive (sentinel is fresh OR cold-start
        grace applies); ``False`` otherwise.
    """
    sentinel = _sentinel_path()
    uptime = _container_uptime_seconds()

    if not sentinel.exists():
        # No sentinel yet — alive only if within cold-start grace.
        return uptime < _COLD_START_GRACE_SECONDS

    # Sentinel exists — check its freshness.
    age_seconds = time.time() - sentinel.stat().st_mtime
    if age_seconds < _FRESHNESS_THRESHOLD_SECONDS:
        return True

    # Sentinel is stale — alive only if within cold-start grace.
    return uptime < _COLD_START_GRACE_SECONDS


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Execute the liveness check and exit with the appropriate code.

    Exits:
        0: scheduler is alive.
        1: scheduler is dead or unresponsive.
    """
    if is_alive():
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
