"""Tests for POST /api/post-message.

Phase 5 of Epic #128 sidecar replacement (issue #179).

Covers:
- POST /api/post-message: 200 + {"status": "sent"} on success
- 404 when channel_name not found in guild channel list
- Bearer auth: missing → 403; wrong → 401 + WWW-Authenticate; correct → passes
- Discord exception translation: Forbidden → 403; 4xx → 502; 5xx/timeout → 503
- Body validation: missing required field → 422
- Channel-name resolution: exact match, first-match on duplicates, empty guild

Contract sources:
  - siege-web/bot/INTERFACE.md § POST /api/post-message
  - siege-web/backend/tests/integration/sidecar/test_post_message.py (tests win
    on disagreement with prose)

Design notes
------------
- Uses FastAPI TestClient (synchronous) via ``_make_client`` helper.
- Channel lookup is against ``guild.channels`` (TextChannel only, exact name).
- Discord exception translation is exercised by configuring ``FakeChannel`` to
  raise specific exceptions in ``send()``.
- Channel resolution failures (name not found) raise 404 before any Discord
  API call is attempted — per INTERFACE.md's "collapse to 404" rule.
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

_VALID_KEY = "test-bearer-key-post-message-xyz"
_WRONG_KEY = "wrong-key"

_KNOWN_CHANNEL = "siege-assignments"
_UNKNOWN_CHANNEL = "no-such-channel"


# ---------------------------------------------------------------------------
# Fake Discord objects
# ---------------------------------------------------------------------------


class FakeChannel:
    """Minimal stand-in for discord.TextChannel.

    Attributes:
        name: Channel name (used for resolution by exact match).
        send_exc: If set, ``send()`` raises this exception to simulate
            send failures (Forbidden, HTTPException, TimeoutError, etc.).
    """

    def __init__(
        self,
        name: str,
        send_exc: Exception | None = None,
    ) -> None:
        """Initialise a fake text channel.

        Args:
            name: Discord channel name (exact; no ``#`` prefix).
            send_exc: Optional exception to raise when ``send()`` is called,
                simulating send failures.
        """
        self.name = name
        self._send_exc = send_exc

    async def send(self, content: str) -> None:
        """Send a message to this channel, or raise a configured exception.

        Mirrors ``discord.TextChannel.send()`` — the endpoint calls this
        after channel resolution.

        Args:
            content: Message text to send.

        Raises:
            Exception: Whatever ``send_exc`` is set to, if set.
        """
        if self._send_exc is not None:
            raise self._send_exc


class FakeGuild:
    """Minimal stand-in for discord.Guild for post-message tests.

    Supplies ``.channels`` (iterable of FakeChannel for name resolution)
    and ``.members`` (empty list — not used by post-message but required
    by app construction).

    Attributes:
        channels: List of :class:`FakeChannel` objects.
        members: Empty list (unused by post-message endpoint).
    """

    def __init__(
        self,
        channels: list[FakeChannel] | None = None,
    ) -> None:
        """Initialise the fake guild.

        Args:
            channels: Guild channel list.  Defaults to one known channel
                with no send failure.
        """
        if channels is not None:
            self.channels: list[FakeChannel] = channels
        else:
            self.channels = [FakeChannel(_KNOWN_CHANNEL)]
        self.members: list[Any] = []

    async def fetch_member(self, user_id: int) -> None:
        """Not exercised by post-message; raises NotFound unconditionally.

        Args:
            user_id: Discord snowflake (unused by post-message endpoint).

        Raises:
            discord.NotFound: Always.
        """
        response = MagicMock()
        response.status = 404
        response.reason = "Unknown Member"
        raise discord.NotFound(response, "Unknown Member")


class _FakeBot:
    """Minimal stand-in for discord.Client used by build_app."""

    def is_ready(self) -> bool:
        """Always reports ready — post-message tests do not exercise health.

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
            known channel.

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


def _forbidden_exc() -> discord.Forbidden:
    """Build a discord.Forbidden exception for send-failure tests.

    Returns:
        A :class:`discord.Forbidden` instance.
    """
    response_mock = MagicMock()
    response_mock.status = 403
    response_mock.reason = "Forbidden"
    return discord.Forbidden(response_mock, "Missing Permissions")


def _http4xx_exc() -> discord.HTTPException:
    """Build a discord.HTTPException with status 429 for send-failure tests.

    Returns:
        A :class:`discord.HTTPException` with status 429.
    """
    response_mock = MagicMock()
    response_mock.status = 429
    response_mock.reason = "Too Many Requests"
    return discord.HTTPException(response_mock, "Rate limited")


def _http5xx_exc() -> discord.HTTPException:
    """Build a discord.HTTPException with status 500 for send-failure tests.

    Returns:
        A :class:`discord.HTTPException` with status 500.
    """
    response_mock = MagicMock()
    response_mock.status = 500
    response_mock.reason = "Internal Server Error"
    return discord.HTTPException(response_mock, "Server Error")


# ---------------------------------------------------------------------------
# POST /api/post-message — auth
# ---------------------------------------------------------------------------


class TestPostMessageAuth:
    """Bearer auth gates POST /api/post-message."""

    def test_missing_auth_returns_403(self) -> None:
        """No Authorization header → 403.

        Per siege-web/backend/tests/integration/sidecar/test_auth.py
        and issue glitchwerks/mom-bot#186.
        """
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
        )
        assert response.status_code == 403

    def test_missing_auth_body_has_detail_string(self) -> None:
        """403 for missing header must contain a 'detail' string key."""
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
        )
        data = response.json()
        assert "detail" in data
        assert isinstance(data["detail"], str)

    def test_wrong_token_returns_401(self) -> None:
        """Wrong Bearer token → 401."""
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(_WRONG_KEY),
        )
        assert response.status_code == 401

    def test_wrong_token_has_www_authenticate_bearer(self) -> None:
        """Wrong-token 401 must include WWW-Authenticate: Bearer header."""
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(_WRONG_KEY),
        )
        assert "Bearer" in response.headers.get("www-authenticate", "")


