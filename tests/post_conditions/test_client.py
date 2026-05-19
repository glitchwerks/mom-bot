"""Tests for mom_bot.post_conditions.client.

Uses unittest.mock to stub aiohttp.ClientSession.  No live siege-web calls.
Covers: happy path, all 4xx/5xx error modes, auth header verification,
token-leak prevention, single-session reuse, session lifecycle, and the
async context manager.

Session-mock strategy
---------------------
The new :class:`SiegeWebClient` holds a single ``aiohttp.ClientSession``
instance at ``self._session``.  Tests inject a pre-built mock session via
:func:`_inject_session` rather than patching the ``aiohttp.ClientSession``
constructor.  This cleanly tests the reuse behaviour without relying on the
constructor call count.
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
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    status: int,
    json_data: Any = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Return a mock aiohttp response async context manager.

    Args:
        status: HTTP status code the mock response should report.
        json_data: Value to return from ``await resp.json()``.
        headers: Optional response headers dict (e.g. ``{"Retry-After":
            "2"}``).  Defaults to an empty dict.

    Returns:
        A :class:`~unittest.mock.MagicMock` that acts as an ``async with``
        context manager yielding a response mock.
    """
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.headers = headers or {}

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_session(
    get_response: Any = None,
    put_response: Any = None,
) -> MagicMock:
    """Build a mock aiohttp.ClientSession with get/put configured.

    Args:
        get_response: Return value for ``session.get(...)``.
        put_response: Return value for ``session.put(...)``.

    Returns:
        A :class:`~unittest.mock.MagicMock` mimicking a
        :class:`aiohttp.ClientSession`.
    """
    session = MagicMock()
    session.get = MagicMock(return_value=get_response)
    session.put = MagicMock(return_value=put_response)
    session.closed = False
    session.close = AsyncMock()
    return session


def _inject_session(
    client: SiegeWebClient,
    session: MagicMock,
) -> None:
    """Inject a pre-built mock session into *client* for testing.

    This bypasses the lazy ``_get_session`` constructor so tests control
    the exact session instance used.

    Args:
        client: The :class:`SiegeWebClient` under test.
        session: A mock session to inject.
    """
    client._session = session


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_client_stores_base_url_and_token() -> None:
    """SiegeWebClient stores base_url and token without exposing them."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    assert client.base_url == _BASE_URL
    # Token must not be stored under a public attribute named 'token'.
    assert not hasattr(
        client, "token"
    ), "SiegeWebClient must not expose token as a public attribute"


def test_client_starts_with_no_session() -> None:
    """SiegeWebClient._session is None at construction time."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    assert client._session is None


# ---------------------------------------------------------------------------
# Session reuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_session_reused_across_multiple_calls() -> None:
    """SiegeWebClient reuses the same session across sequential calls.

    Verifies that two sequential ``get_my_preferences`` calls result in
    exactly one :class:`aiohttp.ClientSession` being created.
    """
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    resp_ctx = _make_response(200, _SAMPLE_PREFS)
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session) as mock_cls:
        await client.get_my_preferences(discord_id=_DISCORD_ID)
        await client.get_my_preferences(discord_id=_DISCORD_ID)

    # Constructor must have been called exactly once despite two calls.
    mock_cls.assert_called_once()


# ---------------------------------------------------------------------------
# Session lifecycle — close() and context manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_closes_session_and_sets_none() -> None:
    """close() closes the underlying session and resets _session to None."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    session = _make_session()
    _inject_session(client, session)

    await client.close()

    session.close.assert_called_once()
    assert client._session is None


@pytest.mark.asyncio
async def test_close_when_no_session_is_noop() -> None:
    """close() is safe to call when no session has been created yet."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    # Must not raise.
    await client.close()
    assert client._session is None


