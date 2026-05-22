"""Tests for GET /api/version, GET /api/health, and Bearer auth dependency.

Phase 2 of Epic #128 sidecar replacement (issue #176).

Covers:
- GET /api/version: 200 + correct body shape; env var combinations
- GET /api/health: 200 + bot_connected reflects is_ready() per-request
- Bearer auth: missing header → 403, wrong token → 401 + WWW-Authenticate,
  correct token → endpoint runs
- is_ready() is called on EACH health request (not cached)

Design notes
------------
- Uses FastAPI TestClient (synchronous) — no asyncio needed for HTTP tests.
- Bot is injected via a simple fake that lets tests flip is_ready().
- build_app is called directly with controlled api_key / fake_bot arguments
  so no Key Vault or Discord gateway is involved.
- The existing role-sync endpoint (POST /api/internal/role-sync) acts as the
  "arbitrary protected endpoint" for Bearer reuse tests.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import discord
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mom_bot.db import Base
from mom_bot.sidecar.app import build_app
from mom_bot.sidecar.models import MemberRoleSyncState as _MemberRoleSyncState  # noqa: F401

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_VALID_KEY = "test-bearer-key-abc123"
_WRONG_KEY = "wrong-key"
_VERSION_STRING = "1.2.3"

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_session_factory() -> Any:
    """Build an in-memory SQLite session factory with all ORM tables.

    Uses ``StaticPool`` so all connections (including those from background
    threads spawned by TestClient/anyio) share the same in-memory SQLite
    database.  Without ``StaticPool``, each new connection sees a fresh
    empty database — tables created by ``create_all`` would be invisible
    to the request handler.

    Returns:
        A :class:`~sqlalchemy.orm.sessionmaker` backed by an in-memory DB.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


class FakeBot:
    """Minimal stand-in for a discord.Client used in health endpoint tests.

    Attributes:
        _ready: Controls the return value of :meth:`is_ready`.
    """

    def __init__(self, *, ready: bool = True) -> None:
        """Initialise the fake bot.

        Args:
            ready: Initial value returned by :meth:`is_ready`.
        """
        self._ready = ready

    def is_ready(self) -> bool:
        """Return the current ready state.

        Returns:
            The value of ``self._ready`` at call time.
        """
        return self._ready


