"""Tests for mom_bot.post_conditions.client.

Uses unittest.mock to stub aiohttp.ClientSession.  No live siege-web calls.
Covers: happy path, all 4xx/5xx error modes, auth header verification,
and token-leak prevention.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mom_bot.post_conditions.client import (
    SiegeWebAuthError,
    SiegeWebClient,
    SiegeWebNotFoundError,
    SiegeWebRateLimitError,
    SiegeWebValidationError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://rslsiege.com"
_TOKEN = "super-secret-bot-token"
_DISCORD_ID = "123456789012345678"

_SAMPLE_CATALOG: list[dict[str, Any]] = [
    {
        "id": 5,
        "description": "Only HP Champions can be used.",
        "stronghold_level": 1,
        "condition_type": "role",
    },
    {
        "id": 12,
        "description": "Only Barbarian Champions can be used.",
        "stronghold_level": 1,
        "condition_type": "faction",
    },
]

_SAMPLE_PREFS: list[dict[str, Any]] = [
    {
        "id": 5,
        "description": "Only HP Champions can be used.",
        "stronghold_level": 1,
        "condition_type": "role",
    }
]


# ---------------------------------------------------------------------------
# Helpers — build a mock aiohttp response
# ---------------------------------------------------------------------------


def _make_response(
    status: int,
    json_data: Any = None,
) -> MagicMock:
    """Return a mock aiohttp response context-manager with status + json."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)

    # Support async context manager (async with session.get(...) as resp:)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_session(get_response: Any = None, put_response: Any = None) -> MagicMock:
    """Build a mock aiohttp.ClientSession with get/put configured."""
    session = MagicMock()
    session.get = MagicMock(return_value=get_response)
    session.put = MagicMock(return_value=put_response)

    # async context manager for session itself
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_client_stores_base_url_and_token() -> None:
    """SiegeWebClient stores base_url and token without exposing them."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    assert client.base_url == _BASE_URL
    # Token must not be stored under a public attribute named 'token'
    # (it may be stored privately).
    assert not hasattr(
        client, "token"
    ), "SiegeWebClient must not expose token as a public attribute"


# ---------------------------------------------------------------------------
# list_catalog — GET /api/reference/post-conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_catalog_happy_path() -> None:
    """list_catalog returns a list of condition dicts on 200."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_CATALOG)
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        result = await client.list_catalog()

    assert result == _SAMPLE_CATALOG


@pytest.mark.asyncio
async def test_list_catalog_does_not_send_auth_header() -> None:
    """list_catalog must NOT send Authorization to the open catalog endpoint."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_CATALOG)
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        await client.list_catalog()

    call_kwargs = session.get.call_args[1] if session.get.call_args else {}
    headers_sent = call_kwargs.get("headers", {})
    assert (
        "Authorization" not in headers_sent
    ), "Catalog endpoint must not receive Authorization header"


@pytest.mark.asyncio
async def test_list_catalog_with_stronghold_level_passes_query_param() -> None:
    """list_catalog(stronghold_level=2) passes ?stronghold_level=2 as param."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, [])
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        await client.list_catalog(stronghold_level=2)

    call_kwargs = session.get.call_args[1] if session.get.call_args else {}
    params = call_kwargs.get("params", {})
    assert params.get("stronghold_level") == 2


# ---------------------------------------------------------------------------
# get_my_preferences — GET /api/members/me/preferences
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_my_preferences_happy_path() -> None:
    """get_my_preferences returns the preference list on 200."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_PREFS)
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        result = await client.get_my_preferences(discord_id=_DISCORD_ID)

    assert result == _SAMPLE_PREFS


@pytest.mark.asyncio
async def test_get_my_preferences_sends_auth_headers() -> None:
    """get_my_preferences sends Bearer + X-Acting-Discord-Id headers."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_PREFS)
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        await client.get_my_preferences(discord_id=_DISCORD_ID)

    call_kwargs = session.get.call_args[1] if session.get.call_args else {}
    headers = call_kwargs.get("headers", {})
    assert headers.get("Authorization") == f"Bearer {_TOKEN}"
    assert headers.get("X-Acting-Discord-Id") == _DISCORD_ID


@pytest.mark.asyncio
async def test_get_my_preferences_401_raises_auth_error() -> None:
    """get_my_preferences raises SiegeWebAuthError on 401."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(401, None)
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(SiegeWebAuthError):
            await client.get_my_preferences(discord_id=_DISCORD_ID)


@pytest.mark.asyncio
async def test_get_my_preferences_404_raises_not_found_error() -> None:
    """get_my_preferences raises SiegeWebNotFoundError on 404."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(404, None)
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(SiegeWebNotFoundError):
            await client.get_my_preferences(discord_id=_DISCORD_ID)


