"""mom_bot.health — HTTP /healthz endpoint and scheduler heartbeat sentinel.

Exposes a lightweight ``GET /healthz`` endpoint via :mod:`aiohttp` that
reflects whether the reminder scheduler's event loop is alive.  The endpoint
is consumed by the Azure Container Apps liveness probe (httpGet type).

Architecture
------------
A module-level :data:`last_heartbeat` records the monotonic timestamp of the
most recent scheduler tick, or ``None`` when the scheduler has not yet ticked.
The scheduler calls :func:`record_heartbeat` at the top of every tick.  The
HTTP handler :func:`healthz_handler` compares the elapsed time against
:data:`HEARTBEAT_THRESHOLD_SECONDS` (60 s) and returns:

- ``200 OK`` — heartbeat is recent (scheduler alive).
- ``503 Service Unavailable`` — heartbeat is stale or was never recorded.

The server is started by :func:`start_health_server`, which binds to
``0.0.0.0:8080`` by default and runs in the same asyncio event loop as the
Discord bot.  Shutdown is performed by the caller (see :func:`start_health_server`
return values).

Port choice
-----------
ACA liveness probes require a **separate** port from the ingress target when
ingress is enabled.  In the current config ingress is disabled, so port 8080
is used exclusively for the probe.  When Epic 2.6 re-enables ingress (on
a different port), this module continues to bind 8080 without conflict.
"""

from __future__ import annotations

import time

from aiohttp import web

__all__ = [
    "HEARTBEAT_THRESHOLD_SECONDS",
    "last_heartbeat",
    "record_heartbeat",
    "healthz_handler",
    "start_health_server",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Seconds within which a heartbeat must have been recorded for the probe
#: to return 200.  Chosen to be comfortably shorter than ACA's
#: ``periodSeconds`` (30 s) × ``failureThreshold`` (3) = 90 s kill window,
#: but long enough to absorb a momentary scheduler hiccup.
HEARTBEAT_THRESHOLD_SECONDS: float = 60.0

# ---------------------------------------------------------------------------
# Heartbeat sentinel
# ---------------------------------------------------------------------------

#: Last monotonic timestamp when :func:`record_heartbeat` was called.
#: ``None`` means the scheduler has never ticked — guarantees 503 until the
#: first tick regardless of how small ``time.monotonic()`` is on the host
#: (e.g. a freshly-booted CI runner whose uptime is < 60 s).
last_heartbeat: float | None = None


def record_heartbeat() -> None:
    """Update the heartbeat sentinel to the current monotonic time.

    The scheduler calls this once per tick so the HTTP handler can compute
    how recently the loop was alive.  Thread-safe in a single-threaded
    asyncio process; the GIL protects the float assignment.
    """
    global last_heartbeat
    last_heartbeat = time.monotonic()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


async def healthz_handler(request: web.Request) -> web.Response:
    """Handle ``GET /healthz`` — return 200 if scheduler is alive, 503 otherwise.

    Compares :data:`last_heartbeat` against :data:`HEARTBEAT_THRESHOLD_SECONDS`.
    A value of ``None`` (never set) always produces 503.  A non-``None`` value
    is safe to compare directly without a positive-value guard — the sentinel
    distinction is now ``None`` vs. a real timestamp, not ``0.0`` vs. positive,
    which was ambiguous on freshly-booted hosts where
    ``time.monotonic() - threshold`` can be negative.

    Args:
        request: The aiohttp :class:`~aiohttp.web.Request` (unused — the
            endpoint is purely a health signal with no query parameters).

    Returns:
        :class:`~aiohttp.web.Response` with status 200 and body
        ``{"status":"ok"}`` when alive, or status 503 and body
        ``{"status":"unhealthy"}`` when the heartbeat is stale or absent.
    """
    if last_heartbeat is not None:
        elapsed = time.monotonic() - last_heartbeat
        if elapsed < HEARTBEAT_THRESHOLD_SECONDS:
            return web.Response(
                status=200,
                content_type="application/json",
                text='{"status":"ok"}',
            )
    return web.Response(
        status=503,
        content_type="application/json",
        text='{"status":"unhealthy"}',
    )


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


async def start_health_server(
    host: str = "0.0.0.0",
    port: int = 8080,
) -> tuple[web.AppRunner, web.TCPSite]:
    """Start the aiohttp health server in the current event loop.

    Creates a minimal :class:`~aiohttp.web.Application` with a single
    ``GET /healthz`` route, binds it to *host*:*port*, and returns the
    runner and site so the caller can shut them down cleanly on bot exit.

    Args:
        host: Bind address.  Defaults to ``"0.0.0.0"`` so ACA's probe
            can reach the container from any interface.
        port: TCP port to listen on.  Defaults to ``8080``.

    Returns:
        A ``(runner, site)`` tuple.  Call ``await runner.cleanup()`` on
        shutdown to close the server and release the port.
    """
    app = web.Application()
    app.router.add_get("/healthz", healthz_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port)
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    return runner, site