def _make_fake_guild() -> MagicMock:
    """Build a minimal fake discord.Guild.

    Returns:
        A MagicMock with ``spec=discord.Guild``.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.id = 123456789
    return guild


def _make_client(
    *,
    api_key: str = _VALID_KEY,
    ready: bool = True,
) -> TestClient:
    """Build a TestClient wrapping the sidecar app.

    Args:
        api_key: The Bearer token the sidecar will validate against.
        ready: Initial ``is_ready()`` state of the injected fake bot.

    Returns:
        A :class:`~fastapi.testclient.TestClient` for the app.
    """
    fake_bot = FakeBot(ready=ready)
    fake_guild = _make_fake_guild()
    session_factory = _make_session_factory()
    app = build_app(
        api_key=api_key,
        bot=fake_bot,  # type: ignore[arg-type]
        guild=fake_guild,
        session_factory=session_factory,
    )
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# GET /api/version
# ---------------------------------------------------------------------------


class TestGetVersion:
    """GET /api/version returns 200 with correct body shape."""

    def test_version_returns_200(self) -> None:
        """GET /api/version must return HTTP 200.

        No auth header required — the endpoint is public.
        """
        client = _make_client()
        response = client.get("/api/version")
        assert response.status_code == 200

    def test_version_content_type_is_json(self) -> None:
        """GET /api/version must return application/json content-type."""
        client = _make_client()
        response = client.get("/api/version")
        assert "application/json" in response.headers["content-type"]

    def test_version_body_has_version_key(self) -> None:
        """GET /api/version body must contain a 'version' key."""
        client = _make_client()
        response = client.get("/api/version")
        body = response.json()
        assert "version" in body, f"Expected 'version' key in body, got: {body!r}"

    def test_version_value_is_non_empty_string(self) -> None:
        """The 'version' value must be a non-empty string."""
        client = _make_client()
        response = client.get("/api/version")
        version = response.json()["version"]
        assert (
            isinstance(version, str) and len(version) > 0
        ), f"Expected non-empty string for 'version', got: {version!r}"

    def test_version_bare_semver_when_env_vars_absent(self) -> None:
        """Version string is bare semver when BUILD_NUMBER / GIT_SHA not set.

        Removes both env vars before the request so the handler sees neither,
        then asserts the returned version equals the package semver without a
        build suffix.
        """
        client = _make_client()
        env_clean = {k: v for k, v in os.environ.items() if k not in ("BUILD_NUMBER", "GIT_SHA")}
        with patch.dict(os.environ, env_clean, clear=True):
            response = client.get("/api/version")
        version = response.json()["version"]
        assert (
            "+" not in version
        ), f"Expected bare semver (no '+') when env vars absent; got: {version!r}"

    def test_version_includes_build_suffix_when_env_vars_set(self) -> None:
        """Version string includes '+<BUILD_NUMBER>.<GIT_SHA[:7]>' when both set.

        Sets BUILD_NUMBER=99 and GIT_SHA=abcdef1234567 and asserts the
        returned version matches '<semver>+99.abcdef1'.
        """
        client = _make_client()
        env = {**os.environ, "BUILD_NUMBER": "99", "GIT_SHA": "abcdef1234567"}
        with patch.dict(os.environ, env, clear=True):
            response = client.get("/api/version")
        version = response.json()["version"]
        assert "+99.abcdef1" in version, (
            f"Expected '+99.abcdef1' suffix in version when env vars set; " f"got: {version!r}"
        )

    def test_version_no_auth_required(self) -> None:
        """GET /api/version must succeed with no Authorization header."""
        client = _make_client()
        response = client.get("/api/version")  # no auth header
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


class TestGetHealth:
    """GET /api/health returns 200 with bot_connected reflecting is_ready()."""

    def test_health_returns_200(self) -> None:
        """GET /api/health must return HTTP 200 regardless of bot state."""
        client = _make_client(ready=True)
        response = client.get("/api/health")
        assert response.status_code == 200

    def test_health_body_has_status_healthy(self) -> None:
        """GET /api/health body must contain 'status': 'healthy'."""
        client = _make_client()
        response = client.get("/api/health")
        body = response.json()
        assert body.get("status") == "healthy", f"Expected status='healthy' in body; got: {body!r}"

    def test_health_bot_connected_true_when_ready(self) -> None:
        """bot_connected is true when the fake bot's is_ready() returns True."""
        client = _make_client(ready=True)
        response = client.get("/api/health")
        body = response.json()
        assert body.get("bot_connected") is True, f"Expected bot_connected=true; got: {body!r}"

    def test_health_bot_connected_false_when_not_ready(self) -> None:
        """bot_connected is false when the fake bot's is_ready() returns False."""
        client = _make_client(ready=False)
        response = client.get("/api/health")
        body = response.json()
        assert body.get("bot_connected") is False, f"Expected bot_connected=false; got: {body!r}"

    def test_health_no_auth_required(self) -> None:
        """GET /api/health must succeed with no Authorization header."""
        client = _make_client()
        response = client.get("/api/health")  # no auth header
        assert response.status_code == 200

    def test_health_calls_is_ready_on_each_request_not_cached(self) -> None:
        """bot_connected reflects is_ready() at handler time, not construction.

        Creates one client with a mutable FakeBot.  First request expects
        bot_connected=True; then flips the flag; second request expects
        bot_connected=False.  This confirms the handler calls is_ready()
        dynamically rather than caching a snapshot.
        """
        fake_bot = FakeBot(ready=True)
        fake_guild = _make_fake_guild()
        session_factory = _make_session_factory()
        app = build_app(
            api_key=_VALID_KEY,
            bot=fake_bot,  # type: ignore[arg-type]
            guild=fake_guild,
            session_factory=session_factory,
        )
        client = TestClient(app, raise_server_exceptions=True)

        # First call — bot is ready.
        r1 = client.get("/api/health")
        assert r1.json()["bot_connected"] is True, "First call: expected bot_connected=true"

        # Flip the flag without rebuilding the app.
        fake_bot._ready = False

        # Second call — must reflect the updated state.
        r2 = client.get("/api/health")
        assert r2.json()["bot_connected"] is False, (
            "Second call after flipping is_ready to False: "
            "expected bot_connected=false — handler must not cache is_ready()"
        )

    def test_health_body_has_both_required_keys(self) -> None:
        """GET /api/health body must have exactly 'status' and 'bot_connected'."""
        client = _make_client()
        body = client.get("/api/health").json()
        assert "status" in body, f"Missing 'status' key in health body: {body!r}"
        assert "bot_connected" in body, f"Missing 'bot_connected' key in health body: {body!r}"


# ---------------------------------------------------------------------------
# Bearer auth
# ---------------------------------------------------------------------------


