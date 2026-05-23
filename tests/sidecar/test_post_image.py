"""Tests for POST /api/post-image.

Phase 6 of Epic #128 sidecar replacement (issue #180).

Covers:
- POST /api/post-image: 200 + {"status": "sent", "url": "<cdn-url>"} on success
- 404 when channel_name not found in guild channel list
- Bearer auth: missing → 403; wrong → 401 + WWW-Authenticate; correct → passes
- Discord exception translation: Forbidden → 403; 4xx → 502; 5xx/timeout → 503
- Multipart form validation: missing channel_name → 422; missing file → 422
- Channel-name as query param (not form field) → 422
- Streaming verification: UploadFile.file (SpooledTemporaryFile) is passed
  directly to discord.File — no full-buffer read via await upload.read()
- Channel-name resolution: exact match, first-match on duplicates, empty guild

Contract sources:
  - siege-web/bot/INTERFACE.md § POST /api/post-image
  - siege-web/backend/tests/integration/sidecar/test_post_image.py (tests win)
  - siege-web/backend/app/services/bot_client.py:post_image (caller side)

Design notes
------------
- Uses FastAPI TestClient (synchronous) via ``_make_client`` helper.
- FakeChannel.send() accepts file= kwarg and stores the discord.File arg so
  the streaming test can inspect what was passed.
- Streaming test patches discord.File to capture the fp= argument and asserts
  it is the UploadFile's underlying SpooledTemporaryFile, not a BytesIO copy.
- The ``build_app`` ``guild=`` parameter accepts any object duck-typed to the
  subset of ``discord.Guild`` the endpoints use.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock, patch

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

_VALID_KEY = "test-bearer-key-post-image-xyz"
_WRONG_KEY = "wrong-key"

_KNOWN_CHANNEL = "siege-images"
_UNKNOWN_CHANNEL = "no-such-channel"

# Minimal 1×1 white PNG — valid multipart bytes.
_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
    b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
    b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

_CDN_URL = "https://cdn.discordapp.com/attachments/123/456/test.png"


# ---------------------------------------------------------------------------
# Fake Discord objects
# ---------------------------------------------------------------------------


class FakeMessage:
    """Minimal stand-in for discord.Message with a single attachment.

    Attributes:
        attachments: List containing one fake attachment with a ``url``.
    """

    def __init__(self, url: str = _CDN_URL) -> None:
        """Initialise with a single attachment URL.

        Args:
            url: Discord CDN URL for the fake attachment.
        """

        class _FakeAttachment:
            pass

        att = _FakeAttachment()
        att.url = url  # type: ignore[attr-defined]
        self.attachments = [att]


class FakeChannel:
    """Minimal stand-in for discord.TextChannel.

    Attributes:
        name: Channel name (used for resolution by exact match).
        send_exc: If set, ``send()`` raises this exception.
        last_file_arg: The ``discord.File`` passed to the last ``send()``
            call; used by streaming tests.
    """

    def __init__(
        self,
        name: str,
        send_exc: Exception | None = None,
        cdn_url: str = _CDN_URL,
    ) -> None:
        """Initialise a fake text channel.

        Args:
            name: Discord channel name (exact; no ``#`` prefix).
            send_exc: Optional exception to raise when ``send()`` is
                called, simulating send failures.
            cdn_url: CDN URL to return in the fake message attachment.
        """
        self.name = name
        self._send_exc = send_exc
        self._cdn_url = cdn_url
        self.last_file_arg: discord.File | None = None

    async def send(
        self,
        content: str = "",
        *,
        file: discord.File | None = None,
    ) -> FakeMessage:
        """Send a file to this channel, or raise a configured exception.

        Stores the ``file`` argument so streaming tests can inspect it.

        Args:
            content: Optional caption text.
            file: The :class:`discord.File` to post.

        Returns:
            A :class:`FakeMessage` with one attachment.

        Raises:
            Exception: Whatever ``send_exc`` is set to, if set.
        """
        if self._send_exc is not None:
            raise self._send_exc
        self.last_file_arg = file
        return FakeMessage(self._cdn_url)


class FakeGuild:
    """Minimal stand-in for discord.Guild for post-image tests.

    Attributes:
        channels: List of :class:`FakeChannel` objects.
        members: Empty list (unused by post-image endpoint).
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
        """Not exercised by post-image; raises NotFound unconditionally.

        Args:
            user_id: Discord snowflake (unused by post-image endpoint).

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
        """Always reports ready — post-image tests do not exercise health.

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


def _multipart(
    channel_name: str = _KNOWN_CHANNEL,
    png: bytes = _MINIMAL_PNG,
    filename: str = "test.png",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (files, data) dicts for a standard multipart post-image request.

    Args:
        channel_name: Value for the ``channel_name`` form field.
        png: Image bytes to send.
        filename: Filename portion of the ``file`` part.

    Returns:
        Tuple of ``(files, data)`` suitable for ``client.post(...)``.
    """
    files = {"file": (filename, png, "image/png")}
    data = {"channel_name": channel_name}
    return files, data


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
# POST /api/post-image — auth
# ---------------------------------------------------------------------------


class TestPostImageAuth:
    """Bearer auth gates POST /api/post-image."""

    def test_missing_auth_returns_403(self) -> None:
        """No Authorization header → 403.

        Per siege-web/backend/tests/integration/sidecar/test_auth.py
        and issue glitchwerks/mom-bot#186.
        """
        client = _make_client()
        files, data = _multipart()
        response = client.post("/api/post-image", files=files, data=data)
        assert response.status_code == 403

    def test_missing_auth_body_has_detail_string(self) -> None:
        """403 for missing header must contain a 'detail' string key."""
        client = _make_client()
        files, data = _multipart()
        response = client.post("/api/post-image", files=files, data=data)
        body = response.json()
        assert "detail" in body
        assert isinstance(body["detail"], str)

    def test_wrong_token_returns_401(self) -> None:
        """Wrong Bearer token → 401."""
        client = _make_client()
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(_WRONG_KEY),
        )
        assert response.status_code == 401

    def test_wrong_token_has_www_authenticate_bearer(self) -> None:
        """Wrong-token 401 must include WWW-Authenticate: Bearer header."""
        client = _make_client()
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(_WRONG_KEY),
        )
        assert "Bearer" in response.headers.get("www-authenticate", "")


# ---------------------------------------------------------------------------
# POST /api/post-image — happy path
# ---------------------------------------------------------------------------


class TestPostImageSuccess:
    """POST /api/post-image returns 200 with status+url for a known channel."""

    def test_returns_200(self) -> None:
        """Valid auth + known channel_name → 200.

        Mirrors test_post_image_known_channel_returns_200_with_url.
        """
        client = _make_client()
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        assert response.status_code == 200

    def test_body_has_status_sent(self) -> None:
        """Response body status field is 'sent'.

        Mirrors INTERFACE.md: {"status": "sent", "url": "<non-empty string>"}.
        """
        client = _make_client()
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        body = response.json()
        assert body["status"] == "sent"

    def test_body_has_url_string(self) -> None:
        """Response body url field is a non-empty string.

        Per INTERFACE.md: ``url`` must be present and non-empty on 200.
        """
        client = _make_client()
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        body = response.json()
        assert "url" in body
        assert isinstance(body["url"], str)
        assert len(body["url"]) > 0

    def test_url_is_cdn_url(self) -> None:
        """Response url matches the CDN URL from the attachment."""
        client = _make_client()
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        body = response.json()
        assert body["url"] == _CDN_URL

    def test_first_matching_channel_used_when_duplicates_exist(self) -> None:
        """When multiple channels share a name, first match is used.

        Mirrors Phase 5's first-match semantics.
        """
        send_exc = _forbidden_exc()
        channels = [
            FakeChannel(_KNOWN_CHANNEL),  # first: succeeds
            FakeChannel(_KNOWN_CHANNEL, send_exc=send_exc),  # second: would fail
        ]
        guild = FakeGuild(channels=channels)
        client = _make_client(guild=guild)
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/post-image — streaming verification
# ---------------------------------------------------------------------------


class TestPostImageStreaming:
    """Endpoint passes UploadFile.file directly to discord.File (no full buffer).

    The streaming requirement: ``discord.File(fp=upload.file, filename=...)``
    not ``discord.File(fp=io.BytesIO(await upload.read()), ...)``.

    Strategy: patch ``discord.File`` in the module under test so we can
    capture the ``fp`` argument.  Assert the fp is NOT a BytesIO (which would
    indicate a full-buffer read path was used).
    """

    def test_discord_file_fp_is_not_bytesio(self) -> None:
        """discord.File fp= argument must not be a BytesIO instance.

        A BytesIO fp indicates the endpoint read the whole file into memory
        first.  The streaming path passes the raw UploadFile file object.
        """
        captured_fp: list[Any] = []

        original_discord_file = discord.File

        def _capture_discord_file(fp: Any, filename: str = "") -> discord.File:
            """Intercept discord.File construction to capture fp arg.

            Args:
                fp: The file-like object passed to discord.File.
                filename: The filename hint.

            Returns:
                A real discord.File constructed with the given args.
            """
            captured_fp.append(fp)
            return original_discord_file(fp, filename=filename)

        client = _make_client()
        files, data = _multipart()

        with patch(
            "mom_bot.sidecar.app.discord.File",
            side_effect=_capture_discord_file,
        ):
            response = client.post(
                "/api/post-image",
                files=files,
                data=data,
                headers=_auth(),
            )

        assert response.status_code == 200
        assert len(captured_fp) == 1, "discord.File should be constructed exactly once"
        assert not isinstance(captured_fp[0], io.BytesIO), (
            "fp passed to discord.File must not be a BytesIO — "
            "that indicates a full-buffer read; use upload.file directly"
        )

    def test_discord_file_filename_matches_upload(self) -> None:
        """discord.File filename= should reflect the uploaded filename."""
        captured_kwargs: list[dict[str, Any]] = []

        original_discord_file = discord.File

        def _capture_discord_file(fp: Any, filename: str = "") -> discord.File:
            """Intercept discord.File construction to capture kwargs.

            Args:
                fp: The file-like object passed to discord.File.
                filename: The filename hint.

            Returns:
                A real discord.File.
            """
            captured_kwargs.append({"fp": fp, "filename": filename})
            return original_discord_file(fp, filename=filename)

        client = _make_client()
        files, data = _multipart(filename="assignment.png")

        with patch(
            "mom_bot.sidecar.app.discord.File",
            side_effect=_capture_discord_file,
        ):
            response = client.post(
                "/api/post-image",
                files=files,
                data=data,
                headers=_auth(),
            )

        assert response.status_code == 200
        assert len(captured_kwargs) == 1
        assert captured_kwargs[0]["filename"] == "assignment.png"


# ---------------------------------------------------------------------------
# POST /api/post-image — 404 (channel not in guild)
# ---------------------------------------------------------------------------


class TestPostImageNotFound:
    """Unknown channel_name → 404 with detail string.

    Per INTERFACE.md: "all channel-resolution-class failures collapse to 404".
    The 404 fires on name resolution before any send attempt.
    """

    def test_unknown_channel_returns_404(self) -> None:
        """channel_name not in guild.channels → 404.

        Mirrors test_post_image_unknown_channel_returns_404_with_detail.
        """
        client = _make_client()
        files, data = _multipart(channel_name=_UNKNOWN_CHANNEL)
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        assert response.status_code == 404

    def test_404_body_has_detail_string(self) -> None:
        """404 response body must contain a 'detail' string key.

        Mirrors test_post_image_unknown_channel_returns_404_with_detail.
        """
        client = _make_client()
        files, data = _multipart(channel_name=_UNKNOWN_CHANNEL)
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        body = response.json()
        assert "detail" in body
        assert isinstance(body["detail"], str)

    def test_empty_guild_channels_returns_404(self) -> None:
        """No channels in guild always returns 404 for any channel_name."""
        client = _make_client(guild=FakeGuild(channels=[]))
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/post-image — multipart validation (422)
# ---------------------------------------------------------------------------


class TestPostImageValidation:
    """Missing required multipart fields → 422 per sidecar sub-app contract.

    Mirrors test_post_image_missing_channel_name_form_field_returns_422 and
    test_post_image_channel_name_as_query_param_returns_422 from siege-web.
    """

    def test_missing_channel_name_returns_422(self) -> None:
        """Missing 'channel_name' form field → 422.

        Mirrors test_post_image_missing_channel_name_form_field_returns_422.
        """
        client = _make_client()
        response = client.post(
            "/api/post-image",
            files={"file": ("test.png", _MINIMAL_PNG, "image/png")},
            # data= omitted — channel_name missing from form
            headers=_auth(),
        )
        assert response.status_code == 422

    def test_missing_channel_name_detail_is_list(self) -> None:
        """422 detail must be a list with loc/msg/type items."""
        client = _make_client()
        response = client.post(
            "/api/post-image",
            files={"file": ("test.png", _MINIMAL_PNG, "image/png")},
            headers=_auth(),
        )
        body = response.json()
        assert isinstance(body["detail"], list)
        assert len(body["detail"]) > 0
        item = body["detail"][0]
        assert "loc" in item
        assert "msg" in item
        assert "type" in item

    def test_channel_name_as_query_param_returns_422(self) -> None:
        """channel_name as query param (not form field) → 422.

        Mirrors test_post_image_channel_name_as_query_param_returns_422.
        The form field is absent, so FastAPI returns 422.
        """
        client = _make_client()
        response = client.post(
            f"/api/post-image?channel_name={_KNOWN_CHANNEL}",
            files={"file": ("test.png", _MINIMAL_PNG, "image/png")},
            headers=_auth(),
        )
        assert response.status_code == 422

    def test_missing_file_returns_422(self) -> None:
        """Missing 'file' multipart part → 422."""
        client = _make_client()
        response = client.post(
            "/api/post-image",
            data={"channel_name": _KNOWN_CHANNEL},
            # files= omitted — file part missing
            headers=_auth(),
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/post-image — Discord exception translation
# ---------------------------------------------------------------------------


class TestPostImageDiscordExceptions:
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

        Mirrors test_post_image_discord_forbidden_returns_403.
        """
        client = _make_client(guild=self._make_send_exc_guild(_forbidden_exc()))
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        assert response.status_code == 403

    def test_discord_forbidden_body_has_permission_denied(self) -> None:
        """403 body detail must contain 'permission denied'.

        Per INTERFACE.md: ``{"detail": "Discord permission denied"}``.
        """
        client = _make_client(guild=self._make_send_exc_guild(_forbidden_exc()))
        files, data = _multipart()
        body = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        ).json()
        assert "detail" in body
        assert "permission denied" in body["detail"].lower()

    def test_discord_4xx_translates_to_502(self) -> None:
        """discord.HTTPException status < 500 → HTTP 502.

        Mirrors test_post_image_discord_4xx_returns_502.
        """
        client = _make_client(guild=self._make_send_exc_guild(_http4xx_exc()))
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        assert response.status_code == 502

    def test_discord_4xx_body_is_upstream_error(self) -> None:
        """502 body detail is 'Upstream Discord error'; raw status not exposed.

        Mirrors test_post_image_discord_4xx_returns_502.
        """
        client = _make_client(guild=self._make_send_exc_guild(_http4xx_exc()))
        files, data = _multipart()
        body = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        ).json()
        assert body["detail"] == "Upstream Discord error"
        assert "429" not in body["detail"]

    def test_discord_5xx_translates_to_503(self) -> None:
        """discord.HTTPException status >= 500 → HTTP 503.

        Mirrors test_post_image_discord_5xx_returns_503.
        """
        client = _make_client(guild=self._make_send_exc_guild(_http5xx_exc()))
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        assert response.status_code == 503

    def test_discord_5xx_body_has_unavailable(self) -> None:
        """503 body detail contains 'unavailable'.

        Mirrors test_post_image_discord_5xx_returns_503.
        """
        client = _make_client(guild=self._make_send_exc_guild(_http5xx_exc()))
        files, data = _multipart()
        body = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        ).json()
        assert "unavailable" in body["detail"].lower()

    def test_asyncio_timeout_translates_to_503(self) -> None:
        """asyncio.TimeoutError from channel send → HTTP 503.

        Mirrors test_post_image_timeout_returns_503.
        """
        client = _make_client(guild=self._make_send_exc_guild(TimeoutError()))
        files, data = _multipart()
        response = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        )
        assert response.status_code == 503

    def test_timeout_body_has_unavailable(self) -> None:
        """Timeout 503 body detail contains 'unavailable'."""
        client = _make_client(guild=self._make_send_exc_guild(TimeoutError()))
        files, data = _multipart()
        body = client.post(
            "/api/post-image",
            files=files,
            data=data,
            headers=_auth(),
        ).json()
        assert "unavailable" in body["detail"].lower()
