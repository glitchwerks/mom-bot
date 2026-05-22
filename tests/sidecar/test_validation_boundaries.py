"""Regression test: validation error status codes per API boundary.

Issue #187: the global RequestValidationError handler incorrectly returned
400 for body validation errors on sidecar endpoints. The sidecar contract
(INTERFACE.md line 301) requires 422; the role-sync ingestion contract
requires 400.

This module asserts the correct status per boundary so a future refactor
cannot silently regress either contract.

Boundaries
----------
- **Sidecar** (``/api/version``, ``/api/health``, ``/api/notify``,
  ``/api/post-message``, ``/api/post-image``, ``/api/members``,
  ``/api/members/{discord_user_id}``) — body/query validation errors
  must return **422**.  Path-parameter validation on
  ``/api/members/{discord_user_id}`` must also return **422**.
- **Role-sync ingestion** (``POST /api/internal/role-sync``) — body
  validation errors must return **400** (preserved from the pre-fix
  contract).

Design
------
Uses FastAPI TestClient (synchronous) with fake Discord objects.

The key RED test is
``TestSidecarValidationReturns422.
test_sidecar_body_validation_from_build_app_returns_422``:
it adds a canary POST endpoint to the app returned by ``build_app``, sends a
request that triggers a body ValidationError through the app's actual
exception handlers, and asserts the sidecar contract of 422.

Before the fix, ``build_app`` registers a single global handler that returns
400 for all body errors regardless of which endpoint they came from.  That
test therefore fails (gets 400 instead of 422).  After the structural fix
(separate handlers per boundary), it passes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.routing import Mount

from mom_bot.db import Base
from mom_bot.sidecar.app import build_app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_TOKEN = "boundary-test-token"

_VALID_ASSIGN_PAYLOAD: dict[str, Any] = {
    "discord_id": "123456789012345678",
    "siege_id": 1,
    "day_number": 1,
    "action": "assign",
    "assigned_at": "2026-05-22T00:00:00.000Z",
    "correlation_id": "aaaabbbb-cccc-dddd-eeee-ffffffffffff",
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeBot:
    """Minimal discord.Client stand-in."""

    def is_ready(self) -> bool:
        """Always ready for boundary tests."""
        return True


class _FakeGuild:
    """Minimal discord.Guild stand-in."""

    def __init__(self) -> None:
        """Initialise with an empty members list per instance."""
        self.members: list[Any] = []

    async def fetch_member(self, user_id: int) -> None:
        """Always raises NotFound — member lookup is not tested here."""
        response = MagicMock()
        response.status = 404
        response.reason = "Unknown Member"
        raise discord.NotFound(response, "Unknown Member")


_FAKE_BOT = _FakeBot()
_FAKE_GUILD = _FakeGuild()


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
    """Build a TestClient for boundary validation tests.

    Returns:
        A :class:`~fastapi.testclient.TestClient` wrapping the sidecar app.
    """
    app = build_app(
        api_key=_VALID_TOKEN,
        bot=_FAKE_BOT,  # type: ignore[arg-type]
        guild=_FAKE_GUILD,  # type: ignore[arg-type]
        session_factory=_make_session_factory(),
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth(token: str = _VALID_TOKEN) -> dict[str, str]:
    """Build an Authorization header dict.

    Args:
        token: Bearer token value.

    Returns:
        Dict with Authorization header.
    """
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Sidecar boundary — ALL validation errors → 422
# ---------------------------------------------------------------------------


class TestSidecarValidationReturns422:
    """Sidecar endpoints return 422 for validation errors.

    This class asserts the sidecar contract (INTERFACE.md line 301).
    The split must NOT depend on ``loc[0]`` inspection — it is expressed
    per-boundary in the app factory (issue #187).
    """

    def test_members_list_missing_auth_is_403_not_422(self) -> None:
        """Sanity: missing auth on /api/members is 403, not a validation error.

        Ensures the auth check fires before validation, so we do not confuse
        an auth rejection with a validation response.  Missing the
        Authorization header entirely → 403 (post-#188 contract); a wrong
        token → 401 + WWW-Authenticate: Bearer.
        """
        client = _make_client()
        response = client.get("/api/members")
        assert response.status_code == 403

    def test_member_detail_non_numeric_path_returns_422(self) -> None:
        """Non-numeric path param on /api/members/{id} → 422.

        Path-parameter validation is a sidecar validation error and must
        return 422 regardless of the ``loc[0]`` value in the error detail.
        """
        client = _make_client()
        response = client.get("/api/members/not-a-number", headers=_auth())
        assert response.status_code == 422

    def test_member_detail_non_numeric_path_response_shape(self) -> None:
        """422 body on path validation has the framework list shape.

        INTERFACE.md requires the detail field to be a list of error objects
        with loc/msg/type keys (the FastAPI default shape).
        """
        client = _make_client()
        response = client.get("/api/members/not-a-number", headers=_auth())
        assert response.status_code == 422
        data = response.json()
        assert isinstance(data["detail"], list)
        assert len(data["detail"]) > 0
        item = data["detail"][0]
        assert "loc" in item
        assert "msg" in item
        assert "type" in item

    def test_sidecar_path_validation_returns_422_not_400(self) -> None:
        """Path-parameter validation on a sidecar endpoint → 422, not 400.

        This is the primary regression assertion for issue #187.

        Before the fix, the global ``RequestValidationError`` handler used
        ``loc[0]``-based inspection: path errors → 422, body/query errors
        → 400.  After the structural fix, the sidecar sub-app's handler
        always returns 422 regardless of ``loc[0]``.

        This test verifies that path-parameter validation on the sidecar
        ``GET /api/members/{discord_user_id}`` endpoint returns 422 — which
        also proves the sidecar sub-app's 422 handler is in effect.  A
        parallel test for the role-sync boundary is in
        :class:`TestRoleSyncValidationReturns400`.
        """
        client = _make_client()
        # Non-numeric path param → RequestValidationError with loc[0]="path"
        response = client.get("/api/members/not-a-number", headers=_auth())
        assert response.status_code == 422, (
            f"Sidecar path validation must return 422 (issue #187), " f"got {response.status_code}"
        )
        data = response.json()
        assert isinstance(
            data["detail"], list
        ), "Detail must be a list per INTERFACE.md validation error shape"

    def test_sidecar_body_validation_from_build_app_returns_422(
        self,
    ) -> None:
        """Canary: sidecar sub-app returns 422 for body validation errors.

        This is the key regression test for issue #187 on the *body*
        validation path.  The parametrised
        ``test_validation_status_per_boundary`` only exercises path
        validation (``loc[0]="path"``), which also returned 422 before
        the fix.  This test exercises *body* validation — the case that
        was broken (returned 400) before the per-boundary split.

        A canary ``POST /test/canary`` endpoint is added to the sidecar
        sub-app (the sub-app mounted at ``/`` on the parent) after
        ``build_app`` returns.  Sending an empty body triggers a
        ``RequestValidationError`` with ``loc[0]="body"``, which must
        reach the sidecar sub-app's handler and return 422.

        The sidecar sub-app is resolved by walking ``app.routes`` and
        finding the :class:`~starlette.routing.Mount` with ``path="/"``
        — this is explicit and decoupled from mount registration order.
        """
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,  # type: ignore[arg-type]
            guild=_FakeGuild(),  # type: ignore[arg-type]
            session_factory=_make_session_factory(),
        )

        # Locate the sidecar sub-app by its mount path ("/"), not by
        # index, so the test survives future mount-order changes.
        sidecar_mounts = [r for r in app.routes if isinstance(r, Mount) and r.path == ""]
        assert sidecar_mounts, (
            "Expected a Mount with path='' (root) on the parent app — "
            "did build_app's mount structure change?"
        )
        sidecar_sub = sidecar_mounts[0].app

        class _CanaryBody(BaseModel):
            required_field: int

        @sidecar_sub.post("/test/canary")  # type: ignore[attr-defined]
        async def _canary(body: _CanaryBody) -> dict[str, bool]:
            return {"ok": True}

        client = TestClient(app, raise_server_exceptions=False)

        # Empty body → missing required_field → RequestValidationError
        # with loc[0]="body".  Must return 422 via the sidecar handler.
        response = client.post(
            "/test/canary",
            json={},
            headers=_auth(),
        )
        assert response.status_code == 422, (
            f"Sidecar body validation must return 422 (issue #187), " f"got {response.status_code}"
        )

        # Wrong type → also a body ValidationError → 422.
        response_wrong_type = client.post(
            "/test/canary",
            json={"required_field": "not-an-int"},
            headers=_auth(),
        )
        assert response_wrong_type.status_code == 422, (
            f"Sidecar body type-validation must return 422 (issue #187), "
            f"got {response_wrong_type.status_code}"
        )


# ---------------------------------------------------------------------------
# Role-sync boundary — body validation → 400
# ---------------------------------------------------------------------------


class TestRoleSyncValidationReturns400:
    """Role-sync ingestion endpoint returns 400 for body validation errors.

    This class asserts the role-sync contract (preserved from pre-fix
    behaviour, issue #187 AC).  Body validation errors on
    POST /api/internal/role-sync must continue to return 400.
    """

    def test_missing_required_field_returns_400(self) -> None:
        """Missing discord_id in role-sync body → 400 (not 422).

        This is the key preservation assertion: after the fix role-sync must
        still return 400 while sidecar endpoints return 422.
        """
        client = _make_client()
        payload = {k: v for k, v in _VALID_ASSIGN_PAYLOAD.items() if k != "discord_id"}
        response = client.post(
            "/api/internal/role-sync",
            json=payload,
            headers=_auth(),
        )
        assert response.status_code == 400

    def test_invalid_action_returns_400(self) -> None:
        """Invalid action value in role-sync body → 400.

        Confirms body-level enum validation on role-sync still returns 400.
        """
        client = _make_client()
        payload = {**_VALID_ASSIGN_PAYLOAD, "action": "invalid_action"}
        response = client.post(
            "/api/internal/role-sync",
            json=payload,
            headers=_auth(),
        )
        assert response.status_code == 400

    def test_assign_without_day_number_returns_400(self) -> None:
        """action='assign' without day_number on role-sync → 400.

        Model-validator (cross-field) errors are body errors and must
        return 400 on the role-sync boundary.
        """
        client = _make_client()
        payload = {k: v for k, v in _VALID_ASSIGN_PAYLOAD.items() if k != "day_number"}
        response = client.post(
            "/api/internal/role-sync",
            json=payload,
            headers=_auth(),
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Parametrized cross-boundary summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,payload,expected_status,label",
    [
        # Sidecar boundary: path validation → 422
        (
            "GET",
            "/api/members/not-a-number",
            None,
            422,
            "sidecar/path-validation",
        ),
        # Role-sync boundary: body validation → 400
        (
            "POST",
            "/api/internal/role-sync",
            {k: v for k, v in _VALID_ASSIGN_PAYLOAD.items() if k != "discord_id"},
            400,
            "role-sync/missing-body-field",
        ),
    ],
)
def test_validation_status_per_boundary(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    expected_status: int,
    label: str,
) -> None:
    """Each boundary returns the correct HTTP status for validation errors.

    Args:
        method: HTTP method (GET or POST).
        path: URL path to request.
        payload: JSON body dict (None for GET).
        expected_status: Expected HTTP status code.
        label: Human-readable label for the parametrize ID.
    """
    client = _make_client()
    kwargs: dict[str, Any] = {"headers": _auth()}
    if payload is not None:
        kwargs["json"] = payload

    if method == "GET":
        response = client.get(path, **kwargs)
    else:
        response = client.post(path, **kwargs)

    assert (
        response.status_code == expected_status
    ), f"[{label}] Expected {expected_status}, got {response.status_code}"