@pytest.mark.asyncio
async def test_close_then_call_recreates_session() -> None:
    """After close(), the next API call transparently creates a new session."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_PREFS)

    with patch("aiohttp.ClientSession") as mock_cls:
        mock_cls.return_value = _make_session(get_response=resp_ctx)
        await client.get_my_preferences(discord_id=_DISCORD_ID)

    # Now close.
    old_session = client._session
    if old_session:
        old_session.close = AsyncMock()
    await client.close()
    assert client._session is None

    # Issue a second call — a fresh session must be created.
    resp_ctx2 = _make_response(200, _SAMPLE_PREFS)
    new_session = _make_session(get_response=resp_ctx2)
    with patch("aiohttp.ClientSession", return_value=new_session):
        result = await client.get_my_preferences(discord_id=_DISCORD_ID)

    assert result == _SAMPLE_PREFS
    assert client._session is new_session


@pytest.mark.asyncio
async def test_async_context_manager_closes_session_on_exit() -> None:
    """'async with SiegeWebClient(...)' closes the session on __aexit__."""
    resp_ctx = _make_response(200, _SAMPLE_PREFS)
    session = _make_session(get_response=resp_ctx)

    with patch("aiohttp.ClientSession", return_value=session):
        async with SiegeWebClient(base_url=_BASE_URL, token=_TOKEN) as client:
            await client.get_my_preferences(discord_id=_DISCORD_ID)

    # After exiting the context, close() should have been invoked.
    session.close.assert_called_once()
    assert client._session is None


# ---------------------------------------------------------------------------
# list_catalog — GET /api/reference/post-conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_catalog_happy_path() -> None:
    """list_catalog returns a list of condition dicts on 200."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_CATALOG)
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

    result = await client.list_catalog()

    assert result == _SAMPLE_CATALOG


@pytest.mark.asyncio
async def test_list_catalog_does_not_send_auth_header() -> None:
    """list_catalog must NOT send Authorization to the open catalog endpoint."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_CATALOG)
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

    await client.list_catalog()

    call_kwargs = session.get.call_args[1] if session.get.call_args else {}
    # After D10, _call_with_retry omits the 'headers' key entirely when the
    # caller passes headers=None.  Accept both the old shape (headers={}) and
    # the new shape (key absent) so this assertion survives the transition.
    assert "headers" not in call_kwargs or "Authorization" not in call_kwargs.get(
        "headers", {}
    ), "Catalog endpoint must not receive Authorization header"


@pytest.mark.asyncio
async def test_list_catalog_with_stronghold_level_passes_query_param() -> None:
    """list_catalog(stronghold_level=2) passes ?stronghold_level=2 as param."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, [])
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

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
    _inject_session(client, session)

    result = await client.get_my_preferences(discord_id=_DISCORD_ID)

    assert result == _SAMPLE_PREFS


@pytest.mark.asyncio
async def test_get_my_preferences_sends_auth_headers() -> None:
    """get_my_preferences sends Bearer + X-Acting-Discord-Id headers."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_PREFS)
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

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
    _inject_session(client, session)

    with pytest.raises(SiegeWebAuthError):
        await client.get_my_preferences(discord_id=_DISCORD_ID)


@pytest.mark.asyncio
async def test_get_my_preferences_404_raises_not_found_error() -> None:
    """get_my_preferences raises SiegeWebNotFoundError on 404."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(404, None)
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

    with pytest.raises(SiegeWebNotFoundError):
        await client.get_my_preferences(discord_id=_DISCORD_ID)


@pytest.mark.asyncio
async def test_get_my_preferences_422_raises_validation_error() -> None:
    """get_my_preferences raises SiegeWebValidationError on 422."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(422, None)
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

    with pytest.raises(SiegeWebValidationError):
        await client.get_my_preferences(discord_id=_DISCORD_ID)


@pytest.mark.asyncio
async def test_get_my_preferences_429_retries_once_and_succeeds() -> None:
    """get_my_preferences retries once after 429 and returns result on 200."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    first_ctx = _make_response(429, None)
    second_ctx = _make_response(200, _SAMPLE_PREFS)

    session = _make_session()
    session.get = MagicMock(side_effect=[first_ctx, second_ctx])
    _inject_session(client, session)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await client.get_my_preferences(discord_id=_DISCORD_ID)

    assert result == _SAMPLE_PREFS
    assert session.get.call_count == 2