# ---------------------------------------------------------------------------
# POST /api/post-message — happy path
# ---------------------------------------------------------------------------


class TestPostMessageSuccess:
    """POST /api/post-message returns 200 with status:sent for a known channel."""

    def test_returns_200(self) -> None:
        """Valid auth + known channel_name → 200.

        Mirrors test_post_message_known_channel_returns_200_sent.
        """
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Siege ready!"},
            headers=_auth(),
        )
        assert response.status_code == 200

    def test_body_is_status_sent(self) -> None:
        """Response body is exactly {"status": "sent"}.

        Mirrors test_post_message_known_channel_returns_200_sent.
        """
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Siege ready!"},
            headers=_auth(),
        )
        assert response.json() == {"status": "sent"}

    def test_first_matching_channel_used_when_duplicates_exist(self) -> None:
        """When multiple channels share a name, first match is used.

        Mirrors bundled sidecar's discord.utils.find semantics: returns
        the first match in guild.channels iteration order.
        """
        send_exc = _forbidden_exc()
        channels = [
            FakeChannel(_KNOWN_CHANNEL),  # first: succeeds
            FakeChannel(_KNOWN_CHANNEL, send_exc=send_exc),  # second: would fail
        ]
        guild = FakeGuild(channels=channels)
        client = _make_client(guild=guild)
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        )
        # First channel succeeds → 200
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/post-message — 404 (channel not in guild)
# ---------------------------------------------------------------------------


class TestPostMessageNotFound:
    """Unknown channel_name → 404 with detail string.

    Per INTERFACE.md: "all channel-resolution-class failures collapse to 404".
    The 404 fires on name resolution before any send attempt.
    """

    def test_unknown_channel_returns_404(self) -> None:
        """channel_name not in guild.channels → 404.

        Mirrors test_post_message_unknown_channel_returns_404_with_detail.
        """
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"channel_name": _UNKNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 404

    def test_404_body_has_detail_string(self) -> None:
        """404 response body must contain a 'detail' string key.

        Mirrors test_post_message_unknown_channel_returns_404_with_detail.
        """
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"channel_name": _UNKNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        )
        data = response.json()
        assert "detail" in data
        assert isinstance(data["detail"], str)

    def test_empty_guild_channels_returns_404(self) -> None:
        """No channels in guild always returns 404 for any channel_name."""
        client = _make_client(guild=FakeGuild(channels=[]))
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/post-message — body validation (422)
# ---------------------------------------------------------------------------


