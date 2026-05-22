"""Tests for POST /api/notify.

Phase 4 of Epic #128 sidecar replacement (issue #178).

Covers:
- POST /api/notify: 200 + {"status": "sent"} on success
- 404 when username not found in guild member cache
- Bearer auth: missing → 403; wrong → 401 + WWW-Authenticate; correct → passes
- Discord exception translation: Forbidden → 403; 4xx → 502; 5xx/timeout → 503
- Body validation: missing required field → 422

Contract sources:
  - siege-web/bot/INTERFACE.md § POST /api/notify
  - siege-web/backend/tests/integration/sidecar/test_notify.py (tests win on
    disagreement with prose)

Design notes
------------
- Uses FastAPI TestClient (synchronous) via ``_make_client`` helper.
- Member lookup is against ``guild.members`` cache (no live Discord API call).
- Discord exception translation is exercised by configuring ``FakeGuild`` to
  raise specific exceptions in ``send_dm()``.
- The ``build_app`` ``guild=`` parameter accepts any object duck-typed to the
  subset of ``discord.Guild`` the endpoints use.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mom_bot.db import Base
from mom_bot.sidecar.app import build_app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_KEY = "test-bearer-key-notify-xyz"
_WRONG_KEY = "wrong-key"

_KNOWN_USERNAME = "known-user"
_UNKNOWN_USERNAME = "unknown-ghost"


# ---------------------------------------------------------------------------
# Fake Discord objects
# ---------------------------------------------------------------------------


class FakeMember:
    """Minimal stand-in for discord.Member.

    Attributes:
        id: Snowflake integer.
        name: Discord username (used for cache lookup).
        display_name: Guild display name.
        dm_exc: If set, ``send()`` raises this exception to simulate DM
            failures (Forbidden, HTTPException, TimeoutError, etc.).
    """

    def __init__(
        self,
        member_id: int,
        name: str,
        display_name: str,
        dm_exc: Exception | None = None,
    ) -> None:
        """Initialise a fake member.

        Args:
            member_id: Integer snowflake.
            name: Discord username (``member.name`` — used for notify lookup).
            display_name: Guild display name.
            dm_exc: Optional exception to raise when ``send()`` is called,
                simulating DM delivery failures.
        """
        self.id = member_id
        self.name = name
        self.display_name = display_name
        self._dm_exc = dm_exc

    async def send(self, content: str) -> None:
        """Send a DM to this member, or raise a configured exception.

        Mirrors ``discord.Member.send()`` — the endpoint calls this
        directly to deliver the DM.

        Args:
            content: Message text to send.

        Raises:
            Exception: Whatever ``dm_exc`` is set to, if set.
        """
        if self._dm_exc is not None:
            raise self._dm_exc


class FakeGuild:
    """Minimal stand-in for discord.Guild for notify tests.

    Supplies ``.members`` (iterable for cache lookup) and
    ``.fetch_member()`` (not used by notify, but required for app
    construction).  Pass members with ``dm_exc`` set to simulate DM
    failures after member resolution.

    Attributes:
        members: List of :class:`FakeMember` objects.
    """

    def __init__(
        self,
        members: list[FakeMember] | None = None,
        dm_exc: Exception | None = None,
    ) -> None:
        """Initialise the fake guild.

        Args:
            members: Guild member list.  Defaults to one known member with
                no DM failure.
            dm_exc: If provided and ``members`` is not provided, the default
                known member is constructed with this ``dm_exc`` so DM sends
                raise it.  Ignored when ``members`` is supplied explicitly.
        """
        if members is not None:
            self.members: list[FakeMember] = members
        else:
            self.members = [
                FakeMember(
                    111000111000111001,
                    _KNOWN_USERNAME,
                    "Known User",
                    dm_exc=dm_exc,
                )
            ]

    async def fetch_member(self, user_id: int) -> FakeMember:
        """Not exercised by notify; returns matching member or raises NotFound.

        Args:
            user_id: Discord snowflake (unused by notify endpoint).

        Returns:
            Matching :class:`FakeMember` if found.

        Raises:
            discord.NotFound: If no member has this user_id.
        """
        for m in self.members:
            if m.id == user_id:
                return m
        response = MagicMock()
        response.status = 404
        response.reason = "Unknown Member"
        raise discord.NotFound(response, "Unknown Member")


class _FakeBot:
    """Minimal stand-in for discord.Client used by build_app."""

    def is_ready(self) -> bool:
        """Always reports ready — notify tests do not exercise health.

        Returns:
            True always.
        """
        return True


_FAKE_BOT = _FakeBot()


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


def _make_client(
    *,
    api_key: str = _VALID_KEY,
    guild: FakeGuild | None = None,
) -> TestClient:
    """Build a TestClient wrapping the sidecar app with a fake guild.

    Args:
        api_key: Bearer token the sidecar validates against.
        guild: Fake guild to inject; defaults to a standard guild with the
            known member.

    Returns:
        A :class:`~fastapi.testclient.TestClient` for the app.
    """
    app = build_app(
        api_key=api_key,
        bot=_FAKE_BOT,  # type: ignore[arg-type]
        guild=guild if guild is not None else FakeGuild(),  # type: ignore[arg-type]
        session_factory=_make_session_factory(),
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth(key: str = _VALID_KEY) -> dict[str, str]:
    """Return an Authorization header dict for the given key.

    Args:
        key: Bearer token value.

    Returns:
        Dict with ``Authorization`` key.
    """
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# POST /api/notify — auth
# ---------------------------------------------------------------------------


class TestNotifyAuth:
    """Bearer auth gates POST /api/notify."""

    def test_missing_auth_returns_403(self) -> None:
        """No Authorization header → 403.

        Per siege-web/backend/tests/integration/sidecar/test_auth.py
        and issue glitchwerks/mom-bot#186.
        """
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
        )
        assert response.status_code == 403

    def test_missing_auth_body_has_detail_string(self) -> None:
        """403 for missing header must contain a 'detail' string key."""
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
        )
        data = response.json()
        assert "detail" in data
        assert isinstance(data["detail"], str)

    def test_wrong_token_returns_401(self) -> None:
        """Wrong Bearer token → 401."""
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(_WRONG_KEY),
        )
        assert response.status_code == 401

    def test_wrong_token_has_www_authenticate_bearer(self) -> None:
        """Wrong-token 401 must include WWW-Authenticate: Bearer header."""
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(_WRONG_KEY),
        )
        assert "Bearer" in response.headers.get("www-authenticate", "")


# ---------------------------------------------------------------------------
# POST /api/notify — happy path
# ---------------------------------------------------------------------------


class TestNotifySuccess:
    """POST /api/notify returns 200 with status:sent for a known member."""

    def test_returns_200(self) -> None:
        """Valid auth + known username → 200."""
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hello!"},
            headers=_auth(),
        )
        assert response.status_code == 200

    def test_body_is_status_sent(self) -> None:
        """Response body is exactly {"status": "sent"}.

        Mirrors siege-web test_notify_known_user_returns_200_sent.
        """
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hello!"},
            headers=_auth(),
        )
        assert response.json() == {"status": "sent"}

    def test_case_insensitive_username_match(self) -> None:
        """Username lookup is case-insensitive per INTERFACE.md.

        The bundled sidecar uses case-insensitive matching
        (discord_client.py:25-29).
        """
        guild = FakeGuild(members=[FakeMember(111, "KnownUser", "Known User")])
        client = _make_client(guild=guild)
        response = client.post(
            "/api/notify",
            json={"username": "knownuser", "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/notify — 404 (member not in cache)
# ---------------------------------------------------------------------------


class TestNotifyNotFound:
    """Unknown username → 404 with detail string."""

    def test_unknown_username_returns_404(self) -> None:
        """Username not in guild member cache → 404.

        Mirrors test_notify_unknown_user_returns_404_with_detail.
        """
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"username": _UNKNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 404

    def test_404_body_has_detail_string(self) -> None:
        """404 response body must contain a 'detail' string key."""
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"username": _UNKNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        )
        data = response.json()
        assert "detail" in data
        assert isinstance(data["detail"], str)

    def test_empty_guild_returns_404(self) -> None:
        """An empty guild always returns 404 for any username."""
        client = _make_client(guild=FakeGuild(members=[]))
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/notify — body validation (422)
# ---------------------------------------------------------------------------


class TestNotifyValidation:
    """Missing required body fields → 422 per sidecar sub-app contract."""

    def test_missing_username_returns_422(self) -> None:
        """Missing 'username' field → 422.

        Mirrors test_notify_missing_username_returns_422.
        """
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 422

    def test_missing_username_detail_is_list(self) -> None:
        """422 detail must be a list with loc/msg/type items."""
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"message": "Hi"},
            headers=_auth(),
        )
        data = response.json()
        assert isinstance(data["detail"], list)
        assert len(data["detail"]) > 0
        item = data["detail"][0]
        assert "loc" in item
        assert "msg" in item
        assert "type" in item

    def test_missing_message_returns_422(self) -> None:
        """Missing 'message' field → 422.

        Mirrors test_notify_missing_message_returns_422.
        """
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME},
            headers=_auth(),
        )
        assert response.status_code == 422

    def test_empty_body_returns_422(self) -> None:
        """Empty body → 422."""
        client = _make_client()
        response = client.post(
            "/api/notify",
            json={},
            headers=_auth(),
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/notify — Discord exception translation
# ---------------------------------------------------------------------------


class TestNotifyDiscordExceptions:
    """Discord exceptions from DM send translate to correct HTTP codes.

    The member is resolved first (200 path through cache lookup), then
    the DM send is attempted.  Failures at the send step are translated
    by the exception handlers on _sidecar_sub.
    """

    def _make_dm_exc_guild(self, exc: Exception) -> FakeGuild:
        """Build a FakeGuild whose DM send raises ``exc``.

        Args:
            exc: Exception to raise when ``member.send()`` is called.

        Returns:
            A FakeGuild with the known member configured to raise ``exc``
            on DM send.
        """
        return FakeGuild(
            members=[FakeMember(111, _KNOWN_USERNAME, "Known User", dm_exc=exc)],
        )

    def test_discord_forbidden_translates_to_403(self) -> None:
        """discord.Forbidden from DM send → HTTP 403.

        Mirrors test_notify_dms_blocked_returns_403_with_permission_denied.
        """
        response_mock = MagicMock()
        response_mock.status = 403
        response_mock.reason = "Forbidden"
        exc = discord.Forbidden(response_mock, "Cannot send messages to this user")
        client = _make_client(guild=self._make_dm_exc_guild(exc))
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 403

    def test_discord_forbidden_body_has_permission_denied(self) -> None:
        """403 body detail must contain 'permission denied'.

        Per INTERFACE.md: ``{"detail": "Discord permission denied"}``.
        """
        response_mock = MagicMock()
        response_mock.status = 403
        response_mock.reason = "Forbidden"
        exc = discord.Forbidden(response_mock, "Cannot send messages to this user")
        client = _make_client(guild=self._make_dm_exc_guild(exc))
        data = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        ).json()
        assert "detail" in data
        assert "permission denied" in data["detail"].lower()

    def test_discord_4xx_translates_to_502(self) -> None:
        """discord.HTTPException status < 500 → HTTP 502.

        Mirrors test_notify_discord_4xx_returns_502.
        """
        response_mock = MagicMock()
        response_mock.status = 429
        response_mock.reason = "Too Many Requests"
        exc = discord.HTTPException(response_mock, "Rate limited")
        client = _make_client(guild=self._make_dm_exc_guild(exc))
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 502

    def test_discord_4xx_body_is_upstream_error(self) -> None:
        """502 body detail is 'Upstream Discord error'; raw status not exposed."""
        response_mock = MagicMock()
        response_mock.status = 429
        response_mock.reason = "Too Many Requests"
        exc = discord.HTTPException(response_mock, "Rate limited")
        client = _make_client(guild=self._make_dm_exc_guild(exc))
        data = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        ).json()
        assert data["detail"] == "Upstream Discord error"
        assert "429" not in data["detail"]

    def test_discord_5xx_translates_to_503(self) -> None:
        """discord.HTTPException status >= 500 → HTTP 503.

        Mirrors test_notify_discord_5xx_returns_503_unavailable.
        """
        response_mock = MagicMock()
        response_mock.status = 500
        response_mock.reason = "Internal Server Error"
        exc = discord.HTTPException(response_mock, "Server Error")
        client = _make_client(guild=self._make_dm_exc_guild(exc))
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 503

    def test_discord_5xx_body_has_unavailable(self) -> None:
        """503 body detail contains 'unavailable'."""
        response_mock = MagicMock()
        response_mock.status = 500
        response_mock.reason = "Internal Server Error"
        exc = discord.HTTPException(response_mock, "Server Error")
        client = _make_client(guild=self._make_dm_exc_guild(exc))
        data = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        ).json()
        assert "unavailable" in data["detail"].lower()

    def test_asyncio_timeout_translates_to_503(self) -> None:
        """asyncio.TimeoutError from DM send → HTTP 503.

        Mirrors test_notify_timeout_returns_503_unavailable.
        """
        client = _make_client(guild=self._make_dm_exc_guild(TimeoutError()))
        response = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 503

    def test_timeout_body_has_unavailable(self) -> None:
        """Timeout 503 body detail contains 'unavailable'."""
        client = _make_client(guild=self._make_dm_exc_guild(TimeoutError()))
        data = client.post(
            "/api/notify",
            json={"username": _KNOWN_USERNAME, "message": "Hi"},
            headers=_auth(),
        ).json()
        assert "unavailable" in data["detail"].lower()