@pytest.mark.asyncio
async def test_get_my_preferences_429_persistent_raises_rate_limit_error() -> None:
    """get_my_preferences raises SiegeWebRateLimitError after 4 consecutive 429s."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    session = _make_session()
    session.get = MagicMock(side_effect=[_make_response(429, None) for _ in range(4)])
    _inject_session(client, session)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(SiegeWebRateLimitError):
            await client.get_my_preferences(discord_id=_DISCORD_ID)

    assert mock_sleep.await_count == 3


# ---------------------------------------------------------------------------
# set_my_preferences — PUT /api/members/me/preferences
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_my_preferences_happy_path() -> None:
    """set_my_preferences returns the updated preference list on 200."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_PREFS)
    session = _make_session(put_response=resp_ctx)
    _inject_session(client, session)

    result = await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])

    assert result == _SAMPLE_PREFS


@pytest.mark.asyncio
async def test_set_my_preferences_sends_correct_body() -> None:
    """set_my_preferences sends {post_condition_ids: [...]} JSON body."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_PREFS)
    session = _make_session(put_response=resp_ctx)
    _inject_session(client, session)

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
    _inject_session(client, session)

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
    _inject_session(client, session)

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
    _inject_session(client, session)

    with pytest.raises(SiegeWebAuthError):
        await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])


@pytest.mark.asyncio
async def test_set_my_preferences_404_raises_not_found_error() -> None:
    """set_my_preferences raises SiegeWebNotFoundError on 404."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(404, None)
    session = _make_session(put_response=resp_ctx)
    _inject_session(client, session)

    with pytest.raises(SiegeWebNotFoundError):
        await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])


@pytest.mark.asyncio
async def test_set_my_preferences_422_raises_validation_error() -> None:
    """set_my_preferences raises SiegeWebValidationError on 422."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(422, None)
    session = _make_session(put_response=resp_ctx)
    _inject_session(client, session)

    with pytest.raises(SiegeWebValidationError):
        await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])


@pytest.mark.asyncio
async def test_set_my_preferences_429_retries_once_and_succeeds() -> None:
    """set_my_preferences retries once after 429 and returns result on 200."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    first_ctx = _make_response(429, None)
    second_ctx = _make_response(200, _SAMPLE_PREFS)

    session = _make_session()
    session.put = MagicMock(side_effect=[first_ctx, second_ctx])
    _inject_session(client, session)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])

    assert result == _SAMPLE_PREFS
    assert session.put.call_count == 2


@pytest.mark.asyncio
async def test_set_my_preferences_429_persistent_raises_rate_limit_error() -> None:
    """set_my_preferences raises SiegeWebRateLimitError after 4 consecutive 429s."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    session = _make_session()
    session.put = MagicMock(side_effect=[_make_response(429, None) for _ in range(4)])
    _inject_session(client, session)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(SiegeWebRateLimitError):
            await client.set_my_preferences(discord_id=_DISCORD_ID, ids=[5])

    assert mock_sleep.await_count == 3


# ---------------------------------------------------------------------------
# list_catalog — catalog TTL cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_catalog_caches_response() -> None:
    """Two rapid list_catalog() calls hit HTTP exactly once; both return equal data."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_CATALOG)
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

    first = await client.list_catalog()
    second = await client.list_catalog()

    assert first == _SAMPLE_CATALOG
    assert second == _SAMPLE_CATALOG
    assert session.get.call_count == 1, "HTTP layer must be called only once when the cache is warm"


@pytest.mark.asyncio
async def test_list_catalog_cache_expires_after_ttl() -> None:
    """list_catalog() refetches from HTTP after the TTL has elapsed."""
    import time_machine  # noqa: PLC0415 — only available in dev deps

    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_CATALOG)
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

    await client.list_catalog()
    assert session.get.call_count == 1

    # Advance monotonic time past the TTL.
    with time_machine.travel(0, tick=False):
        # Re-inject session since time_machine may reset things; re-use same.
        pass

    # Manually expire the cache by backdating its timestamp.
    from mom_bot.post_conditions.client import _CATALOG_CACHE_TTL  # noqa: PLC0415

    old_ts, old_payload = client._catalog_cache[None]
    client._catalog_cache[None] = (
        old_ts - _CATALOG_CACHE_TTL - 1.0,
        old_payload,
    )

    # Second call must go to HTTP again.
    resp_ctx2 = _make_response(200, _SAMPLE_CATALOG)
    session.get = MagicMock(return_value=resp_ctx2)

    second = await client.list_catalog()

    assert second == _SAMPLE_CATALOG
    assert session.get.call_count == 1  # reset to 1 after re-assignment