@pytest.mark.asyncio
async def test_get_my_preferences_422_raises_validation_error() -> None:
    """get_my_preferences raises SiegeWebValidationError on 422."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(422, None)
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(SiegeWebValidationError):
            await client.get_my_preferences(discord_id=_DISCORD_ID)


@pytest.mark.asyncio
async def test_get_my_preferences_429_retries_once_and_succeeds() -> None:
    """get_my_preferences retries once after 429 and returns result on 200."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    # First call → 429, second call → 200
    first_ctx = _make_response(429, None)
    second_ctx = _make_response(200, _SAMPLE_PREFS)

    session = MagicMock()
    session.get = MagicMock(side_effect=[first_ctx, second_ctx])
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=session):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client.get_my_preferences(discord_id=_DISCORD_ID)

    assert result == _SAMPLE_PREFS
    assert session.get.call_count == 2


@pytest.mark.asyncio
async def test_get_my_preferences_429_twice_raises_rate_limit_error() -> None:
    """get_my_preferences raises SiegeWebRateLimitError if 429 on retry too."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    first_ctx = _make_response(429, None)
    second_ctx = _make_response(429, None)

    session = MagicMock()
    session.get = MagicMock(side_effect=[first_ctx, second_ctx])
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=session):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(SiegeWebRateLimitError):
                await client.get_my_preferences(discord_id=_DISCORD_ID)


# ---------------------------------------------------------------------------
# set_my_preferences — PUT /api/members/me/preferences
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_my_preferences_happy_path() -> None:
    """set_my_preferences returns the updated preference list on 200."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_PREFS)
    session = _make_session(put_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        result = await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])

    assert result == _SAMPLE_PREFS


@pytest.mark.asyncio
async def test_set_my_preferences_sends_correct_body() -> None:
    """set_my_preferences sends {post_condition_ids: [...]} JSON body."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_PREFS)
    session = _make_session(put_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5, 12])

    call_kwargs = session.put.call_args[1] if session.put.call_args else {}
    body = call_kwargs.get("json", {})
    assert body == {"post_condition_ids": [5, 12]}


@pytest.mark.asyncio
async def test_set_my_preferences_sends_auth_headers() -> None:
    """set_my_preferences sends Bearer + X-Acting-Discord-Id headers."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_PREFS)
    session = _make_session(put_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])

    call_kwargs = session.put.call_args[1] if session.put.call_args else {}
    headers = call_kwargs.get("headers", {})
    assert headers.get("Authorization") == f"Bearer {_TOKEN}"
    assert headers.get("X-Acting-Discord-Id") == _DISCORD_ID


@pytest.mark.asyncio
async def test_set_my_preferences_empty_ids_clears_preferences() -> None:
    """set_my_preferences([]) sends empty list — clearing all preferences."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, [])
    session = _make_session(put_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        result = await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[])

    assert result == []
    call_kwargs = session.put.call_args[1] if session.put.call_args else {}
    assert call_kwargs["json"] == {"post_condition_ids": []}


@pytest.mark.asyncio
async def test_set_my_preferences_401_raises_auth_error() -> None:
    """set_my_preferences raises SiegeWebAuthError on 401."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(401, None)
    session = _make_session(put_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(SiegeWebAuthError):
            await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])


@pytest.mark.asyncio
async def test_set_my_preferences_404_raises_not_found_error() -> None:
    """set_my_preferences raises SiegeWebNotFoundError on 404."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(404, None)
    session = _make_session(put_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(SiegeWebNotFoundError):
            await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])


@pytest.mark.asyncio
async def test_set_my_preferences_422_raises_validation_error() -> None:
    """set_my_preferences raises SiegeWebValidationError on 422."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(422, None)
    session = _make_session(put_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(SiegeWebValidationError):
            await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])


# ---------------------------------------------------------------------------
# Token leak prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_error_message_does_not_contain_token() -> None:
    """SiegeWebAuthError raised on 401 must not include the token in its message."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(401, None)
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(SiegeWebAuthError) as exc_info:
            await client.get_my_preferences(discord_id=_DISCORD_ID)

    assert _TOKEN not in str(exc_info.value), "Exception message must not contain the bot token"


@pytest.mark.asyncio
async def test_token_not_logged_on_auth_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No log record must contain the bot token when a 401 occurs."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(401, None)
    session = _make_session(get_response=resp_ctx)

    with caplog.at_level(logging.DEBUG):
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(SiegeWebAuthError):
                await client.get_my_preferences(discord_id=_DISCORD_ID)

    for record in caplog.records:
        assert (
            _TOKEN not in record.getMessage()
        ), f"Token found in log record: {record.getMessage()!r}"
