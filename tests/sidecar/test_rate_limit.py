"""Tests for app-layer rate limiting on /api/internal/* endpoints.

Issue #209 — pre-auth flood protection via slowapi.

Covers:
- Per-IP rate limit fires on /api/internal/role-sync when hammered past limit
- 429 response includes Retry-After header
- Rate-limit check runs BEFORE bearer auth: invalid token + over-limit → 429,
  not 403/401 (the load-bearing ordering invariant for issue #209)
- Unmetered paths (/api/version, /api/health) are NOT rate-limited
- Total rate limit fires independently of per-IP limit

Design notes
------------
- Uses FastAPI TestClient (sync); slowapi's in-memory storage is reset
  between tests via a monkeypatch on the Limiter's storage so tests are
  deterministic without freezegun.
- build_app is called with a low limit string so the test doesn't need to
  hammer 60 times per request.
- No real Discord API or Postgres is involved; mock pattern mirrors
  test_role_sync.py and test_version_health_auth.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mom_bot.db import Base
from mom_bot.sidecar.app import build_app
from mom_bot.sidecar.models import (  # noqa: F401
    MemberRoleSyncState as _MemberRoleSyncState,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_VALID_KEY = "test-rate-limit-key-abc123"
_WRONG_KEY = "wrong-key"

# The low limits used in tests — we override via env so the factory reads them.
_TEST_PER_IP_LIMIT = "3/minute"
_TEST_TOTAL_LIMIT = "10/minute"

# Minimal valid role-sync payload (action=assign requires day_number).
_ROLE_SYNC_PAYLOAD = {
    "discord_id": "123456789012345678",
    "siege_id": 1,
    "day_number": 1,
    "action": "assign",
    "assigned_at": "2026-05-27T00:00:00.000Z",
    "correlation_id": "00000000-0000-0000-0000-000000000001",
}

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_session_factory() -> Any:
    """Build an in-memory SQLite session factory with all ORM tables.

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


class _FakeBot:
    """Minimal stand-in for a discord.Client.

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
            The value of ``_ready`` at call time.
        """
        return self._ready


