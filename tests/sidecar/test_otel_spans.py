"""Tests verifying OpenTelemetry span instrumentation on the role-sync endpoint.

The role-sync request path must produce a trace span that carries the
request's ``correlation_id`` as a span attribute.  This test verifies
that instrumentation without requiring a live App Insights connection
string or network egress by substituting an ``InMemorySpanExporter``-
backed ``TracerProvider`` via ``opentelemetry.trace.set_tracer_provider``.

After each test the global ``TracerProvider`` is restored to a no-op
provider so the installed tracer does not leak between tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mom_bot.db import Base
from mom_bot.roles.service import RoleSyncResult
from mom_bot.sidecar.app import build_app

# ---------------------------------------------------------------------------
# Minimal fake bot
# ---------------------------------------------------------------------------


class _FakeBot:
    """Minimal stand-in for discord.Client."""

    def is_ready(self) -> bool:
        """Always reports ready."""
        return True


_FAKE_BOT = _FakeBot()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_TOKEN = "test-secret-token"
_DISCORD_ID = "123456789012345678"
_SIEGE_ID = 42
_DAY_NUMBER = 1
_ASSIGNED_AT = "2026-05-14T13:52:18.247Z"
_CORRELATION_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

_VALID_ASSIGN_PAYLOAD: dict[str, Any] = {
    "discord_id": _DISCORD_ID,
    "siege_id": _SIEGE_ID,
    "day_number": _DAY_NUMBER,
    "action": "assign",
    "assigned_at": _ASSIGNED_AT,
    "correlation_id": _CORRELATION_ID,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    """In-memory SQLite session factory with the role-sync state table.

    Returns:
        A bound session factory for an in-memory SQLite DB.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def mock_guild() -> MagicMock:
    """Minimal mock discord.Guild.

    Returns:
        A MagicMock representing a Discord guild.
    """
    guild = MagicMock()
    guild.id = 100_000_000_000_000_001
    return guild


@pytest.fixture()
def span_exporter() -> InMemorySpanExporter:
    """Install an InMemorySpanExporter-backed TracerProvider globally.

    Sets the global OTel TracerProvider to one backed by an
    ``InMemorySpanExporter`` using a ``SimpleSpanProcessor`` (synchronous
    — avoids buffering delays in tests).  Restores the original provider
    and resets the ``_TRACER_PROVIDER_SET_ONCE`` guard after the test so
    the installed tracer does not leak into other tests.

    The OTel SDK's ``set_tracer_provider`` uses an internal ``Once``
    guard (``_TRACER_PROVIDER_SET_ONCE``) that prevents overriding the
    provider after it has been set once.  Tests must reset this guard
    around each test, otherwise the provider set by the first test (or
    by module import side effects) wins for the entire pytest session.

    Yields:
        The ``InMemorySpanExporter`` that will capture spans during the
        test.
    """
    # Capture the before state so teardown restores it precisely.
    # ``trace.get_tracer_provider()`` returns the global ``_PROXY_TRACER_PROVIDER``
    # (a singleton) when no real provider has been set; we must NOT pass that
    # back through ``set_tracer_provider`` in teardown because doing so sets
    # ``_TRACER_PROVIDER = _PROXY_TRACER_PROVIDER``, which causes
    # ``get_tracer_provider()`` → proxy → ``_TRACER_PROVIDER.get_tracer()``
    # → proxy → ... (infinite recursion).  Instead, preserve the raw
    # ``_TRACER_PROVIDER`` pointer (which may be ``None``) and restore it
    # directly, bypassing ``set_tracer_provider`` entirely in teardown.
    original_raw_provider = trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    original_done = trace._TRACER_PROVIDER_SET_ONCE._done  # type: ignore[attr-defined]

    # Reset the Once guard so set_tracer_provider fires.
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    try:
        yield exporter
    finally:
        # Restore the previous provider by direct pointer assignment so we
        # never pass the proxy back into set_tracer_provider (see comment
        # above for why that causes infinite recursion).
        trace._TRACER_PROVIDER = original_raw_provider  # type: ignore[attr-defined]
        trace._TRACER_PROVIDER_SET_ONCE._done = original_done  # type: ignore[attr-defined]


@pytest.fixture()
def instrumented_client(
    session_factory: sessionmaker[Session],
    mock_guild: MagicMock,
    span_exporter: InMemorySpanExporter,
) -> TestClient:
    """FastAPI TestClient with InMemorySpanExporter installed globally.

    Patches ``apply_day_role`` to return an 'applied' result so the
    handler reaches its successful completion path (where the span is
    emitted with the ``correlation_id`` attribute).

    Args:
        session_factory: In-memory session factory.
        mock_guild: Mock discord.Guild.
        span_exporter: The in-memory exporter fixture (ensures the
            provider is installed before the app is built).

    Returns:
        A configured FastAPI TestClient.
    """
    applied = RoleSyncResult(
        status="applied",
        added=[300_000_000_000_000_001],
        removed=[],
        reason=None,
    )
    app = build_app(
        api_key=_VALID_TOKEN,
        bot=_FAKE_BOT,
        guild=mock_guild,
        session_factory=session_factory,
    )
    with patch(
        "mom_bot.sidecar.app.apply_day_role",
        new_callable=AsyncMock,
        return_value=applied,
    ):
        yield TestClient(app)


# ---------------------------------------------------------------------------
# Tests — RED phase: these must FAIL before implementation
# ---------------------------------------------------------------------------


class TestRoleSyncOtelSpan:
    """The role-sync handler emits a span carrying the correlation_id."""

    def test_role_sync_emits_span_with_correlation_id(
        self,
        instrumented_client: TestClient,
        span_exporter: InMemorySpanExporter,
    ) -> None:
        """POST /api/internal/role-sync produces a span with correlation_id.

        Verifies that after a successful role-sync request the global
        OTel ``TracerProvider`` (backed by ``InMemorySpanExporter``) has
        captured at least one finished span whose attributes include
        ``correlation_id`` set to the value from the request body.

        Args:
            instrumented_client: TestClient with in-memory exporter active.
            span_exporter: The exporter capturing spans for this test.
        """
        resp = instrumented_client.post(
            "/api/internal/role-sync",
            json=_VALID_ASSIGN_PAYLOAD,
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        )
        assert resp.status_code == 200

        finished_spans = span_exporter.get_finished_spans()
        assert (
            len(finished_spans) >= 1
        ), "Expected at least one finished OTel span after role-sync request"

        # Find the span that carries correlation_id as an attribute.
        matching = [
            s
            for s in finished_spans
            if s.attributes and s.attributes.get("correlation_id") == _CORRELATION_ID
        ]
        assert matching, (
            f"No span found with attribute correlation_id={_CORRELATION_ID!r}. "
            f"Finished spans: {[s.name for s in finished_spans]!r}"
        )

    def test_role_sync_span_name_identifies_endpoint(
        self,
        instrumented_client: TestClient,
        span_exporter: InMemorySpanExporter,
    ) -> None:
        """The correlation_id-bearing span has a descriptive name.

        Verifies the span name is ``"role_sync"`` so operators can
        identify it in the App Insights trace view without guessing.

        Args:
            instrumented_client: TestClient with in-memory exporter active.
            span_exporter: The exporter capturing spans for this test.
        """
        instrumented_client.post(
            "/api/internal/role-sync",
            json=_VALID_ASSIGN_PAYLOAD,
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        )

        matching = [
            s
            for s in span_exporter.get_finished_spans()
            if s.attributes and s.attributes.get("correlation_id") == _CORRELATION_ID
        ]
        assert matching, "No span with correlation_id found"
        assert matching[0].name == "role_sync"