@pytest.mark.asyncio
async def test_list_catalog_caches_per_stronghold_level() -> None:
    """Cache keys are distinct for None, 5, and then None again (cache hit)."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    resp_none = _make_response(200, _SAMPLE_CATALOG)
    resp_five = _make_response(200, [])

    session = _make_session()
    session.get = MagicMock(side_effect=[resp_none, resp_five])
    _inject_session(client, session)

    await client.list_catalog()  # miss → key None
    await client.list_catalog(stronghold_level=5)  # miss → key 5
    await client.list_catalog()  # hit → key None (no HTTP)

    assert (
        session.get.call_count == 2
    ), "HTTP must be called once per distinct cache key, not on cache hits"


@pytest.mark.asyncio
async def test_list_catalog_concurrent_requests_share_fetch() -> None:
    """Two concurrent list_catalog() calls on a cold cache produce one HTTP request."""
    import asyncio as _asyncio  # noqa: PLC0415

    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_CATALOG)
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

    results = await _asyncio.gather(
        client.list_catalog(),
        client.list_catalog(),
    )

    assert results[0] == _SAMPLE_CATALOG
    assert results[1] == _SAMPLE_CATALOG
    assert (
        session.get.call_count == 1
    ), "Lock must prevent concurrent cold-cache misses from double-fetching"


@pytest.mark.asyncio
async def test_list_catalog_concurrent_different_keys_dont_serialize() -> None:
    """Cache hit on key=None must not block a concurrent miss on key=5.

    Strategy: start ``list_catalog(5)`` first so it acquires the lock and
    enters the (stalled) HTTP fetch.  While the lock is held, start
    ``list_catalog(None)`` which has a pre-warmed cache entry.  With
    double-checked locking (DCL), ``list_catalog(None)`` returns immediately
    via the lock-free fast path.  Without DCL (old code), it blocks behind
    the lock until key=5's HTTP call finishes.

    We observe this by recording the order of completion: ``none_done``
    must be set *before* ``five_done`` even though ``list_catalog(5)``
    started first.
    """
    import asyncio as _asyncio  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    # Pre-warm the cache for key=None.
    client._catalog_cache[None] = (_time.monotonic(), _SAMPLE_CATALOG)

    key5_result: list[dict[str, Any]] = [{"id": 99}]

    # Event pair to stall the key=5 HTTP call until we choose to release it.
    fetch_started = _asyncio.Event()
    fetch_release = _asyncio.Event()

    # Patch _call_with_retry so key=5's "HTTP call" blocks on fetch_release.
    async def _stalling_call_with_retry(
        method: str,
        url: str,
        *,
        headers: Any,
        params: Any = None,
        json: Any = None,
    ) -> list[dict[str, Any]]:
        fetch_started.set()
        await fetch_release.wait()
        return key5_result

    client._call_with_retry = _stalling_call_with_retry  # type: ignore[method-assign]

    completion_order: list[str] = []

    async def _call_five() -> list[dict[str, Any]]:
        result = await client.list_catalog(stronghold_level=5)
        completion_order.append("five")
        return result

    async def _call_none() -> list[dict[str, Any]]:
        # Yield once so _call_five starts first and acquires the lock.
        await _asyncio.sleep(0)
        result = await client.list_catalog(stronghold_level=None)
        completion_order.append("none")
        return result

    task_five = _asyncio.create_task(_call_five())
    task_none = _asyncio.create_task(_call_none())

    # Wait until key=5 has entered its stalled fetch (lock is now held by it).
    await _asyncio.wait_for(fetch_started.wait(), timeout=2.0)

    # Yield to let _call_none run as far as it can while the lock is held.
    await _asyncio.sleep(0)
    await _asyncio.sleep(0)

    # With DCL, _call_none should have already completed (fast path bypasses
    # the lock entirely).  Without DCL it would still be waiting on the lock.
    assert "none" in completion_order, (
        "list_catalog(None) should have returned via DCL fast path while "
        "key=5's lock-held fetch was still in progress"
    )
    assert (
        "five" not in completion_order
    ), "list_catalog(5) should still be blocked on the stalled HTTP call"

    # Release the stall and let both tasks finish.
    fetch_release.set()
    result_none, result_five = await _asyncio.gather(task_none, task_five)

    assert result_none == _SAMPLE_CATALOG
    assert result_five == key5_result


# ---------------------------------------------------------------------------
# _call_with_retry — Retry-After / exponential backoff / helper contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_with_retry_honors_retry_after_header() -> None:
    """On 429 with Retry-After: 2, the helper sleeps exactly 2.0 seconds."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    retry_after_ctx = _make_response(429, None, headers={"Retry-After": "2"})
    ok_ctx = _make_response(200, _SAMPLE_PREFS)

    session = _make_session()
    session.get = MagicMock(side_effect=[retry_after_ctx, ok_ctx])
    _inject_session(client, session)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await client.get_my_preferences(discord_id=_DISCORD_ID)

    mock_sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_call_with_retry_caps_retry_after() -> None:
    """On 429 with Retry-After: 600 (above cap), falls through to exponential.

    The first retry sleep must use the exponential schedule value (1.0),
    not the header value (600) or the cap (30).
    """
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    retry_after_ctx = _make_response(429, None, headers={"Retry-After": "600"})
    ok_ctx = _make_response(200, _SAMPLE_PREFS)

    session = _make_session()
    session.get = MagicMock(side_effect=[retry_after_ctx, ok_ctx])
    _inject_session(client, session)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await client.get_my_preferences(discord_id=_DISCORD_ID)

    mock_sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_call_with_retry_uses_exponential_when_header_absent() -> None:
    """On persistent 429 with no Retry-After header, sleeps 1.0, 2.0, 4.0."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    session = _make_session()
    session.get = MagicMock(side_effect=[_make_response(429, None) for _ in range(4)])
    _inject_session(client, session)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(SiegeWebRateLimitError):
            await client.get_my_preferences(discord_id=_DISCORD_ID)

    sleep_args = [call.args[0] for call in mock_sleep.await_args_list]
    assert sleep_args == [1.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_call_with_retry_succeeds_on_third_attempt() -> None:
    """Client succeeds on the 3rd attempt after two 429s (extended envelope)."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)

    session = _make_session()
    session.get = MagicMock(
        side_effect=[
            _make_response(429, None),
            _make_response(429, None),
            _make_response(200, _SAMPLE_PREFS),
        ]
    )
    _inject_session(client, session)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await client.get_my_preferences(discord_id=_DISCORD_ID)

    assert result == _SAMPLE_PREFS
    assert session.get.call_count == 3
    assert mock_sleep.await_count == 2


@pytest.mark.asyncio
async def test_call_with_retry_omits_headers_when_none() -> None:
    """When called with headers=None, aiohttp kwargs must not contain 'headers'."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(200, _SAMPLE_CATALOG)
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

    # list_catalog calls _call_with_retry without auth headers
    await client.list_catalog()

    call_kwargs = session.get.call_args[1] if session.get.call_args else {}
    assert "headers" not in call_kwargs, (
        "_call_with_retry must omit 'headers' key from aiohttp kwargs when "
        "headers=None is passed"
    )


# ---------------------------------------------------------------------------
# Token leak prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_error_message_does_not_contain_token() -> None:
    """SiegeWebAuthError raised on 401 must not include the token."""
    client = SiegeWebClient(base_url=_BASE_URL, token=_TOKEN)
    resp_ctx = _make_response(401, None)
    session = _make_session(get_response=resp_ctx)
    _inject_session(client, session)

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
    _inject_session(client, session)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SiegeWebAuthError):
            await client.get_my_preferences(discord_id=_DISCORD_ID)

    for record in caplog.records:
        assert (
            _TOKEN not in record.getMessage()
        ), f"Token found in log record: {record.getMessage()!r}"