def _make_fake_guild() -> MagicMock:
    """Build a minimal fake discord.Guild.

    Returns:
        A MagicMock with ``spec=discord.Guild``.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.id = 111222333444555666
    return guild


def _make_client(
    *,
    per_ip: str = _TEST_PER_IP_LIMIT,
    total: str = _TEST_TOTAL_LIMIT,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> TestClient:
    """Build a TestClient with rate-limit env vars set to low test values.

    The env vars ``RATE_LIMIT_PER_IP`` and ``RATE_LIMIT_TOTAL`` are set
    before ``build_app`` is called so the factory picks them up.  If a
    ``monkeypatch`` fixture is supplied it is used (test-scoped teardown);
    otherwise ``os.environ`` is mutated directly and the caller is
    responsible for cleanup.

    Args:
        per_ip: ``RATE_LIMIT_PER_IP`` value for the test app.
        total: ``RATE_LIMIT_TOTAL`` value for the test app.
        monkeypatch: Optional pytest monkeypatch fixture for env teardown.

    Returns:
        A :class:`~fastapi.testclient.TestClient` wrapping the sidecar app.
    """
    import os

    if monkeypatch is not None:
        monkeypatch.setenv("RATE_LIMIT_PER_IP", per_ip)
        monkeypatch.setenv("RATE_LIMIT_TOTAL", total)
    else:
        os.environ["RATE_LIMIT_PER_IP"] = per_ip
        os.environ["RATE_LIMIT_TOTAL"] = total

    app = build_app(
        api_key=_VALID_KEY,
        bot=_FakeBot(),  # type: ignore[arg-type]
        guild=_make_fake_guild(),
        session_factory=_make_session_factory(),
    )
    return TestClient(app, raise_server_exceptions=False)


def _reset_limiter(client: TestClient) -> None:
    """Clear all rate-limit counters on the limiter attached to the app.

    slowapi stores per-key counters in its ``_storage`` backend.  Calling
    ``reset()`` on the storage wipes all counters so tests don't bleed into
    each other, avoiding the need for freezegun or per-test app construction.

    Args:
        client: The TestClient whose app's limiter will be reset.
    """
    limiter = client.app.state.limiter  # type: ignore[attr-defined]
    limiter._storage.reset()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Per-IP rate-limit tests
# ---------------------------------------------------------------------------


class TestPerIpRateLimit:
    """Per-IP rate limit on /api/internal/* fires and returns 429."""

    def test_under_limit_returns_non_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Requests within the per-IP limit must not be rate-limited.

        Sends exactly (limit - 1) requests and confirms none return 429.
        The responses may be 200/400/401/403 depending on payload/auth —
        the test only asserts that 429 does NOT appear.

        Args:
            monkeypatch: Pytest fixture for env var teardown.
        """
        client = _make_client(per_ip="3/minute", monkeypatch=monkeypatch)
        _reset_limiter(client)
        for _ in range(2):
            resp = client.post(
                "/api/internal/role-sync",
                json=_ROLE_SYNC_PAYLOAD,
                headers={"Authorization": f"Bearer {_VALID_KEY}"},
            )
            assert resp.status_code != 429, f"Expected non-429 under limit, got {resp.status_code}"

    def test_over_limit_returns_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The (limit + 1)th request from the same IP must return 429.

        Args:
            monkeypatch: Pytest fixture for env var teardown.
        """
        client = _make_client(per_ip="3/minute", monkeypatch=monkeypatch)
        _reset_limiter(client)
        # Exhaust the 3-per-minute limit with valid requests.
        for _ in range(3):
            client.post(
                "/api/internal/role-sync",
                json=_ROLE_SYNC_PAYLOAD,
                headers={"Authorization": f"Bearer {_VALID_KEY}"},
            )
        # The 4th request must be rate-limited.
        resp = client.post(
            "/api/internal/role-sync",
            json=_ROLE_SYNC_PAYLOAD,
            headers={"Authorization": f"Bearer {_VALID_KEY}"},
        )
        assert (
            resp.status_code == 429
        ), f"Expected 429 over limit, got {resp.status_code}: {resp.text}"

    def test_429_includes_retry_after_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 429 response must include a Retry-After header.

        The Retry-After header tells the caller when to retry.  Its presence
        is mandated by the issue #209 acceptance criteria.

        Args:
            monkeypatch: Pytest fixture for env var teardown.
        """
        client = _make_client(per_ip="1/minute", monkeypatch=monkeypatch)
        _reset_limiter(client)
        # Exhaust the 1-per-minute allowance.
        client.post(
            "/api/internal/role-sync",
            json=_ROLE_SYNC_PAYLOAD,
            headers={"Authorization": f"Bearer {_VALID_KEY}"},
        )
        # The second request must 429 with Retry-After.
        resp = client.post(
            "/api/internal/role-sync",
            json=_ROLE_SYNC_PAYLOAD,
            headers={"Authorization": f"Bearer {_VALID_KEY}"},
        )
        assert resp.status_code == 429
        assert "retry-after" in {
            k.lower() for k in resp.headers
        }, f"Expected Retry-After header in 429 response; headers={dict(resp.headers)}"


# ---------------------------------------------------------------------------
# Critical ordering invariant: rate-limit runs BEFORE bearer auth
# ---------------------------------------------------------------------------


class TestRateLimitBeforeAuth:
    """Rate-limit must fire before bearer auth — the load-bearing invariant.

    Issue #209 AC: a pre-auth flood must be stopped by the rate limiter
    without running the bearer token check.  This means an over-limit
    request with an *invalid* token must still return 429, not 403/401.

    If this test fails, the rate-limit is wired AFTER auth and the flood
    protection does not prevent compute exhaustion.
    """

    def test_invalid_token_over_limit_returns_429_not_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Over-limit request with wrong token must return 429, not 403/401.

        This is the key ordering invariant: rate-limit middleware fires
        before the bearer dependency, so the attacker never exercises the
        token check path regardless of what token they send.

        Args:
            monkeypatch: Pytest fixture for env var teardown.
        """
        client = _make_client(per_ip="2/minute", monkeypatch=monkeypatch)
        _reset_limiter(client)
        # Exhaust the limit using requests with the WRONG token.
        for _ in range(2):
            client.post(
                "/api/internal/role-sync",
                json=_ROLE_SYNC_PAYLOAD,
                headers={"Authorization": f"Bearer {_WRONG_KEY}"},
            )
        # The 3rd request — still invalid token — must return 429, not 403/401.
        resp = client.post(
            "/api/internal/role-sync",
            json=_ROLE_SYNC_PAYLOAD,
            headers={"Authorization": f"Bearer {_WRONG_KEY}"},
        )
        assert resp.status_code == 429, (
            f"Expected 429 (rate-limit before auth), got {resp.status_code}. "
            "This means rate-limit is wired AFTER bearer auth — fix the wiring."
        )

    def test_missing_token_over_limit_returns_429_not_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Over-limit request with no auth header must return 429, not 403.

        Same ordering invariant as above — missing header should hit the
        rate limiter wall before any auth dependency runs.

        Args:
            monkeypatch: Pytest fixture for env var teardown.
        """
        client = _make_client(per_ip="2/minute", monkeypatch=monkeypatch)
        _reset_limiter(client)
        # Exhaust the limit without any auth header.
        for _ in range(2):
            client.post("/api/internal/role-sync", json=_ROLE_SYNC_PAYLOAD)
        # The 3rd request must return 429, not 403.
        resp = client.post("/api/internal/role-sync", json=_ROLE_SYNC_PAYLOAD)
        assert resp.status_code == 429, (
            f"Expected 429 (rate-limit before auth), got {resp.status_code}. "
            "This means rate-limit is wired AFTER bearer auth — fix the wiring."
        )


# ---------------------------------------------------------------------------
# Unmetered paths must NOT be rate-limited
# ---------------------------------------------------------------------------


class TestUnmeteredPaths:
    """Health and version endpoints must remain unmetered.

    Rate limiting must apply ONLY to /api/internal/* routes.  Probing and
    monitoring paths must never return 429 regardless of request frequency.
    """

    def test_health_not_rate_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /api/health must not return 429 even when hammered.

        Args:
            monkeypatch: Pytest fixture for env var teardown.
        """
        client = _make_client(per_ip="1/minute", monkeypatch=monkeypatch)
        _reset_limiter(client)
        for _ in range(5):
            resp = client.get("/api/health")
            assert resp.status_code != 429, "GET /api/health must not be rate-limited"

    def test_version_not_rate_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /api/version must not return 429 even when hammered.

        Args:
            monkeypatch: Pytest fixture for env var teardown.
        """
        client = _make_client(per_ip="1/minute", monkeypatch=monkeypatch)
        _reset_limiter(client)
        for _ in range(5):
            resp = client.get("/api/version")
            assert resp.status_code != 429, "GET /api/version must not be rate-limited"


# ---------------------------------------------------------------------------
# Total (aggregate) rate-limit fires across distinct IPs
# ---------------------------------------------------------------------------


class TestTotalRateLimit:
    """Aggregate rate limit fires when total requests exceed the cap.

    RATE_LIMIT_TOTAL is a shared budget across all source IPs.  This test
    verifies that the 6th request returns 429 even though each request comes
    from a distinct IP (so per-IP buckets never overflow).

    This also validates that the custom XFF key function (Finding #2) reads
    X-Forwarded-For correctly so each distinct header value is treated as a
    distinct IP key.
    """

    def test_total_limit_fires_across_distinct_ips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """6th request returns 429 when RATE_LIMIT_TOTAL=5/minute.

        Each of the 6 requests carries a different X-Forwarded-For header.
        The per-IP limit is set to 2/minute so that if the key function
        ignores X-Forwarded-For and buckets all requests under one IP,
        the 3rd request would hit the per-IP limit (not the total limit).
        With the XFF key function working correctly, each request lands in
        its own per-IP bucket (all under-limit) and only the aggregate fires
        on the 6th request.

        This validates both the aggregate-limit path and that the XFF key
        function from Finding #2 reads X-Forwarded-For correctly.

        Args:
            monkeypatch: Pytest fixture for env var teardown.
        """
        # per-IP limit=2/minute (would fire at request 3 if XFF is ignored);
        # total=5/minute (should fire at request 6 if XFF is read correctly).
        client = _make_client(
            per_ip="2/minute",
            total="5/minute",
            monkeypatch=monkeypatch,
        )
        _reset_limiter(client)

        distinct_ips = [
            f"192.0.2.{i}" for i in range(1, 7)
        ]

        # First 5 requests from distinct IPs must NOT be rate-limited.
        # If XFF is not read, request 3 would 429 on per-IP (not total),
        # proving the key function is broken.
        for ip in distinct_ips[:5]:
            resp = client.post(
                "/api/internal/role-sync",
                json=_ROLE_SYNC_PAYLOAD,
                headers={
                    "Authorization": f"Bearer {_VALID_KEY}",
                    "X-Forwarded-For": ip,
                },
            )
            assert resp.status_code != 429, (
                f"Expected non-429 for request from {ip} "
                f"(within per-IP limit), got {resp.status_code}. "
                "If request 3+ fails here, the key function is not reading "
                "X-Forwarded-For — all requests are sharing one IP bucket."
            )

        # 6th request from yet another distinct IP must be rate-limited by
        # the aggregate RATE_LIMIT_TOTAL budget.
        resp = client.post(
            "/api/internal/role-sync",
            json=_ROLE_SYNC_PAYLOAD,
            headers={
                "Authorization": f"Bearer {_VALID_KEY}",
                "X-Forwarded-For": distinct_ips[5],
            },
        )
        assert resp.status_code == 429, (
            f"Expected 429 from aggregate total limit after 5 requests, "
            f"got {resp.status_code}: {resp.text}"
        )
