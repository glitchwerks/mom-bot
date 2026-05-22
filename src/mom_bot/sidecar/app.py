"""FastAPI sidecar application for mom-bot (Epic 2.6 B2).

Exposes the following endpoints:

- ``GET /api/version`` — no auth; returns the sidecar version string.
- ``GET /api/health`` — no auth; returns bot-connected status.
- ``POST /api/internal/role-sync`` — Bearer-gated webhook that siege-web
  calls when a member's attack-day assignment changes.
- ``GET /api/members`` — Bearer-gated; returns the full guild member list.
- ``GET /api/members/{discord_user_id}`` — Bearer-gated; returns a single
  guild member with an ``is_member`` discriminator.

``/api/internal/role-sync`` decision tree (per contract spec § 6–7 and
issue #65 AC):

1. Bearer auth fails → ``401 Unauthorized``.
2. Request body fails Pydantic validation → ``400 Bad Request``.
3. Look up ``member_role_sync_state`` by ``discord_id``:

   a. **Exact replay** — ``(assigned_at, action, day_number)`` matches stored
      key → return the stored response verbatim; do NOT invoke the role
      service; log INFO ``role_sync_idempotent_replay`` with ``attempt=2``.

   b. **Stale write** — incoming ``assigned_at`` < stored ``last_assigned_at``
      AND key does not exactly match → return
      ``{status:"skipped", reason:"stale_write", last_assigned_at:<stored>}``
      without invoking; do NOT update stored row.

   c. **Fresh write** (no row, or incoming ``assigned_at`` ≥ stored) →
      invoke the role service, UPSERT the row, return the result.

All role-service outcomes (``applied``, ``partial``, ``skipped``, ``failed``)
are returned as ``200`` with a structured JSON body.  A ``failed`` result is
a **delivered** response, not an HTTP-layer error.

Response body fields (per contract spec § 3):

- ``status``: ``"applied" | "partial" | "skipped" | "failed"``
- ``added``: ``list[int]`` — role snowflakes added (empty list if none)
- ``removed``: ``list[int]`` — role snowflakes removed (empty list if none)
- ``reason``: ``str | None`` — present when ``status != "applied"``
- ``last_assigned_at``: ``str | None`` — present only on ``stale_write``

Structured log record emitted per call (AC requirement):

  ``role_sync correlation_id=… discord_id=… siege_id=… day_number=… action=…
  assigned_at=… status=… added=… removed=… attempt=…``

Member endpoints — multi-guild decision
---------------------------------------
Both ``GET /api/members`` and ``GET /api/members/{discord_user_id}`` are
scoped to the **single guild** supplied to :func:`build_app` via the
``guild`` parameter.  The guild object is constructed at startup from the
``DISCORD_GUILD_ID`` environment variable and passed in by the entrypoint.

This matches the siege-web sidecar contract, which is inherently single-guild.
Supporting multiple guilds would require a contract change (a new ``guild_id``
query/path parameter) and is deferred to a future issue if needed.

Discord exception translation
------------------------------
Both member endpoints translate discord.py exceptions to HTTP status codes
per INTERFACE.md § Error semantics:

- ``discord.Forbidden`` (403 from Discord) → HTTP 403
- ``discord.HTTPException`` with ``status < 500`` → HTTP 502
- ``discord.HTTPException`` with ``status >= 500`` → HTTP 503
- ``asyncio.TimeoutError`` → HTTP 503

``discord.Forbidden`` and ``discord.NotFound`` are subclasses of
``discord.HTTPException``; they are handled by the more-specific exception
handlers registered on the app so they do not reach the generic handler.

``discord.NotFound`` from ``guild.fetch_member()`` is **not** an error —
it means the user is not in the guild, and the endpoint returns 200 with
``is_member: false`` in that case.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Literal
from weakref import WeakValueDictionary

import discord
from fastapi import Depends, FastAPI, Request, status
from fastapi import Path as FastAPIPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session, sessionmaker

from mom_bot import __version__ as _pkg_version
from mom_bot.roles.service import apply_day_role
from mom_bot.sidecar.auth import make_bearer_dependency
from mom_bot.sidecar.models import MemberRoleSyncState

__all__ = ["build_app"]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-discord_id asyncio lock registry
#
# Serialises concurrent requests for the same member so the idempotency
# check → apply_day_role → UPSERT sequence is atomic within the process.
# WeakValueDictionary ensures entries for inactive members are garbage-
# collected; without it the dict would grow unboundedly across all members
# the bot has ever processed.
# ---------------------------------------------------------------------------

_discord_id_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _get_lock(discord_id: str) -> asyncio.Lock:
    """Return the per-discord_id asyncio.Lock, creating it if absent.

    Uses a module-level ``WeakValueDictionary`` so locks for members that
    are no longer being processed are garbage-collected automatically.

    Args:
        discord_id: The Discord snowflake string (wire form).

    Returns:
        The existing or newly created :class:`asyncio.Lock` for this member.
    """
    lock = _discord_id_locks.get(discord_id)
    if lock is None:
        lock = asyncio.Lock()
        _discord_id_locks[discord_id] = lock
    return lock


# ---------------------------------------------------------------------------
# Request / response Pydantic models
# ---------------------------------------------------------------------------


class RoleSyncRequest(BaseModel):
    """Inbound payload from siege-web (contract spec § 2).

    Attributes:
        discord_id: Discord snowflake — treated as opaque string per spec.
        siege_id: PK of the siege record; used for correlation logging.
        day_number: Attack-day number.  Required when ``action="assign"``;
            MUST be absent when ``action="unassign"``.
        action: ``"assign"`` or ``"unassign"``.
        assigned_at: ISO-8601 UTC timestamp — monotonic ordering token.
        correlation_id: UUID v4 tracing identifier from the producer.
    """

    discord_id: str
    siege_id: int
    day_number: int | None = None
    action: Literal["assign", "unassign"]
    assigned_at: str
    correlation_id: str

    @model_validator(mode="after")
    def _validate_day_number_conditionality(self) -> RoleSyncRequest:
        """Enforce action / day_number conditionality per contract spec § 2.

        Returns:
            The validated model instance.

        Raises:
            ValueError: If ``action="assign"`` and ``day_number`` is absent,
                or if ``action="unassign"`` and ``day_number`` is present.
        """
        if self.action == "assign" and self.day_number is None:
            raise ValueError("day_number is required when action='assign'")
        if self.action == "unassign" and self.day_number is not None:
            raise ValueError("day_number must be absent when action='unassign'")
        return self


class RoleSyncResponse(BaseModel):
    """Outbound response body (contract spec § 3).

    Attributes:
        status: Overall outcome — ``"applied"``, ``"partial"``,
            ``"skipped"``, or ``"failed"``.
        added: Role snowflakes successfully added.
        removed: Role snowflakes successfully removed.
        reason: Reason code when status is not ``"applied"``.
        last_assigned_at: Stored timestamp; present only on ``stale_write``.
    """

    status: Literal["applied", "partial", "skipped", "failed"]
    added: list[int]
    removed: list[int]
    reason: str | None = None
    last_assigned_at: str | None = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_app(
    *,
    api_key: str,
    bot: discord.Client,
    guild: discord.Guild,
    session_factory: sessionmaker[Session],
) -> FastAPI:
    """Construct the sidecar FastAPI application.

    The app is stateless beyond its closure over ``api_key``, ``bot``,
    ``guild``, and ``session_factory`` — all four are supplied at construction
    time so the same factory can be used in tests (with an in-memory DB and
    fake objects) and in production (with a live Discord gateway and a
    file-backed SQLite DB).

    Endpoints registered:

    - ``GET /api/version`` — no auth; version string from ``mom_bot.__version__``
      optionally suffixed with ``+<BUILD_NUMBER>.<GIT_SHA[:7]>`` env vars.
    - ``GET /api/health`` — no auth; ``{"status": "healthy",
      "bot_connected": <bool>}`` where ``bot_connected`` reflects
      ``bot.is_ready()`` at handler call time (not cached).
    - ``POST /api/internal/role-sync`` — Bearer-gated; existing role-sync
      endpoint documented in this module's docstring.
    - ``GET /api/members`` — Bearer-gated; returns ``[{id, username,
      display_name}]`` for all cached guild members.  The Discord ID field
      is ``id`` (not ``discord_id``) per INTERFACE.md.
    - ``GET /api/members/{discord_user_id}`` — Bearer-gated; looks up a
      single member via ``guild.fetch_member()``.  Returns
      ``{"is_member": true, "discord_id": ..., ...}`` when found, or
      ``{"is_member": false, "discord_id": null, ...}`` when the user is
      not in the guild.  The ``@everyone`` role is excluded from ``roles``
      and ``role_names``.

    Multi-guild decision (issue #177):
    Both member endpoints are scoped to the single ``guild`` supplied here.
    Siege-web's sidecar contract is single-guild; this matches that design.
    Supporting multiple guilds would require a contract change; file a new
    issue if that becomes necessary.

    Args:
        api_key: Expected Bearer token value.  Requests whose
            ``Authorization`` header does not match are rejected with ``401``.
        bot: The :class:`discord.Client` instance.  Consulted in the
            ``/api/health`` handler via ``bot.is_ready()`` at request time.
        guild: The connected :class:`discord.Guild` used by the member
            endpoints and passed to
            :func:`~mom_bot.roles.service.apply_day_role`.
        session_factory: Bound SQLAlchemy session factory used for all DB
            reads and writes inside the endpoint handler.

    Returns:
        A fully-configured :class:`fastapi.FastAPI` instance with all
        sidecar routes registered.
    """
    app = FastAPI(title="mom-bot sidecar", docs_url=None, redoc_url=None)

    _require_bearer = make_bearer_dependency(api_key=api_key)

    # ------------------------------------------------------------------
    # Discord exception → HTTP translation (global handlers).
    #
    # Registered per the error-semantics in INTERFACE.md § Error semantics:
    #   discord.Forbidden (403 from Discord)   → HTTP 403
    #   discord.HTTPException status < 500     → HTTP 502
    #   discord.HTTPException status >= 500    → HTTP 503
    #   asyncio.TimeoutError                   → HTTP 503
    #
    # discord.Forbidden is a subclass of discord.HTTPException; FastAPI
    # resolves exception handlers in MRO order, so the Forbidden handler
    # fires first and the generic HTTPException handler handles the rest.
    # discord.NotFound (also a subclass) is handled per-endpoint as
    # business logic (200 with is_member=false), not error translation.
    # ------------------------------------------------------------------

    @app.exception_handler(discord.Forbidden)
    async def _handle_discord_forbidden(
        _request: Request,
        exc: discord.Forbidden,
    ) -> JSONResponse:
        """Translate discord.Forbidden to HTTP 403.

        Raised when the bot lacks permissions (e.g. cannot fetch member
        data from a locked-down server).  Raw ``exc.text`` is logged
        server-side but never exposed in response bodies.

        Args:
            _request: The incoming FastAPI request (unused).
            exc: The :class:`discord.Forbidden` exception.

        Returns:
            JSONResponse with status 403 and a generic detail message.
        """
        _logger.warning("Discord Forbidden: status=%s text=%r", exc.status, exc.text)
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "Discord permission denied"},
        )

    @app.exception_handler(discord.HTTPException)
    async def _handle_discord_http_exception(
        _request: Request,
        exc: discord.HTTPException,
    ) -> JSONResponse:
        """Translate discord.HTTPException to 502 or 503.

        ``discord.Forbidden`` is handled by its own more-specific handler
        above and will NOT reach this handler.

        Status mapping:
        - ``exc.status < 500``  → 502 Bad Gateway (upstream Discord 4xx)
        - ``exc.status >= 500`` → 503 Service Unavailable (Discord 5xx)

        Raw ``exc.status`` and ``exc.text`` are logged server-side but
        excluded from response bodies per the error envelope policy.

        Args:
            _request: The incoming FastAPI request (unused).
            exc: The :class:`discord.HTTPException` instance.

        Returns:
            JSONResponse with status 502 or 503 and a generic detail.
        """
        _logger.warning("Discord HTTPException: status=%s text=%r", exc.status, exc.text)
        if exc.status < 500:
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={"detail": "Upstream Discord error"},
            )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Discord temporarily unavailable"},
        )

    @app.exception_handler(asyncio.TimeoutError)
    async def _handle_timeout(
        _request: Request,
        exc: asyncio.TimeoutError,
    ) -> JSONResponse:
        """Translate asyncio.TimeoutError to HTTP 503.

        Raised when a Discord API call exceeds its timeout.

        Args:
            _request: The incoming FastAPI request (unused).
            exc: The :class:`asyncio.TimeoutError` instance.

        Returns:
            JSONResponse with status 503 and a generic detail message.
        """
        _logger.warning("Discord timeout: %r", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Discord temporarily unavailable"},
        )

    # ------------------------------------------------------------------
    # Override FastAPI's default 422 validation error → return 400 instead.
    # The contract specifies 400 for invalid schema, not 422.
    # ------------------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Convert Pydantic validation errors to 400 or 422.

        Request body and query-parameter validation errors are returned as
        ``400 Bad Request`` per the role-sync contract spec.  Path-parameter
        validation errors (``loc[0] == "path"``) are returned as
        ``422 Unprocessable Entity`` per the members endpoint contract
        (INTERFACE.md § ``GET /api/members/{discord_user_id}``).

        Pydantic v2's ``exc.errors()`` may include non-JSON-serializable
        objects (e.g. ``ValueError`` instances in the ``ctx`` key).  We
        serialise them to strings before building the response body.

        Args:
            request: The incoming HTTP request.
            exc: The validation exception raised by Pydantic.

        Returns:
            A 400 or 422 JSON response with the validation detail.
        """
        errors = exc.errors()

        # If every error originates from a path parameter, return 422 so
        # that path-validation on /api/members/{discord_user_id} conforms
        # to INTERFACE.md.  Body/query errors retain 400 (role-sync spec).
        all_path_errors = all(
            isinstance(e.get("loc"), (list, tuple)) and e["loc"][0] == "path" for e in errors
        )
        http_status = 422 if all_path_errors else 400

        # Pydantic v2 errors can include non-serializable ctx values;
        # convert every error dict to string-safe form.
        clean_errors: list[dict[str, Any]] = []
        for err in errors:
            clean: dict[str, Any] = {}
            for k, v in err.items():
                if k == "ctx" and isinstance(v, dict):
                    clean[k] = {ck: str(cv) for ck, cv in v.items()}
                else:
                    clean[k] = v
            clean_errors.append(clean)
        return JSONResponse(
            status_code=http_status,
            content={"detail": clean_errors},
        )

    # ------------------------------------------------------------------
    # Public endpoints (no auth)
    # ------------------------------------------------------------------

    @app.get("/api/version")
    async def version() -> dict[str, str]:
        """Return the sidecar version string.

        Reads the version from the ``mom_bot`` package.  When both
        ``BUILD_NUMBER`` and ``GIT_SHA`` environment variables are set (CI-
        built images), appends ``+<BUILD_NUMBER>.<GIT_SHA[:7]>`` to form a
        full build-qualified version.  In local development the bare package
        version is returned.

        Returns:
            A dict with a single ``version`` key containing the version
            string, e.g. ``{"version": "0.0.1+42.abcdef1"}``.
        """
        semver = _pkg_version
        build_number = os.environ.get("BUILD_NUMBER", "")
        git_sha = os.environ.get("GIT_SHA", "")
        if build_number and git_sha:
            ver = f"{semver}+{build_number}.{git_sha[:7]}"
        else:
            ver = semver
        return {"version": ver}

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        """Return bot connectivity status.

        Calls ``bot.is_ready()`` at handler execution time on every request —
        the result is **not** cached.  This ensures the response reflects the
        bot's gateway state at the moment the health probe runs rather than
        a stale snapshot from app construction.

        Returns:
            A dict with ``status`` (always ``"healthy"``) and
            ``bot_connected`` (bool) reflecting ``bot.is_ready()`` right now.
        """
        return {"status": "healthy", "bot_connected": bot.is_ready()}

    # ------------------------------------------------------------------
    # Member endpoints (Bearer-gated)
    #
    # Both endpoints are scoped to the single ``guild`` supplied to
    # build_app().  See the module docstring for the multi-guild decision.
    # ------------------------------------------------------------------

    @app.get(
        "/api/members",
        dependencies=[Depends(_require_bearer)],
    )
    async def get_members() -> list[dict[str, str]]:
        """Return the full guild member list.

        Reads from the guild's local member cache (``guild.members``).  No
        live Discord API call is made; the response is a best-effort snapshot
        of the cached state at request time.

        Each element has exactly three keys — ``id``, ``username``, and
        ``display_name`` — matching the siege-web INTERFACE.md contract.
        The Discord snowflake field is named ``id`` (not ``discord_id``);
        this is load-bearing and must not be changed.

        Returns:
            A JSON array where each element is a dict with:
            - ``id``: Discord snowflake (numeric string).
            - ``username``: Discord username (``member.name``).
            - ``display_name``: Guild display name
              (``member.display_name``).
        """
        return [
            {
                "id": str(m.id),
                "username": m.name,
                "display_name": m.display_name,
            }
            for m in guild.members
        ]

    @app.get(
        "/api/members/{discord_user_id}",
        dependencies=[Depends(_require_bearer)],
    )
    async def get_member(
        discord_user_id: str = FastAPIPath(..., pattern=r"^\d+$"),
    ) -> dict[str, Any]:
        """Look up a single guild member by Discord user ID.

        Calls ``guild.fetch_member()`` (live Discord API call) to get the
        most up-to-date membership status and role list.  If the user is not
        in the guild, Discord raises ``discord.NotFound`` and the endpoint
        returns 200 with ``is_member: false`` and all other fields ``null``.

        The ``@everyone`` role is excluded from both ``roles`` and
        ``role_names`` in the ``is_member: true`` case.

        All six keys (``is_member``, ``discord_id``, ``username``,
        ``display_name``, ``roles``, ``role_names``) are always present
        regardless of membership status, per INTERFACE.md.

        Args:
            discord_user_id: Discord snowflake (numeric string only).
                FastAPI validates against ``^\\d+$`` before the handler
                runs — non-numeric values are rejected with 422.

        Returns:
            A dict with ``is_member: true`` and populated fields when the
            user is in the guild, or ``is_member: false`` with all other
            fields ``null`` when the user is not.

        Raises:
            HTTPException: 403 if Discord returns Forbidden (translated by
                the global ``_handle_discord_forbidden`` handler).
            HTTPException: 502 if Discord returns a 4xx error other than
                Forbidden (translated by ``_handle_discord_http_exception``).
            HTTPException: 503 if Discord returns a 5xx error or times out
                (translated by the global exception handlers).
        """
        try:
            member = await guild.fetch_member(int(discord_user_id))
        except discord.NotFound:
            return {
                "is_member": False,
                "discord_id": None,
                "username": None,
                "display_name": None,
                "roles": None,
                "role_names": None,
            }
        # discord.Forbidden, discord.HTTPException, and asyncio.TimeoutError
        # are NOT caught here — the global exception handlers translate them
        # to 403, 502, and 503 respectively per the error-envelope policy.
        return {
            "is_member": True,
            "discord_id": str(member.id),
            "username": member.name,
            "display_name": member.display_name,
            "roles": [str(r.id) for r in member.roles if r.name != "@everyone"],
            "role_names": [r.name for r in member.roles if r.name != "@everyone"],
        }

    # ------------------------------------------------------------------
    # Routes (Bearer-gated)
    # ------------------------------------------------------------------

    @app.post(
        "/api/internal/role-sync",
        response_model=RoleSyncResponse,
        dependencies=[Depends(_require_bearer)],
    )
    async def role_sync(body: RoleSyncRequest) -> Any:
        """Handle a day-role-sync webhook from siege-web.

        Implements the full decision tree from the wire contract (§ 6–7)
        and issue #65 AC.  The entire idempotency-check → service call →
        UPSERT sequence is wrapped in a per-``discord_id`` asyncio.Lock to
        prevent a race condition where two concurrent requests for the same
        member could both pass the stale-write check and both write — with
        the older ``assigned_at`` potentially overwriting the newer one.

        1. Acquire the per-discord_id lock.
        2. Look up the stored state for ``body.discord_id``.
        3. **Exact replay** → return stored response, log replay event.
        4. **Stale write** → return skipped/stale_write, do nothing else.
        5. **Fresh write** → invoke role service, UPSERT row, return result.

        If the stored row contains corrupted JSON in ``last_response_added``
        or ``last_response_removed`` (database corruption or prior-version
        bug), the error is logged and the request proceeds as a fresh write,
        overwriting the corrupted row and self-healing the database.

        Args:
            body: Validated request payload.

        Returns:
            A :class:`RoleSyncResponse`-shaped dict; FastAPI serialises it.
        """
        discord_id_str = body.discord_id

        async with _get_lock(discord_id_str):
            with session_factory() as session:
                stored = session.get(MemberRoleSyncState, discord_id_str)

                # ----------------------------------------------------------
                # Step 1: Exact replay check
                # ----------------------------------------------------------
                if stored is not None:
                    key_matches = (
                        stored.last_assigned_at == body.assigned_at
                        and stored.last_action == body.action
                        and stored.last_day_number == body.day_number
                    )
                    if key_matches:
                        # Attempt to decode stored JSON; treat corruption as
                        # a cache miss and fall through to the fresh-write path.
                        try:
                            stored_added = json.loads(stored.last_response_added)
                            stored_removed = json.loads(stored.last_response_removed)
                        except json.JSONDecodeError:
                            _logger.error(
                                "role_sync_json_corrupt correlation_id=%s "
                                "discord_id=%s added_raw=%.120r "
                                "removed_raw=%.120r — treating as cache miss",
                                body.correlation_id,
                                discord_id_str,
                                stored.last_response_added,
                                stored.last_response_removed,
                            )
                            # Fall through to fresh-write below.
                        else:
                            # Exact replay — return stored response; no
                            # service call.
                            _logger.info(
                                "role_sync_idempotent_replay "
                                "correlation_id=%s discord_id=%s "
                                "siege_id=%s day_number=%s action=%s "
                                "assigned_at=%s status=%s added=%s "
                                "removed=%s attempt=2",
                                body.correlation_id,
                                body.discord_id,
                                body.siege_id,
                                body.day_number,
                                body.action,
                                body.assigned_at,
                                stored.last_response_status,
                                stored_added,
                                stored_removed,
                            )
                            return RoleSyncResponse(
                                status=stored.last_response_status,  # type: ignore[arg-type]
                                added=stored_added,
                                removed=stored_removed,
                                reason=stored.last_response_reason,
                            )

                    # ----------------------------------------------------------
                    # Step 2: Stale-write check
                    # ----------------------------------------------------------
                    if body.assigned_at < stored.last_assigned_at:
                        _logger.info(
                            "role_sync correlation_id=%s discord_id=%s "
                            "siege_id=%s day_number=%s action=%s "
                            "assigned_at=%s status=skipped "
                            "reason=stale_write added=[] removed=[] attempt=1",
                            body.correlation_id,
                            body.discord_id,
                            body.siege_id,
                            body.day_number,
                            body.action,
                            body.assigned_at,
                        )
                        return RoleSyncResponse(
                            status="skipped",
                            added=[],
                            removed=[],
                            reason="stale_write",
                            last_assigned_at=stored.last_assigned_at,
                        )

            # ------------------------------------------------------------------
            # Step 3: Fresh write — invoke role service
            # ------------------------------------------------------------------
            result = await apply_day_role(
                guild=guild,
                discord_id=int(body.discord_id),
                action=body.action,
                day_number=body.day_number,
                correlation_id=body.correlation_id,
                session_factory=session_factory,
            )

            # UPSERT the state row.
            with session_factory() as session:
                row = session.get(MemberRoleSyncState, discord_id_str)
                if row is None:
                    row = MemberRoleSyncState(discord_id=discord_id_str)
                    session.add(row)
                row.last_assigned_at = body.assigned_at
                row.last_action = body.action
                row.last_day_number = body.day_number
                row.last_correlation_id = body.correlation_id
                row.last_response_status = result.status
                row.last_response_added = json.dumps(result.added)
                row.last_response_removed = json.dumps(result.removed)
                row.last_response_reason = result.reason
                session.commit()

        _logger.info(
            "role_sync correlation_id=%s discord_id=%s siege_id=%s "
            "day_number=%s action=%s assigned_at=%s status=%s "
            "added=%s removed=%s attempt=1",
            body.correlation_id,
            body.discord_id,
            body.siege_id,
            body.day_number,
            body.action,
            body.assigned_at,
            result.status,
            result.added,
            result.removed,
        )

        return RoleSyncResponse(
            status=result.status,
            added=result.added,
            removed=result.removed,
            reason=result.reason,
        )

    return app
