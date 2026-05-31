"""OpenTelemetry / Azure Monitor configuration for mom-bot.

Centralises telemetry bootstrap so both the Discord gateway process and
the FastAPI sidecar share the same initialisation path.

Startup contract
----------------
Call :func:`configure_azure_monitor` once at application startup, before
any ASGI lifespan or request handling begins.  If the
``APPLICATIONINSIGHTS_CONNECTION_STRING`` environment variable is absent
or empty (local dev, unit-test environments), the function logs a single
INFO message and returns without raising, leaving the global
``TracerProvider`` as whatever it was before the call (typically the OTel
no-op provider, or an ``InMemorySpanExporter`` provider installed by a
test fixture).

This guard means local development and the automated test suite never
require a live App Insights connection string.

Tracer access
-------------
Handler code that needs to create spans should obtain a tracer from the
global provider at call time rather than caching a tracer at module import.
The recommended pattern is::

    from opentelemetry import trace

    _tracer = trace.get_tracer(__name__)

    with _tracer.start_as_current_span("my_span") as span:
        span.set_attribute("key", "value")

Doing the ``get_tracer`` call at module level is also fine — the OTel API
returns a ``ProxyTracer`` that delegates to whichever real provider is
installed at the time a span is created.
"""

from __future__ import annotations

import logging
import os

_logger = logging.getLogger(__name__)

_CONN_STRING_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"


def configure_azure_monitor() -> None:
    """Configure the Azure Monitor OpenTelemetry distro at startup.

    Reads ``APPLICATIONINSIGHTS_CONNECTION_STRING`` from the environment.
    If the variable is absent or empty the function logs an INFO message
    and returns immediately without touching the global
    ``TracerProvider``.  This keeps local development and the test suite
    free from any requirement for a live App Insights endpoint.

    When the variable is present the function calls
    ``azure.monitor.opentelemetry.configure_azure_monitor`` which:

    - Installs an ``AzureMonitorTraceExporter``-backed ``TracerProvider``
      as the global OTel provider.
    - Registers auto-instrumentation for FastAPI, requests, and other
      supported libraries found in the current environment.
    - Exports traces, metrics, and logs to the App Insights workspace
      identified by the connection string.

    Safe to call multiple times; the OTel SDK's own ``Once`` guard
    prevents double-initialisation of the provider.

    Raises:
        Nothing — all exceptions from the Azure Monitor SDK are caught,
        logged at ERROR, and swallowed so a misconfigured connection
        string never crashes the bot.
    """
    conn_string = os.environ.get(_CONN_STRING_ENV, "").strip()
    if not conn_string:
        _logger.info(
            "Azure Monitor telemetry disabled: %s is not set",
            _CONN_STRING_ENV,
        )
        return

    try:
        # Import deferred so the module is importable even in environments
        # where azure-monitor-opentelemetry is not installed (though it IS
        # a declared dependency in pyproject.toml — this is belt-and-
        # suspenders for edge cases like stripped container images).
        from azure.monitor.opentelemetry import (
            configure_azure_monitor as _cam,
        )

        _cam(connection_string=conn_string)
        _logger.info("Azure Monitor OpenTelemetry exporter configured successfully")
    except Exception:
        _logger.error(
            "Failed to configure Azure Monitor OpenTelemetry exporter; "
            "telemetry will not be exported",
            exc_info=True,
        )