class TestPostMessageValidation:
    """Missing required body fields → 422 per sidecar sub-app contract."""

    def test_missing_channel_name_returns_422(self) -> None:
        """Missing 'channel_name' field → 422.

        Mirrors test_post_message_missing_channel_name_returns_422.
        """
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 422

    def test_missing_channel_name_detail_is_list(self) -> None:
        """422 detail must be a list with loc/msg/type items."""
        client = _make_client()
        response = client.post(
            "/api/post-message",
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

        Mirrors test_post_message_missing_message_returns_422.
        """
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL},
            headers=_auth(),
        )
        assert response.status_code == 422

    def test_missing_message_detail_is_list(self) -> None:
        """422 for missing message also returns detail as list."""
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL},
            headers=_auth(),
        )
        data = response.json()
        assert isinstance(data["detail"], list)

    def test_empty_body_returns_422(self) -> None:
        """Empty body → 422."""
        client = _make_client()
        response = client.post(
            "/api/post-message",
            json={},
            headers=_auth(),
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/post-message — Discord exception translation
# ---------------------------------------------------------------------------


class TestPostMessageDiscordExceptions:
    """Discord exceptions from channel send translate to correct HTTP codes.

    The channel is resolved first (404 if not found), then the send is
    attempted.  Failures at the send step are translated by the exception
    handlers on _sidecar_sub.
    """

    def _make_send_exc_guild(self, exc: Exception) -> FakeGuild:
        """Build a FakeGuild whose channel send raises ``exc``.

        Args:
            exc: Exception to raise when ``channel.send()`` is called.

        Returns:
            A FakeGuild with the known channel configured to raise ``exc``
            on send.
        """
        return FakeGuild(
            channels=[FakeChannel(_KNOWN_CHANNEL, send_exc=exc)],
        )

    def test_discord_forbidden_translates_to_403(self) -> None:
        """discord.Forbidden from channel send → HTTP 403.

        Mirrors test_post_message_discord_forbidden_returns_403.
        """
        client = _make_client(guild=self._make_send_exc_guild(_forbidden_exc()))
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 403

    def test_discord_forbidden_body_has_permission_denied(self) -> None:
        """403 body detail must contain 'permission denied'.

        Per INTERFACE.md: ``{"detail": "Discord permission denied"}``.
        Mirrors test_post_message_discord_forbidden_returns_403.
        """
        client = _make_client(guild=self._make_send_exc_guild(_forbidden_exc()))
        data = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        ).json()
        assert "detail" in data
        assert "permission denied" in data["detail"].lower()

    def test_discord_4xx_translates_to_502(self) -> None:
        """discord.HTTPException status < 500 → HTTP 502.

        Mirrors test_post_message_discord_4xx_returns_502.
        """
        client = _make_client(guild=self._make_send_exc_guild(_http4xx_exc()))
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 502

    def test_discord_4xx_body_is_upstream_error(self) -> None:
        """502 body detail is 'Upstream Discord error'; raw status not exposed.

        Mirrors test_post_message_discord_4xx_returns_502.
        """
        client = _make_client(guild=self._make_send_exc_guild(_http4xx_exc()))
        data = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        ).json()
        assert data["detail"] == "Upstream Discord error"
        assert "429" not in data["detail"]

    def test_discord_5xx_translates_to_503(self) -> None:
        """discord.HTTPException status >= 500 → HTTP 503.

        Mirrors test_post_message_discord_5xx_returns_503.
        """
        client = _make_client(guild=self._make_send_exc_guild(_http5xx_exc()))
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 503

    def test_discord_5xx_body_has_unavailable(self) -> None:
        """503 body detail contains 'unavailable'.

        Mirrors test_post_message_discord_5xx_returns_503.
        """
        client = _make_client(guild=self._make_send_exc_guild(_http5xx_exc()))
        data = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        ).json()
        assert "unavailable" in data["detail"].lower()

    def test_asyncio_timeout_translates_to_503(self) -> None:
        """asyncio.TimeoutError from channel send → HTTP 503.

        Mirrors test_post_message_timeout_returns_503.
        """
        client = _make_client(guild=self._make_send_exc_guild(TimeoutError()))
        response = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        )
        assert response.status_code == 503

    def test_timeout_body_has_unavailable(self) -> None:
        """Timeout 503 body detail contains 'unavailable'."""
        client = _make_client(guild=self._make_send_exc_guild(TimeoutError()))
        data = client.post(
            "/api/post-message",
            json={"channel_name": _KNOWN_CHANNEL, "message": "Hi"},
            headers=_auth(),
        ).json()
        assert "unavailable" in data["detail"].lower()
