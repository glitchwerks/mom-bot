"""Regression tests: missing Authorization header returns 403 on all
Bearer-protected endpoints.

Mirrors ``siege-web/backend/tests/integration/sidecar/test_auth.py:29-134``
which asserts ``response.status_code == 403`` for missing-header across all
four protected endpoints.

Issue: glitchwerks/mom-bot#186
Contract source: siege-web/backend/tests/integration/sidecar/test_auth.py
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
# Constants
# ---------------------------------------------------------------------------

_VALID_KEY = "test-bearer-key-auth-regression"
_KNOWN_MEMBER_ID = "111000111000111001"


# ---------------------------------------------------------------------------
# Minimal fakes — just enough for app construction
# ---------------------------------------------------------------------------


class _FakeBot:
    """Minimal stand-in for discord.Client.

    Attributes:
        None
    """

    def is_ready(self) -> bool:
        """Always reports ready.

        Returns:
            True always.
        """
        return True


class _FakeGuild:
    """Minimal stand-in for discord.Guild.

    Attributes:
        members: Empty member list.
    """

    def __init__(self) -> None:
        """Initialise with an empty member list."""
        self.members: list[Any] = []

    async def fetch_member(self, user_id: int) -> None:
        """Raise discord.NotFound for any ID.

        Args:
            user_id: Discord snowflake (unused).

        Raises:
            discord.NotFound: Always.
        """
        response = MagicMock()
        response.status = 404
        response.reason = "Unknown Member"
        raise discord.NotFound(response, "Unknown Member")


# ---------------------------------------------------------------------------
# Helpers
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


def _make_client() -> TestClient:
    """Build a TestClient wrapping the sidecar app.

    Returns:
        A :class:`~fastapi.testclient.TestClient` for the app.
    """
    app = build_app(
        api_key=_VALID_KEY,
        bot=_FakeBot(),  # type: ignore[arg-type]
        guild=_FakeGuild(),  # type: ignore[arg-type]
        session_factory=_make_session_factory(),
    )
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Parametrized missing-header → 403 regression
# ---------------------------------------------------------------------------

# All currently-Bearer-protected endpoints.
# /api/internal/role-sync is also Bearer-protected; include it for completeness.
_PROTECTED_ENDPOINTS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "GET",
        "/api/members",
        {},
    ),
    (
        "GET",
        f"/api/members/{_KNOWN_MEMBER_ID}",
        {},
    ),
    (
        "POST",
        "/api/internal/role-sync",
        {
            "discord_id": "111",
            "siege_id": 1,
            "day_number": 1,
            "action": "assign",
            "assigned_at": "2024-01-01T00:00:00Z",
            "correlation_id": "test-corr-id",
        },
    ),
]


@pytest.mark.parametrize(
    "method,path,body",
    _PROTECTED_ENDPOINTS,
    ids=[
        "GET /api/members",
        "GET /api/members/{id}",
        "POST /api/internal/role-sync",
    ],
)
def test_missing_auth_header_returns_403(
    method: str,
    path: str,
    body: dict[str, Any],
) -> None:
    """No Authorization header must produce HTTP 403 on every protected endpoint.

    Mirrors siege-web/backend/tests/integration/sidecar/test_auth.py:29-134.
    Two distinct failure modes are required:
    - 403 when the Authorization header is absent entirely.
    - 401 + WWW-Authenticate: Bearer when present with a wrong token.

    Args:
        method: HTTP method string (GET or POST).
        path: Request path.
        body: JSON body (ignored for GET; sent for POST).
    """
    client = _make_client()
    if method == "GET":
        response = client.get(path)
    else:
        response = client.post(path, json=body)
    assert response.status_code == 403, (
        f"{method} {path} without Authorization header must return 403; "
        f"got {response.status_code}"
    )


@pytest.mark.parametrize(
    "method,path,body",
    _PROTECTED_ENDPOINTS,
    ids=[
        "GET /api/members",
        "GET /api/members/{id}",
        "POST /api/internal/role-sync",
    ],
)
def test_missing_auth_header_body_has_detail(
    method: str,
    path: str,
    body: dict[str, Any],
) -> None:
    """403 response for missing header must contain a 'detail' string key.

    Args:
        method: HTTP method string (GET or POST).
        path: Request path.
        body: JSON body (ignored for GET; sent for POST).
    """
    client = _make_client()
    if method == "GET":
        response = client.get(path)
    else:
        response = client.post(path, json=body)
    data = response.json()
    assert "detail" in data, f"{method} {path} 403 body must have 'detail' key; got: {data!r}"
    assert isinstance(
        data["detail"], str
    ), f"'detail' must be a string; got: {type(data['detail'])!r}"
