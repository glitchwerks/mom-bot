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
- A 429 (rate-limit) response triggers a single retry with a short backoff;
  a second consecutive 429 raises :class:`SiegeWebRateLimitError`.

Usage
-----
Construct once at bot startup (token is resolved via ``load_secret``) and
pass the same instance to every command handler::

    client = SiegeWebClient(
        base_url=load_secret("siege-web-url"),
        token=load_secret("siege-web-bot-token"),
    )
"""

from __future__ import annotations

import asyncio
import logging
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

# Backoff before retrying a 429 response (seconds).
_RETRY_BACKOFF = 1.0


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
    """Raised when a 429 persists after the single automatic retry."""


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

    The client constructs a fresh ``aiohttp.ClientSession`` per call so it
    is safe to create at bot startup and reuse across the process lifetime
    without managing connection lifecycle externally.

    Attributes:
        base_url: The scheme+host root of the siege-web deployment
            (e.g. ``"https://rslsiege.com"``).  No trailing slash.
    """

    def __init__(self, base_url: str, token: str) -> None:
        """Initialise the client with the siege-web base URL and bot token.

        Args:
            base_url: Siege-web root URL (e.g. ``"https://rslsiege.com"``).
                Must not end with a trailing slash.
            token: The ``BOT_SERVICE_TOKEN`` value.  Stored privately and
                never logged or surfaced in exceptions.
        """
        self.base_url = base_url.rstrip("/")
        self._token = token

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auth_headers(self, discord_id: str) -> dict[str, str]:
        """Build the auth headers required for the /me/ endpoints.

        Args:
            discord_id: The invoking user's Discord snowflake as a string.

        Returns:
            A dict with ``Authorization`` and ``X-Acting-Discord-Id`` entries.
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_catalog(
        self,
        stronghold_level: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch the full post-condition catalog from the open reference endpoint.

        Calls ``GET /api/reference/post-conditions`` without authentication.
        An optional ``stronghold_level`` query parameter filters the results.

        Args:
            stronghold_level: If provided, passed as ``?stronghold_level=N``
                to the catalog endpoint.

        Returns:
            A list of PostConditionResponse dicts.

        Raises:
            SiegeWebAuthError: On 401 (should not occur for this open endpoint).
            SiegeWebNotFoundError: On 404.
            SiegeWebValidationError: On 422.
        """
        url = f"{self.base_url}/api/reference/post-conditions"
        params: dict[str, int] = {}
        if stronghold_level is not None:
            params["stronghold_level"] = stronghold_level

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                self._raise_for_status(resp.status)
                result: list[dict[str, Any]] = await resp.json()
                return result

    async def get_my_preferences(
        self,
        discord_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch the invoking user's current post-condition preferences.

        Calls ``GET /api/members/me/preferences`` with Bearer + Discord-Id
        auth headers.  A single 429 retry is attempted before raising.

        Args:
            discord_id: The invoking user's Discord snowflake (numeric string).

        Returns:
            A list of PostConditionResponse dicts (may be empty if the user
            has no preferences set).

        Raises:
            SiegeWebAuthError: On 401 (wrong token or missing header).
            SiegeWebNotFoundError: On 404 (user not registered in siege-web).
            SiegeWebValidationError: On 422.
            SiegeWebRateLimitError: On repeated 429.
        """
        url = f"{self.base_url}/api/members/me/preferences"
        headers = self._auth_headers(discord_id)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 429:
                    _logger.warning(
                        "siege-web returned 429 on GET /me/preferences; " "retrying after backoff."
                    )
                    await asyncio.sleep(_RETRY_BACKOFF)
                    async with session.get(url, headers=headers) as retry_resp:
                        if retry_resp.status == 429:
                            raise SiegeWebRateLimitError(
                                "siege-web rate-limited GET /me/preferences "
                                "on both initial attempt and retry."
                            )
                        self._raise_for_status(retry_resp.status)
                        result: list[dict[str, Any]] = await retry_resp.json()
                        return result

                self._raise_for_status(resp.status)
                data: list[dict[str, Any]] = await resp.json()
                return data

    async def set_my_preferences(
        self,
        discord_id: str,
        ids: list[int],
    ) -> list[dict[str, Any]]:
        """Replace the invoking user's post-condition preferences.

        Calls ``PUT /api/members/me/preferences`` with a replacement-set body.
        This is idempotent: submitting the same IDs twice is a no-op server-side.
        Submitting an empty list clears all preferences.

        Args:
            discord_id: The invoking user's Discord snowflake (numeric string).
            ids: The complete desired set of post-condition IDs.  Each ID must
                exist in siege-web's database.

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

        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, json=body) as resp:
                if resp.status == 429:
                    _logger.warning(
                        "siege-web returned 429 on PUT /me/preferences; " "retrying after backoff."
                    )
                    await asyncio.sleep(_RETRY_BACKOFF)
                    async with session.put(url, headers=headers, json=body) as retry_resp:
                        if retry_resp.status == 429:
                            raise SiegeWebRateLimitError(
                                "siege-web rate-limited PUT /me/preferences "
                                "on both initial attempt and retry."
                            )
                        self._raise_for_status(retry_resp.status)
                        result: list[dict[str, Any]] = await retry_resp.json()
                        return result

                self._raise_for_status(resp.status)
                data: list[dict[str, Any]] = await resp.json()
                return data
