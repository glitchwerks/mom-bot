"""siege-web HTTP client for post-condition preference endpoints.

Provides :class:`SiegeWebClient`, an ``aiohttp``-based wrapper around the
two per-member preference endpoints on siege-web, plus the open catalog
reference endpoint.  Authentication uses a shared ``BOT_SERVICE_TOKEN``
passed as a Bearer header alongside a per-request ``X-Acting-Discord-Id``
header that tells siege-web which member to operate on.

Security contract
-----------------
- The bot token is stored in a private attribute and never appears in log
  output, exception messages, or response bodies sent to Discord.
- A 429 (rate-limit) response triggers up to 3 automatic retries with
  exponential backoff (1 s, 2 s, 4 s).  If a ``Retry-After`` header is
  present and its value is ≤ 30 s, that value is used instead for that
  attempt.  After 4 consecutive 429 responses,
  :class:`SiegeWebRateLimitError` is raised.
- The post-condition catalog is cached in-process for 10 minutes per
  ``stronghold_level`` key; a single ``asyncio.Lock`` serialises cold-cache
  fetches to prevent thundering-herd refetches.

Usage
-----
Construct once at bot startup (token is resolved via ``load_secret``) and
pass the same instance to every command handler.  The client reuses a
single ``aiohttp.ClientSession`` across all calls for efficiency; call
:meth:`close` (or use as an async context manager) on shutdown::

    async with SiegeWebClient(
        base_url=load_secret("siege-web-url"),
        token=load_secret("siege-web-bot-token"),
    ) as client:
        ...

Or manage lifetime manually::

    client = SiegeWebClient(
        base_url=load_secret("siege-web-url"),
        token=load_secret("siege-web-bot-token"),
    )
    # ... use client ...
    await client.close()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

__all__ = [
    "SiegeWebClient",
    "SiegeWebAuthError",
    "SiegeWebNotFoundError",
    "SiegeWebRateLimitError",
    "SiegeWebValidationError",
]

_logger = logging.getLogger(__name__)

# Exponential backoff schedule for 429 retries (seconds per attempt index).
# Three retries → four total attempts; worst-case wait is
# sum(_RETRY_BACKOFF_SCHEDULE) seconds (currently 7s).
_RETRY_BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 4.0)

# Maximum number of seconds we will honour from a ``Retry-After`` header.
# May exceed the exponential schedule ceiling; the header value is trusted
# when within this cap.  Values above the cap fall through to the exponential
# schedule for that attempt.
_RETRY_AFTER_MAX = 30.0

# TTL for the in-process catalog cache (seconds).
_CATALOG_CACHE_TTL = 600.0


def _parse_retry_after(headers: Any) -> float | None:
    """Parse the ``Retry-After`` header value as a sleep duration.

    Returns the header value as a positive float if it is present, parses
    as a number, and is within :data:`_RETRY_AFTER_MAX`.  Returns ``None``
    in all other cases (header absent, unparseable, or above the cap), so
    callers can fall through to the exponential schedule.

    Args:
        headers: A mapping (or dict-like object) of HTTP response headers.

    Returns:
        A positive float seconds value from the header, or ``None`` if the
        header is absent, unparseable, or exceeds ``_RETRY_AFTER_MAX``.
    """
    raw = headers.get("Retry-After") if headers else None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None
    if value <= 0 or value > _RETRY_AFTER_MAX:
        return None
    return value


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SiegeWebError(Exception):
    """Base class for siege-web HTTP errors raised by :class:`SiegeWebClient`."""


class SiegeWebAuthError(SiegeWebError):
    """Raised when siege-web returns HTTP 401 (bad/missing token or header).

    This indicates an operator misconfiguration — the bot token or the
    ``X-Acting-Discord-Id`` header is wrong.  The token must **never**
    appear in this exception's message.
    """


class SiegeWebNotFoundError(SiegeWebError):
    """Raised when siege-web returns HTTP 404.

    For the preferences endpoints this means the Discord ID supplied via
    ``X-Acting-Discord-Id`` does not correspond to a registered member.
    The user must log in at ``https://rslsiege.com`` to link their account.
    """


class SiegeWebRateLimitError(SiegeWebError):
    """Raised when a 429 persists after all automatic retries are exhausted."""


class SiegeWebValidationError(SiegeWebError):
    """Raised when siege-web returns HTTP 422 (schema validation failure).

    In practice this should not occur for well-formed mom-bot requests, but
    it is handled explicitly so callers receive a typed exception rather than
    a generic HTTP error.
    """


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SiegeWebClient:
    """Async HTTP client for siege-web's post-condition preference API.

    Wraps three endpoints:

    - ``GET /api/reference/post-conditions`` — open catalog (no auth).
    - ``GET /api/members/me/preferences``    — read a member's preferences.
    - ``PUT /api/members/me/preferences``    — replace a member's preferences.

    A single :class:`aiohttp.ClientSession` is created lazily on first use
    and reused across all subsequent calls.  Call :meth:`close` when the
    client is no longer needed (or use it as an async context manager).

    Attributes:
        base_url: The scheme+host root of the siege-web deployment
            (e.g. ``"https://rslsiege.com"``).  No trailing slash.
    """

    def __init__(self, base_url: str, token: str) -> None:
        """Initialise the client with the siege-web base URL and bot token.

        The underlying ``aiohttp.ClientSession`` is created lazily on first
        use via :meth:`_get_session`.

        Args:
            base_url: Siege-web root URL (e.g. ``"https://rslsiege.com"``).
                Must not end with a trailing slash.
            token: The ``BOT_SERVICE_TOKEN`` value.  Stored privately and
                never logged or surfaced in exceptions.
        """
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None
        # Catalog cache: maps stronghold_level (int | None) → (timestamp, payload).
        # Guarded by _catalog_cache_lock to prevent thundering-herd refetches.
        self._catalog_cache: dict[int | None, tuple[float, list[dict[str, Any]]]] = {}
        self._catalog_cache_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Async context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> SiegeWebClient:
        """Return self; session is created lazily on first API call.

        Returns:
            This :class:`SiegeWebClient` instance.
        """
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> None:
        """Close the underlying session on context-manager exit.

        Args:
            exc_type: Exception type, if any.
            exc_val: Exception value, if any.
            exc_tb: Exception traceback, if any.
        """
        await self.close()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared session, creating it lazily on first call.

        Returns:
            The :class:`aiohttp.ClientSession` for this client instance.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the underlying aiohttp session and release connections.

        Safe to call when no session has been created yet.  After closing,
        the session is set to ``None`` so subsequent API calls will
        transparently re-create it.
        """
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auth_headers(self, discord_id: str) -> dict[str, str]:
        """Build the auth headers required for the /me/ endpoints.

        Args:
            discord_id: The invoking user's Discord snowflake as a string.

        Returns:
            A dict with ``Authorization`` and ``X-Acting-Discord-Id``
            entries.
        """
        return {
            "Authorization": f"Bearer {self._token}",
            "X-Acting-Discord-Id": discord_id,
        }

    @staticmethod
    def _raise_for_status(status: int) -> None:
        """Raise an appropriate typed exception for non-200 status codes.

        Args:
            status: The HTTP response status code.

        Raises:
            SiegeWebAuthError: On 401.
            SiegeWebNotFoundError: On 404.
            SiegeWebValidationError: On 422.
        """
        if status == 401:
            raise SiegeWebAuthError(
                "siege-web returned 401 — check the bot service token "
                "and ensure X-Acting-Discord-Id is present."
            )
        if status == 404:
            raise SiegeWebNotFoundError("siege-web returned 404 — Discord ID not registered.")
        if status == 422:
            raise SiegeWebValidationError(
                "siege-web returned 422 — request body failed validation."
            )

    async def _call_with_retry(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Perform an HTTP request with exponential-backoff retry on 429.

        Issues up to ``len(_RETRY_BACKOFF_SCHEDULE) + 1`` total attempts.
        On each 429 response the helper reads the ``Retry-After`` header:
        if present, parses as a positive number, and is within
        :data:`_RETRY_AFTER_MAX`, that exact value is used as the sleep
        duration.  Otherwise the client sleeps
        ``_RETRY_BACKOFF_SCHEDULE[attempt]`` (exponential: 1, 2, 4 seconds).
        After the final 429 raises :class:`SiegeWebRateLimitError`.

        The ``headers`` and ``params`` kwargs are omitted from the underlying
        aiohttp call when ``None`` — aiohttp tolerates ``None`` but the
        omission keeps the call-kwargs shape predictable for tests and avoids
        forwarding ``{"headers": None}`` to the wire layer.

        Args:
            method: HTTP verb, one of ``"get"`` or ``"put"``.
            url: Full request URL including scheme and path.
            headers: HTTP headers to include in the request, or ``None``
                for unauthenticated calls (e.g. the catalog endpoint).
            json: Optional request body as a dict (serialised to JSON).
            params: Optional URL query parameters forwarded to aiohttp.

        Returns:
            The parsed JSON response body as a list of dicts.

        Raises:
            SiegeWebRateLimitError: After the maximum number of consecutive
                429 responses.
            SiegeWebAuthError: On 401.
            SiegeWebNotFoundError: On 404.
            SiegeWebValidationError: On 422.
        """
        session = await self._get_session()
        request = getattr(session, method)
        kwargs: dict[str, Any] = {}
        if headers is not None:
            kwargs["headers"] = headers
        if params is not None:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json

        max_attempts = len(_RETRY_BACKOFF_SCHEDULE) + 1  # 4 total
        for attempt in range(max_attempts):
            async with request(url, **kwargs) as resp:
                status = resp.status
                if status != 429:
                    self._raise_for_status(status)
                    data: list[dict[str, Any]] = await resp.json()
                    return data
                # 429 — prepare to sleep before next attempt (if any).
                retry_after = _parse_retry_after(resp.headers)

            if attempt >= max_attempts - 1:
                # All attempts exhausted.
                raise SiegeWebRateLimitError(
                    f"siege-web rate-limited {method.upper()} {url} "
                    f"after {max_attempts} attempts."
                )

            sleep_for = retry_after if retry_after is not None else _RETRY_BACKOFF_SCHEDULE[attempt]
            _logger.warning(
                "siege-web returned 429 on %s %s (attempt %d/%d); sleeping %.1fs before retry.",
                method.upper(),
                url,
                attempt + 1,
                max_attempts,
                sleep_for,
            )
            await asyncio.sleep(sleep_for)

        # Unreachable — loop always raises or returns inside.
        raise SiegeWebRateLimitError(  # pragma: no cover
            f"siege-web rate-limited {method.upper()} {url}."
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_catalog(
        self,
        stronghold_level: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch the post-condition catalog, returning a cached copy when fresh.

        Calls ``GET /api/reference/post-conditions`` without authentication.
        An optional ``stronghold_level`` query parameter filters the results.
        Results are cached per ``stronghold_level`` key for
        :data:`_CATALOG_CACHE_TTL` seconds (10 minutes).  A single
        ``asyncio.Lock`` serialises concurrent cache-miss fetches to avoid
        thundering-herd refetches.

        Cache staleness: at worst, newly added or edited catalog entries may
        not appear for up to 10 minutes after a siege-web admin change.
        Restarting the bot or waiting for TTL expiry are the available escape
        hatches.

        Args:
            stronghold_level: If provided, passed as ``?stronghold_level=N``
                to the catalog endpoint.  Cache is keyed by this value;
                ``None`` and each integer level are cached independently.

        Returns:
            A list of PostConditionResponse dicts.

        Raises:
            SiegeWebAuthError: On 401 (should not occur for this open
                endpoint).
            SiegeWebNotFoundError: On 404.
            SiegeWebValidationError: On 422.
        """
        url = f"{self.base_url}/api/reference/post-conditions"
        params: dict[str, Any] | None = (
            {"stronghold_level": stronghold_level} if stronghold_level is not None else None
        )

        # Fast path: check the cache without acquiring the lock.  This avoids
        # serialising concurrent callers for different stronghold_level keys
        # when their entries are already warm.
        cached = self._catalog_cache.get(stronghold_level)
        if cached is not None:
            ts, payload = cached
            if time.monotonic() - ts < _CATALOG_CACHE_TTL:
                _logger.debug(
                    "catalog cache HIT stronghold_level=%s",
                    stronghold_level,
                )
                return payload

        # Slow path: cache miss or stale entry — acquire the lock, then
        # re-check (another coroutine may have populated the entry while we
        # were waiting).
        async with self._catalog_cache_lock:
            cached = self._catalog_cache.get(stronghold_level)
            if cached is not None:
                ts, payload = cached
                if time.monotonic() - ts < _CATALOG_CACHE_TTL:
                    _logger.debug(
                        "catalog cache HIT (after lock) stronghold_level=%s",
                        stronghold_level,
                    )
                    return payload

            _logger.info(
                "siege-web catalog cache miss " "(stronghold_level=%s); fetching.",
                stronghold_level,
            )
            result = await self._call_with_retry("get", url, headers=None, params=params)
            self._catalog_cache[stronghold_level] = (time.monotonic(), result)
            return result

    async def get_my_preferences(
        self,
        discord_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch the invoking user's current post-condition preferences.

        Calls ``GET /api/members/me/preferences`` with Bearer + Discord-Id
        auth headers.  A single 429 retry is attempted before raising.

        Args:
            discord_id: The invoking user's Discord snowflake (numeric
                string).

        Returns:
            A list of PostConditionResponse dicts (may be empty if the user
            has no preferences set).

        Raises:
            SiegeWebAuthError: On 401 (wrong token or missing header).
            SiegeWebNotFoundError: On 404 (user not registered in
                siege-web).
            SiegeWebValidationError: On 422.
            SiegeWebRateLimitError: On repeated 429.
        """
        url = f"{self.base_url}/api/members/me/preferences"
        headers = self._auth_headers(discord_id)
        return await self._call_with_retry("get", url, headers)

    async def set_my_preferences(
        self,
        discord_id: str,
        ids: list[int],
    ) -> list[dict[str, Any]]:
        """Replace the invoking user's post-condition preferences.

        Calls ``PUT /api/members/me/preferences`` with a replacement-set
        body.  This is idempotent: submitting the same IDs twice is a no-op
        server-side.  Submitting an empty list clears all preferences.

        Args:
            discord_id: The invoking user's Discord snowflake (numeric
                string).
            ids: The complete desired set of post-condition IDs.  Each ID
                must exist in siege-web's database.

        Returns:
            The updated list of PostConditionResponse dicts as returned by
            siege-web after the PUT.

        Raises:
            SiegeWebAuthError: On 401.
            SiegeWebNotFoundError: On 404.
            SiegeWebValidationError: On 422 (unknown IDs in the body).
            SiegeWebRateLimitError: On repeated 429.
        """
        url = f"{self.base_url}/api/members/me/preferences"
        headers = self._auth_headers(discord_id)
        body = {"post_condition_ids": ids}
        return await self._call_with_retry("put", url, headers, json=body)
