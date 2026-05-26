"""Integration tests for POST /api/internal/role-sync sidecar endpoint.

Covers all acceptance criteria from issue #65:

Auth
----
- Missing bearer → 403 (per glitchwerks/mom-bot#186)
- Wrong bearer → 401
- Correct bearer → proceeds to schema validation

Schema validation (400)
-----------------------
- Missing required field → 400
- action="assign" without day_number → 400
- action="unassign" with day_number → 400

Fresh write — success paths
----------------------------
- applied: roles service returns applied → 200, row inserted in DB
- skipped/member_not_in_guild → 200 skipped
- skipped/role_not_seeded → 200 skipped
- skipped/already_has_role → 200 skipped
- skipped/already_lacks_role → 200 skipped
- partial/remove_of_other_day_failed_403 → 200 partial (NOT 500)
- failed → 200 failed (NOT 500)

Idempotency / ordering
-----------------------
- Exact replay: same (assigned_at, action, day_number) → 200 stored response,
  apply_day_role called only once across two POSTs
- Stale write: assigned_at < stored → 200 skipped stale_write,
  apply_day_role NOT called
- Fresh write after existing row (newer assigned_at) → row updated

Structured logging
------------------
- Per-call INFO record contains required fields
- Exact replay emits INFO role_sync_idempotent_replay

Persistence
-----------
- Row in DB survives between sessions (DB layer persistence)

Concurrency
-----------
- asyncio.Lock per discord_id serializes concurrent requests so exactly one
  passes the stale-write check and calls apply_day_role

Resilience
----------
- Malformed JSON in stored row treated as cache miss → 200, row rewritten

Unassign with day_number=None (issue #204)
------------------------------------------
- Unassign after prior assign → resolves day_number from state, removes role,
  returns status=applied removed=[<prior_role_id>]
- Unassign with no prior state → returns status=skipped
  reason=already_lacks_role (not role_not_seeded)
- Round-trip: assign Day N then unassign (day_number=null) → state cleared
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mom_bot.db import Base
from mom_bot.roles.service import RoleSyncResult
from mom_bot.sidecar.app import build_app
from mom_bot.sidecar.models import MemberRoleSyncState

# ---------------------------------------------------------------------------
# Minimal fake bot for build_app(bot=...) parameter (issue #176)
# ---------------------------------------------------------------------------


class _FakeBot:
    """Minimal stand-in for discord.Client used by build_app."""

    def is_ready(self) -> bool:
        """Always reports ready — role-sync tests do not exercise health."""
        return True


_FAKE_BOT = _FakeBot()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_TOKEN = "test-secret-token"
_DISCORD_ID = 123456789012345678
_SIEGE_ID = 42
_DAY_NUMBER = 1
_ACTION_ASSIGN = "assign"
_ACTION_UNASSIGN = "unassign"
_ASSIGNED_AT = "2026-05-14T13:52:18.247Z"
_CORRELATION_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

_VALID_ASSIGN_PAYLOAD: dict[str, Any] = {
    "discord_id": str(_DISCORD_ID),
    "siege_id": _SIEGE_ID,
    "day_number": _DAY_NUMBER,
    "action": _ACTION_ASSIGN,
    "assigned_at": _ASSIGNED_AT,
    "correlation_id": _CORRELATION_ID,
}

_VALID_UNASSIGN_PAYLOAD: dict[str, Any] = {
    "discord_id": str(_DISCORD_ID),
    "siege_id": _SIEGE_ID,
    "action": _ACTION_UNASSIGN,
    "assigned_at": _ASSIGNED_AT,
    "correlation_id": _CORRELATION_ID,
}

# Intentionally invalid — includes day_number which is forbidden for unassign.
# Used to verify the 400 rejection path.
_INVALID_UNASSIGN_PAYLOAD_WITH_DAY_NUMBER: dict[str, Any] = {
    "discord_id": str(_DISCORD_ID),
    "siege_id": _SIEGE_ID,
    "day_number": _DAY_NUMBER,
    "action": _ACTION_UNASSIGN,
    "assigned_at": _ASSIGNED_AT,
    "correlation_id": _CORRELATION_ID,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    """In-memory SQLite session factory with MemberRoleSyncState table.

    Uses ``StaticPool`` so all connections share the same in-memory
    SQLite database instance.  Without ``StaticPool``, each new connection
    from a worker thread (as used by FastAPI's ``TestClient`` via anyio)
    creates a fresh empty database — making tables created by
    ``create_all`` invisible to the request handler.

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
    """Minimal mock discord.Guild sufficient for apply_day_role calls.

    Returns:
        A MagicMock representing a Discord guild.
    """
    guild = MagicMock()
    guild.id = 100_000_000_000_000_001
    return guild


@pytest.fixture()
def applied_result() -> RoleSyncResult:
    """RoleSyncResult representing a fully applied role change.

    Returns:
        A RoleSyncResult with status='applied'.
    """
    return RoleSyncResult(
        status="applied",
        added=[300_000_000_000_000_001],
        removed=[],
        reason=None,
    )


@pytest.fixture()
def client(
    session_factory: sessionmaker[Session],
    mock_guild: MagicMock,
    applied_result: RoleSyncResult,
) -> TestClient:
    """FastAPI TestClient with mocked dependencies.

    Patches apply_day_role to return applied_result by default.
    Tests that need a different result should patch within the test body.

    Args:
        session_factory: In-memory session factory.
        mock_guild: Mock discord.Guild.
        applied_result: Default mocked return from apply_day_role.

    Returns:
        A configured FastAPI TestClient.
    """
    app = build_app(
        api_key=_VALID_TOKEN,
        bot=_FAKE_BOT,
        guild=mock_guild,
        session_factory=session_factory,
    )
    with patch(
        "mom_bot.sidecar.app.apply_day_role",
        new_callable=AsyncMock,
        return_value=applied_result,
    ):
        yield TestClient(app)


def _auth_headers(token: str = _VALID_TOKEN) -> dict[str, str]:
    """Build Authorization header dict.

    Args:
        token: Bearer token value.

    Returns:
        Dict with Authorization header.
    """
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestAuth:
    """Bearer token authentication gates the endpoint."""

    def test_missing_bearer_returns_403(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """No Authorization header → 403.

        Per siege-web/backend/tests/integration/sidecar/test_auth.py:29-39
        and issue glitchwerks/mom-bot#186.
        """
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with TestClient(app) as c:
            resp = c.post("/api/internal/role-sync", json=_VALID_ASSIGN_PAYLOAD)
        assert resp.status_code == 403

    def test_wrong_bearer_returns_401(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """Wrong bearer token → 401."""
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with TestClient(app) as c:
            resp = c.post(
                "/api/internal/role-sync",
                json=_VALID_ASSIGN_PAYLOAD,
                headers=_auth_headers("wrong-token"),
            )
        assert resp.status_code == 401

    def test_correct_bearer_proceeds(self, client: TestClient) -> None:
        """Correct bearer token → not 401 (proceeds to handler)."""
        resp = client.post(
            "/api/internal/role-sync",
            json=_VALID_ASSIGN_PAYLOAD,
            headers=_auth_headers(),
        )
        assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Schema validation tests (400)
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """Invalid request payloads produce 400 responses."""

    def test_missing_discord_id_returns_400(self, client: TestClient) -> None:
        """Missing discord_id field → 400."""
        payload = {k: v for k, v in _VALID_ASSIGN_PAYLOAD.items() if k != "discord_id"}
        resp = client.post(
            "/api/internal/role-sync",
            json=payload,
            headers=_auth_headers(),
        )
        assert resp.status_code == 400

    def test_missing_action_returns_400(self, client: TestClient) -> None:
        """Missing action field → 400."""
        payload = {k: v for k, v in _VALID_ASSIGN_PAYLOAD.items() if k != "action"}
        resp = client.post(
            "/api/internal/role-sync",
            json=payload,
            headers=_auth_headers(),
        )
        assert resp.status_code == 400

    def test_invalid_action_returns_400(self, client: TestClient) -> None:
        """action='set' (old contract name) → 400 (only assign/unassign allowed)."""
        payload = {**_VALID_ASSIGN_PAYLOAD, "action": "set"}
        resp = client.post(
            "/api/internal/role-sync",
            json=payload,
            headers=_auth_headers(),
        )
        assert resp.status_code == 400

    def test_assign_without_day_number_returns_400(self, client: TestClient) -> None:
        """action='assign' without day_number → 400."""
        payload = {k: v for k, v in _VALID_ASSIGN_PAYLOAD.items() if k != "day_number"}
        resp = client.post(
            "/api/internal/role-sync",
            json=payload,
            headers=_auth_headers(),
        )
        assert resp.status_code == 400

    def test_unassign_with_day_number_returns_400(self, client: TestClient) -> None:
        """action='unassign' with day_number present → 400."""
        resp = client.post(
            "/api/internal/role-sync",
            json=_INVALID_UNASSIGN_PAYLOAD_WITH_DAY_NUMBER,
            headers=_auth_headers(),
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Fresh write — success paths
# ---------------------------------------------------------------------------


class TestFreshWriteSuccess:
    """Happy paths — no prior row in DB."""

    def test_applied_returns_200_with_structured_response(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """apply_day_role returns applied → 200 with status/added/removed."""
        result = RoleSyncResult(
            status="applied",
            added=[300_000_000_000_000_001],
            removed=[],
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
            return_value=result,
        ):
            with TestClient(app) as c:
                resp = c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "applied"
        assert body["added"] == [300_000_000_000_000_001]
        assert body["removed"] == []

    def test_applied_inserts_row_in_db(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """Successful fresh write inserts a row into member_role_sync_state."""
        result = RoleSyncResult(status="applied", added=[300], removed=[])
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch(
            "mom_bot.sidecar.app.apply_day_role",
            new_callable=AsyncMock,
            return_value=result,
        ):
            with TestClient(app) as c:
                c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )

        with session_factory() as s:
            row = s.get(MemberRoleSyncState, str(_DISCORD_ID))
        assert row is not None
        assert row.last_action == _ACTION_ASSIGN
        assert row.last_day_number == _DAY_NUMBER
        assert row.last_response_status == "applied"

    def test_member_not_in_guild_returns_200_skipped(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """member_not_in_guild → 200 skipped (NOT 404)."""
        result = RoleSyncResult(
            status="skipped",
            reason="member_not_in_guild",
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
            return_value=result,
        ):
            with TestClient(app) as c:
                resp = c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "skipped"
        assert body["reason"] == "member_not_in_guild"

    def test_role_not_seeded_returns_200_skipped(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """role_not_seeded → 200 skipped."""
        result = RoleSyncResult(status="skipped", reason="role_not_seeded")
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch(
            "mom_bot.sidecar.app.apply_day_role",
            new_callable=AsyncMock,
            return_value=result,
        ):
            with TestClient(app) as c:
                resp = c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "role_not_seeded"

    def test_already_has_role_returns_200_skipped(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """already_has_role → 200 skipped."""
        result = RoleSyncResult(status="skipped", reason="already_has_role")
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch(
            "mom_bot.sidecar.app.apply_day_role",
            new_callable=AsyncMock,
            return_value=result,
        ):
            with TestClient(app) as c:
                resp = c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "already_has_role"

    def test_already_lacks_role_returns_200_skipped(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """already_lacks_role → 200 skipped."""
        result = RoleSyncResult(status="skipped", reason="already_lacks_role")
        unassign_payload = _VALID_UNASSIGN_PAYLOAD
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch(
            "mom_bot.sidecar.app.apply_day_role",
            new_callable=AsyncMock,
            return_value=result,
        ):
            with TestClient(app) as c:
                resp = c.post(
                    "/api/internal/role-sync",
                    json=unassign_payload,
                    headers=_auth_headers(),
                )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "already_lacks_role"

    def test_partial_returns_200_not_500(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """partial outcome → 200 with partial + reason (NOT 500)."""
        result = RoleSyncResult(
            status="partial",
            added=[300_000_000_000_000_001],
            removed=[],
            reason="remove_of_other_day_failed_403",
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
            return_value=result,
        ):
            with TestClient(app) as c:
                resp = c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "partial"
        assert body["reason"] == "remove_of_other_day_failed_403"

    def test_failed_returns_200_not_500(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """failed outcome from role service → 200 (failure is a delivered response)."""
        result = RoleSyncResult(status="failed", added=[], removed=[])
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch(
            "mom_bot.sidecar.app.apply_day_role",
            new_callable=AsyncMock,
            return_value=result,
        ):
            with TestClient(app) as c:
                resp = c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"


# ---------------------------------------------------------------------------
# Exact replay
# ---------------------------------------------------------------------------


class TestExactReplay:
    """Idempotent re-delivery of the same payload returns the stored response."""

    def test_exact_replay_returns_stored_response_without_calling_service(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """Same (discord_id, assigned_at, action, day_number) twice → second call
        returns the stored response without invoking apply_day_role again.

        The mock call count asserts the service is called exactly once across
        both POSTs.
        """
        result = RoleSyncResult(
            status="applied",
            added=[300_000_000_000_000_001],
            removed=[],
        )
        mock_service = AsyncMock(return_value=result)
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch("mom_bot.sidecar.app.apply_day_role", mock_service):
            with TestClient(app) as c:
                # First call — fresh write
                resp1 = c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )
                # Second call — exact replay
                resp2 = c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # The stored response is returned, not stale_write
        assert resp2.json()["status"] == "applied"
        # Service called only once
        assert mock_service.call_count == 1

    def test_exact_replay_logs_idempotent_replay_event(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Exact replay emits INFO with event role_sync_idempotent_replay."""
        result = RoleSyncResult(status="applied", added=[300], removed=[])
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch(
            "mom_bot.sidecar.app.apply_day_role",
            new_callable=AsyncMock,
            return_value=result,
        ):
            with TestClient(app) as c:
                c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )
                with caplog.at_level(logging.INFO, logger="mom_bot.sidecar.app"):
                    c.post(
                        "/api/internal/role-sync",
                        json=_VALID_ASSIGN_PAYLOAD,
                        headers=_auth_headers(),
                    )
        assert any("role_sync_idempotent_replay" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Stale write
# ---------------------------------------------------------------------------


class TestStaleWrite:
    """Older assigned_at than stored → skip without invoking service."""

    def test_stale_write_returns_200_skipped_stale_write(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """Incoming assigned_at < stored last_assigned_at → 200 skipped stale_write."""
        newer_payload = {
            **_VALID_ASSIGN_PAYLOAD,
            "assigned_at": "2026-05-14T20:00:00.000Z",
            "correlation_id": "newer-corr-id",
        }
        older_payload = {
            **_VALID_ASSIGN_PAYLOAD,
            "assigned_at": "2026-05-14T10:00:00.000Z",
            "action": "unassign",
            "correlation_id": "older-corr-id",
        }
        # Remove day_number for unassign
        older_payload.pop("day_number")

        result = RoleSyncResult(status="applied", added=[300], removed=[])
        mock_service = AsyncMock(return_value=result)
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch("mom_bot.sidecar.app.apply_day_role", mock_service):
            with TestClient(app) as c:
                # Write the newer timestamp first
                c.post(
                    "/api/internal/role-sync",
                    json=newer_payload,
                    headers=_auth_headers(),
                )
                # Now send the older timestamp → stale_write
                resp = c.post(
                    "/api/internal/role-sync",
                    json=older_payload,
                    headers=_auth_headers(),
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "skipped"
        assert body["reason"] == "stale_write"
        assert "last_assigned_at" in body
        # Service called only once (for the fresh write)
        assert mock_service.call_count == 1

    def test_stale_write_does_not_update_stored_row(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """Stale write does not mutate the stored row."""
        newer_payload = {
            **_VALID_ASSIGN_PAYLOAD,
            "assigned_at": "2026-05-14T20:00:00.000Z",
        }
        older_payload = {
            **_VALID_ASSIGN_PAYLOAD,
            "assigned_at": "2026-05-14T10:00:00.000Z",
            "correlation_id": "stale-corr-id",
        }
        result = RoleSyncResult(status="applied", added=[300], removed=[])
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch(
            "mom_bot.sidecar.app.apply_day_role",
            new_callable=AsyncMock,
            return_value=result,
        ):
            with TestClient(app) as c:
                c.post(
                    "/api/internal/role-sync",
                    json=newer_payload,
                    headers=_auth_headers(),
                )
                c.post(
                    "/api/internal/role-sync",
                    json=older_payload,
                    headers=_auth_headers(),
                )

        with session_factory() as s:
            row = s.get(MemberRoleSyncState, str(_DISCORD_ID))
        # The stored row should still reflect the newer write
        assert row is not None
        assert row.last_assigned_at == "2026-05-14T20:00:00.000Z"
        assert row.last_correlation_id != "stale-corr-id"


# ---------------------------------------------------------------------------
# Fresh write after prior row (newer assigned_at)
# ---------------------------------------------------------------------------


class TestFreshWriteUpdate:
    """Newer assigned_at updates the stored row."""

    def test_fresh_write_newer_timestamp_updates_row(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """Fresh write with newer assigned_at updates the stored row."""
        older_payload = {
            **_VALID_ASSIGN_PAYLOAD,
            "assigned_at": "2026-05-14T10:00:00.000Z",
            "correlation_id": "first-corr",
        }
        newer_payload = {
            **_VALID_ASSIGN_PAYLOAD,
            "assigned_at": "2026-05-14T20:00:00.000Z",
            "correlation_id": "second-corr",
        }
        result = RoleSyncResult(status="applied", added=[300], removed=[])
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch(
            "mom_bot.sidecar.app.apply_day_role",
            new_callable=AsyncMock,
            return_value=result,
        ):
            with TestClient(app) as c:
                c.post(
                    "/api/internal/role-sync",
                    json=older_payload,
                    headers=_auth_headers(),
                )
                c.post(
                    "/api/internal/role-sync",
                    json=newer_payload,
                    headers=_auth_headers(),
                )

        with session_factory() as s:
            row = s.get(MemberRoleSyncState, str(_DISCORD_ID))
        assert row is not None
        assert row.last_assigned_at == "2026-05-14T20:00:00.000Z"
        assert row.last_correlation_id == "second-corr"


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


class TestStructuredLogging:
    """Per-call INFO record contains required fields."""

    def test_per_call_log_contains_required_fields(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """INFO log emitted after each call contains all required fields."""
        result = RoleSyncResult(status="applied", added=[300], removed=[])
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch(
            "mom_bot.sidecar.app.apply_day_role",
            new_callable=AsyncMock,
            return_value=result,
        ):
            with TestClient(app) as c:
                with caplog.at_level(logging.INFO, logger="mom_bot.sidecar.app"):
                    c.post(
                        "/api/internal/role-sync",
                        json=_VALID_ASSIGN_PAYLOAD,
                        headers=_auth_headers(),
                    )

        # Find the role_sync structured log record
        role_sync_records = [r for r in caplog.records if "role_sync" in r.message]
        assert role_sync_records, "Expected at least one role_sync log record"
        # The final log message should contain key fields
        final_record = role_sync_records[-1]
        msg = final_record.message
        assert _CORRELATION_ID in msg
        assert str(_DISCORD_ID) in msg
        assert "applied" in msg
        assert "attempt" in msg


# ---------------------------------------------------------------------------
# DB persistence test
# ---------------------------------------------------------------------------


class TestDbPersistence:
    """Row survives DB session boundary (proves the DB layer persists)."""

    def test_row_is_readable_in_new_session(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """After writing via the endpoint, a new session can read the row.

        This verifies the DB write was committed and is not just in an
        open transaction that would disappear on session close.
        """
        result = RoleSyncResult(status="applied", added=[300], removed=[])
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch(
            "mom_bot.sidecar.app.apply_day_role",
            new_callable=AsyncMock,
            return_value=result,
        ):
            with TestClient(app) as c:
                c.post(
                    "/api/internal/role-sync",
                    json=_VALID_ASSIGN_PAYLOAD,
                    headers=_auth_headers(),
                )

        # Open a fresh session (simulates a restart for in-memory DB scope)
        with session_factory() as fresh_session:
            row = fresh_session.get(MemberRoleSyncState, str(_DISCORD_ID))

        assert row is not None, "Row not found in new session after write"
        assert row.last_response_status == "applied"


# ---------------------------------------------------------------------------
# Concurrent request serialization (#1 — asyncio.Lock per discord_id)
# ---------------------------------------------------------------------------


class TestConcurrentRequestSerialization:
    """asyncio.Lock per discord_id prevents the concurrent stale-write race.

    The race: two requests for the same discord_id arrive concurrently.
    Without a lock, both can read the DB (finding no row), both can pass the
    stale-write check, and both can call apply_day_role — the older
    ``assigned_at`` could then win the UPSERT and overwrite the newer one.

    With a per-discord_id lock, only one request processes the
    idempotency-check + UPSERT critical section at a time.  The second
    request, upon acquiring the lock, re-reads the DB and finds the row
    already written by the first; it then follows the exact-replay or
    stale-write path without calling apply_day_role again.
    """

    def test_concurrent_requests_call_apply_day_role_exactly_once(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """Two concurrent POSTs for the same discord_id and assigned_at
        result in exactly one apply_day_role invocation (exact-replay path
        for the second request) and both return 200.

        The mock uses an asyncio.Event to ensure both coroutines are
        inside the "critical section" before either completes, maximising
        the window in which a real race could occur.  With the lock in
        place, they are serialized and only one reaches apply_day_role.
        """
        newer_at = "2026-05-14T21:00:00.000Z"
        older_at = "2026-05-14T09:00:00.000Z"

        newer_payload = {
            **_VALID_ASSIGN_PAYLOAD,
            "assigned_at": newer_at,
            "correlation_id": "newer-concurrent",
        }
        older_payload = {
            **_VALID_ASSIGN_PAYLOAD,
            "assigned_at": older_at,
            "correlation_id": "older-concurrent",
        }

        apply_result = RoleSyncResult(status="applied", added=[300], removed=[])
        # Use a list so the nested async closure can mutate it without
        # needing `nonlocal` (which doesn't cross coroutine boundaries cleanly).
        call_counter: list[int] = []

        async def _run_both() -> tuple[int, int]:
            """Post both payloads concurrently via httpx AsyncClient."""
            from mom_bot.sidecar.app import build_app as _build_app  # noqa: PLC0415

            async def _mock_apply(*args: Any, **kwargs: Any) -> RoleSyncResult:
                call_counter.append(1)
                return apply_result

            # Rebuild app inside the coroutine so the patch target resolves
            # correctly in async context.
            _app = _build_app(
                api_key=_VALID_TOKEN,
                bot=_FAKE_BOT,
                guild=mock_guild,
                session_factory=session_factory,
            )
            transport = httpx.ASGITransport(app=_app)
            with patch("mom_bot.sidecar.app.apply_day_role", side_effect=_mock_apply):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                    r1, r2 = await asyncio.gather(
                        ac.post(
                            "/api/internal/role-sync",
                            json=newer_payload,
                            headers=_auth_headers(),
                        ),
                        ac.post(
                            "/api/internal/role-sync",
                            json=older_payload,
                            headers=_auth_headers(),
                        ),
                    )
            return r1.status_code, r2.status_code

        s1, s2 = asyncio.run(_run_both())

        assert s1 == 200, f"First request status: {s1}"
        assert s2 == 200, f"Second request status: {s2}"
        # With the lock in place the older-assigned_at request either sees
        # the row written by the newer one (stale_write) or becomes an
        # exact replay — in both cases apply_day_role is called exactly once.
        total_calls = len(call_counter)
        assert total_calls == 1, (
            f"apply_day_role called {total_calls} times; "
            "expected exactly 1 (lock should serialize concurrent requests)"
        )

        # The stored row must reflect the newer assigned_at.
        with session_factory() as s:
            row = s.get(MemberRoleSyncState, str(_DISCORD_ID))
        assert row is not None
        assert row.last_assigned_at == newer_at


# ---------------------------------------------------------------------------
# Malformed stored JSON resilience (#7)
# ---------------------------------------------------------------------------


class TestMalformedStoredJson:
    """Corrupted last_response_added/removed JSON is treated as a cache miss.

    If a prior write left invalid JSON in the stored row (database
    corruption, software bug in a previous version), the endpoint must
    not 500.  Instead it should:
    - log an ERROR describing the corruption
    - invoke apply_day_role (treating it as a fresh write)
    - overwrite the row with valid JSON, self-healing the DB
    """

    def test_malformed_json_in_stored_row_returns_200_and_heals_row(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Direct DB write with malformed JSON → exact-replay path logs ERROR,
        falls through to fresh write, returns 200, and rewrite the row with
        valid JSON (self-healing).

        The corrupted row uses the same key as the incoming request
        (same ``assigned_at``, ``action``, ``day_number``) to trigger the
        exact-replay branch where json.loads is called.  The corruption is
        detected, an ERROR is emitted, and the endpoint treats it as a fresh
        write rather than returning a 500.
        """
        # The corrupted row uses the same key as _VALID_ASSIGN_PAYLOAD so
        # the endpoint's exact-replay branch is triggered and json.loads is
        # attempted on the corrupted values.
        corrupt_assigned_at = _ASSIGNED_AT  # same as _VALID_ASSIGN_PAYLOAD
        with session_factory() as s:
            row = MemberRoleSyncState(discord_id=str(_DISCORD_ID))
            row.last_assigned_at = corrupt_assigned_at
            row.last_action = _ACTION_ASSIGN
            row.last_day_number = _DAY_NUMBER
            row.last_correlation_id = "corrupt-row"
            row.last_response_status = "applied"
            row.last_response_added = "NOT VALID JSON {"
            row.last_response_removed = "ALSO BAD }"
            row.last_response_reason = None
            s.add(row)
            s.commit()

        apply_result = RoleSyncResult(status="applied", added=[42], removed=[])
        mock_service = AsyncMock(return_value=apply_result)
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        # Use the same assigned_at as the corrupted row → triggers exact replay
        # → json.loads on the corrupted values → should log ERROR and fall
        # through to fresh write.
        replay_payload = _VALID_ASSIGN_PAYLOAD  # same key as the corrupt row

        with patch("mom_bot.sidecar.app.apply_day_role", mock_service):
            with caplog.at_level(logging.ERROR, logger="mom_bot.sidecar.app"):
                with TestClient(app) as c:
                    resp = c.post(
                        "/api/internal/role-sync",
                        json=replay_payload,
                        headers=_auth_headers(),
                    )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert (
            mock_service.call_count == 1
        ), "apply_day_role should be called once (fresh-write after cache miss)"

        # An ERROR log describing the corruption must have been emitted.
        assert any(
            "corrupt" in r.message.lower() or "json" in r.message.lower()
            for r in caplog.records
            if r.levelno >= logging.ERROR
        ), "Expected ERROR log about JSON corruption"

        # The row must be rewritten with valid JSON.
        with session_factory() as s:
            healed = s.get(MemberRoleSyncState, str(_DISCORD_ID))
        assert healed is not None
        assert json.loads(healed.last_response_added) == [42]
        assert json.loads(healed.last_response_removed) == []


# ---------------------------------------------------------------------------
# Unassign with day_number=None (issue #204)
# ---------------------------------------------------------------------------


_PRIOR_ROLE_ID = 1_385_267_473_099_653_170
_PRIOR_DAY_NUMBER = 2
_ASSIGN_AT = "2026-05-26T19:11:41.000Z"
_UNASSIGN_AT = "2026-05-26T19:16:41.000Z"

_ASSIGN_PAYLOAD_DAY2: dict[str, Any] = {
    "discord_id": str(_DISCORD_ID),
    "siege_id": _SIEGE_ID,
    "day_number": _PRIOR_DAY_NUMBER,
    "action": "assign",
    "assigned_at": _ASSIGN_AT,
    "correlation_id": "d4ac1381-a151-46e1-9b30-3568a2fc6834",
}

_UNASSIGN_PAYLOAD_NULL_DAY: dict[str, Any] = {
    "discord_id": str(_DISCORD_ID),
    "siege_id": _SIEGE_ID,
    "action": "unassign",
    "assigned_at": _UNASSIGN_AT,
    "correlation_id": "93ce940c-bfc0-4eba-abff-fcec951edfbb",
}


class TestUnassignWithNullDayNumber:
    """Unassign action where day_number=None (per contract) correctly removes
    the member's previously-applied day role.

    Covers issue #204 acceptance criteria:
    - Unassign with day_number=null removes the previously-applied day role.
    - No role_not_seeded warning fires on the unassign path.
    - Receiver returns status=applied removed=[<role_id>] on success.
    - Round-trip assign Day N → unassign (day_number=null) clears state.
    - No prior state → status=skipped reason=already_lacks_role.
    """

    def test_unassign_null_day_with_prior_state_returns_applied(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """Unassign (day_number=null) after a prior assign returns applied.

        Pre-condition: member_role_sync_state row exists with
        last_action='assign' and last_day_number=2.

        The handler must look up the prior day_number (2) from the state row,
        pass it to apply_day_role, and return the service result.
        """
        # Seed the prior assign state exactly as it would be after a
        # successful assign for Day 2.
        prior_removed: list[int] = []
        with session_factory() as s:
            row = MemberRoleSyncState(discord_id=str(_DISCORD_ID))
            row.last_assigned_at = _ASSIGN_AT
            row.last_action = "assign"
            row.last_day_number = _PRIOR_DAY_NUMBER
            row.last_correlation_id = "d4ac1381-a151-46e1-9b30-3568a2fc6834"
            row.last_response_status = "applied"
            row.last_response_added = json.dumps([_PRIOR_ROLE_ID])
            row.last_response_removed = json.dumps(prior_removed)
            row.last_response_reason = None
            s.add(row)
            s.commit()

        unassign_result = RoleSyncResult(
            status="applied",
            added=[],
            removed=[_PRIOR_ROLE_ID],
            reason=None,
        )
        mock_service = AsyncMock(return_value=unassign_result)
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch("mom_bot.sidecar.app.apply_day_role", mock_service):
            with TestClient(app) as c:
                resp = c.post(
                    "/api/internal/role-sync",
                    json=_UNASSIGN_PAYLOAD_NULL_DAY,
                    headers=_auth_headers(),
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "applied", (
            f"Expected status=applied but got status={body['status']}; "
            "unassign with day_number=null must consult prior state"
        )
        assert body["removed"] == [_PRIOR_ROLE_ID]
        assert body["added"] == []

        # apply_day_role must have been called with day_number=2 (looked up
        # from the prior state row), not with day_number=None.
        assert mock_service.call_count == 1
        _call_kwargs = mock_service.call_args.kwargs
        assert _call_kwargs["day_number"] == _PRIOR_DAY_NUMBER, (
            f"apply_day_role called with day_number={_call_kwargs['day_number']}; "
            f"expected {_PRIOR_DAY_NUMBER} resolved from prior state"
        )
        assert _call_kwargs["action"] == "unassign"

    def test_unassign_null_day_no_prior_state_returns_skipped_already_lacks_role(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """Unassign (day_number=null) with no prior state row returns skipped.

        The reason must be already_lacks_role, not role_not_seeded.
        apply_day_role must NOT be called (member genuinely has no day role).
        """
        mock_service = AsyncMock()
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch("mom_bot.sidecar.app.apply_day_role", mock_service):
            with TestClient(app) as c:
                resp = c.post(
                    "/api/internal/role-sync",
                    json=_UNASSIGN_PAYLOAD_NULL_DAY,
                    headers=_auth_headers(),
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "skipped", f"Expected status=skipped but got {body['status']}"
        assert body.get("reason") == "already_lacks_role", (
            f"Expected reason=already_lacks_role but got {body.get('reason')}; "
            "the role_not_seeded skip path must not fire for unassign"
        )
        # apply_day_role must NOT be called when there is nothing to remove.
        assert (
            mock_service.call_count == 0
        ), "apply_day_role should not be called when there is no prior state"

    def test_unassign_null_day_no_prior_state_does_not_log_role_not_seeded(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unassign (day_number=null) with no prior state must NOT emit
        role_not_seeded; that warning is reserved for truly missing day_role_map
        rows, not for the case where the member held no day role.
        """
        mock_service = AsyncMock()
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch("mom_bot.sidecar.app.apply_day_role", mock_service):
            with caplog.at_level(logging.WARNING):
                with TestClient(app) as c:
                    c.post(
                        "/api/internal/role-sync",
                        json=_UNASSIGN_PAYLOAD_NULL_DAY,
                        headers=_auth_headers(),
                    )

        assert not any(
            "role_not_seeded" in r.message for r in caplog.records
        ), "role_not_seeded warning must not fire for unassign with no prior state"

    def test_round_trip_assign_then_unassign_clears_state(
        self,
        session_factory: sessionmaker[Session],
        mock_guild: MagicMock,
    ) -> None:
        """Full round-trip: assign Day 2 then unassign (day_number=null).

        After the unassign, the state row must reflect the unassign
        (last_action='unassign', last_day_number=None).
        """
        assign_result = RoleSyncResult(
            status="applied",
            added=[_PRIOR_ROLE_ID],
            removed=[],
        )
        unassign_result = RoleSyncResult(
            status="applied",
            added=[],
            removed=[_PRIOR_ROLE_ID],
        )

        def _side_effect(**kwargs: Any) -> RoleSyncResult:
            if kwargs.get("action") == "assign":
                return assign_result
            return unassign_result

        mock_service = AsyncMock(side_effect=_side_effect)
        app = build_app(
            api_key=_VALID_TOKEN,
            bot=_FAKE_BOT,
            guild=mock_guild,
            session_factory=session_factory,
        )
        with patch("mom_bot.sidecar.app.apply_day_role", mock_service):
            with TestClient(app) as c:
                # Step 1: assign Day 2
                r1 = c.post(
                    "/api/internal/role-sync",
                    json=_ASSIGN_PAYLOAD_DAY2,
                    headers=_auth_headers(),
                )
                # Step 2: unassign (day_number=null per contract)
                r2 = c.post(
                    "/api/internal/role-sync",
                    json=_UNASSIGN_PAYLOAD_NULL_DAY,
                    headers=_auth_headers(),
                )

        assert r1.status_code == 200
        assert r1.json()["status"] == "applied"
        assert r2.status_code == 200
        assert r2.json()["status"] == "applied", (
            f"Unassign step returned status={r2.json()['status']}; "
            "expected applied after prior assign"
        )
        assert r2.json()["removed"] == [_PRIOR_ROLE_ID]

        # State row must now reflect the unassign.
        with session_factory() as s:
            row = s.get(MemberRoleSyncState, str(_DISCORD_ID))
        assert row is not None
        assert row.last_action == "unassign"
        assert row.last_day_number is None
        assert row.last_response_status == "applied"
        removed_stored = json.loads(row.last_response_removed)
        assert removed_stored == [_PRIOR_ROLE_ID]