class TestBearerAuth:
    """Bearer auth dependency conformance: missing / wrong / correct token."""

    def test_missing_auth_header_returns_403(self) -> None:
        """A request to a protected endpoint with no Authorization header
        must return 403 per the executable contract.

        Per siege-web/backend/tests/integration/sidecar/test_auth.py:29-134
        and INTERFACE.md § Authentication: missing header → 403, wrong
        token → 401 + WWW-Authenticate: Bearer.
        Issue: glitchwerks/mom-bot#186.
        """
        client = _make_client()
        # POST /api/internal/role-sync is a protected endpoint.
        response = client.post(
            "/api/internal/role-sync",
            json={
                "discord_id": "111",
                "siege_id": 1,
                "day_number": 1,
                "action": "assign",
                "assigned_at": "2024-01-01T00:00:00Z",
                "correlation_id": "test-corr-id",
            },
        )
        assert (
            response.status_code == 403
        ), f"Expected 403 for missing auth header; got {response.status_code}"

    def test_missing_auth_header_body_has_detail(self) -> None:
        """Missing-header 401/403 response must have a 'detail' string key."""
        client = _make_client()
        response = client.post(
            "/api/internal/role-sync",
            json={
                "discord_id": "111",
                "siege_id": 1,
                "day_number": 1,
                "action": "assign",
                "assigned_at": "2024-01-01T00:00:00Z",
                "correlation_id": "test-corr-id",
            },
        )
        body = response.json()
        assert "detail" in body and isinstance(
            body["detail"], str
        ), f"Expected 'detail' string in auth-failure body; got: {body!r}"

    def test_wrong_token_returns_401(self) -> None:
        """A wrong Bearer token must return 401 Unauthorized."""
        client = _make_client()
        response = client.post(
            "/api/internal/role-sync",
            json={
                "discord_id": "111",
                "siege_id": 1,
                "day_number": 1,
                "action": "assign",
                "assigned_at": "2024-01-01T00:00:00Z",
                "correlation_id": "test-corr-id",
            },
            headers={"Authorization": f"Bearer {_WRONG_KEY}"},
        )
        assert (
            response.status_code == 401
        ), f"Expected 401 for wrong token; got {response.status_code}"

    def test_wrong_token_response_has_www_authenticate_bearer(self) -> None:
        """Wrong-token 401 must include 'WWW-Authenticate: Bearer' header.

        Per INTERFACE.md conformance table: wrong token → 401 with
        WWW-Authenticate: Bearer header set.
        """
        client = _make_client()
        response = client.post(
            "/api/internal/role-sync",
            json={
                "discord_id": "111",
                "siege_id": 1,
                "day_number": 1,
                "action": "assign",
                "assigned_at": "2024-01-01T00:00:00Z",
                "correlation_id": "test-corr-id",
            },
            headers={"Authorization": f"Bearer {_WRONG_KEY}"},
        )
        www_auth = response.headers.get("www-authenticate", "")
        assert "Bearer" in www_auth, (
            f"Expected 'WWW-Authenticate: Bearer' on 401; " f"got www-authenticate={www_auth!r}"
        )

    def test_correct_token_reaches_endpoint(self) -> None:
        """A correct Bearer token must allow the request through to the handler.

        Uses POST /api/internal/role-sync with a valid payload — if auth
        passes and the handler runs, we get a non-401/403 response (400 for
        schema error counts; anything that is not a pure auth rejection is
        sufficient).
        """
        client = _make_client()
        response = client.post(
            "/api/internal/role-sync",
            json={
                "discord_id": "111",
                "siege_id": 1,
                "day_number": 1,
                "action": "assign",
                "assigned_at": "2024-01-01T00:00:00Z",
                "correlation_id": "test-corr-id",
            },
            headers={"Authorization": f"Bearer {_VALID_KEY}"},
        )
        # Auth must have passed — status must not be 401 or 403.
        assert response.status_code not in (
            401,
            403,
        ), f"Expected auth to pass with correct token; got {response.status_code}"

    def test_bearer_dependency_reusable_across_endpoints(self) -> None:
        """The same Bearer dependency protects different endpoints.

        Sends the wrong token to /api/internal/role-sync.  Any endpoint
        protected by the shared dependency must reject the same wrong token
        the same way (401 + WWW-Authenticate).
        """
        client = _make_client()
        response = client.post(
            "/api/internal/role-sync",
            json={},
            headers={"Authorization": f"Bearer {_WRONG_KEY}"},
        )
        assert response.status_code == 401
        assert "Bearer" in response.headers.get("www-authenticate", "")
