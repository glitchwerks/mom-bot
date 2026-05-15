"""FastAPI sidecar application for mom-bot (Epic 2.6 B2).

Exposes ``POST /api/internal/role-sync`` — the webhook endpoint that
``rsl-siege-manager`` (siege-web) calls when a member's attack-day
assignment changes.  The endpoint is Bearer-token-gated, persists
idempotency state to a SQLite table, and delegates the Discord role
operations to :func:`~mom_bot.roles.service.apply_day_role`.

Decision tree (per contract spec § 6–7 and issue #65 AC):

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
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Annotated, Any, Literal
from weakref import WeakValueDictionary

import discord
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session, sessionmaker

from mom_bot.roles.service import apply_day_role
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
    guild: discord.Guild,
    session_factory: sessionmaker[Session],
) -> FastAPI:
    """Construct the sidecar FastAPI application.

    The app is stateless beyond its closure over ``api_key``, ``guild``, and
    ``session_factory`` — all three are supplied at construction time so the
    same factory can be used in tests (with an in-memory DB and a mock guild)
    and in production (with a live Discord guild and a file-backed SQLite DB).

    Args:
        api_key: Expected Bearer token value.  Requests whose
            ``Authorization`` header does not match are rejected with ``401``.
        guild: The connected :class:`discord.Guild` passed to
            :func:`~mom_bot.roles.service.apply_day_role`.
        session_factory: Bound SQLAlchemy session factory used for all DB
            reads and writes inside the endpoint handler.

    Returns:
        A fully-configured :class:`fastapi.FastAPI` instance with the
        ``/api/internal/role-sync`` route registered.
    """
    app = FastAPI(title="mom-bot sidecar", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------------
    # Override FastAPI's default 422 validation error → return 400 instead.
    # The contract specifies 400 for invalid schema, not 422.
    # ------------------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Convert Pydantic validation errors to 400 Bad Request.

        Pydantic v2's ``exc.errors()`` may include non-JSON-serializable
        objects (e.g. ``ValueError`` instances in the ``ctx`` key).  We
        serialise them to strings before building the response body.

        Args:
            request: The incoming HTTP request.
            exc: The validation exception raised by Pydantic.

        Returns:
            A 400 JSON response with the validation detail.
        """
        # Pydantic v2 errors can include non-serializable ctx values;
        # convert every error dict to string-safe form.
        errors = []
        for err in exc.errors():
            clean: dict[str, Any] = {}
            for k, v in err.items():
                if k == "ctx" and isinstance(v, dict):
                    clean[k] = {ck: str(cv) for ck, cv in v.items()}
                else:
                    clean[k] = v
            errors.append(clean)
        return JSONResponse(
            status_code=400,
            content={"detail": errors},
        )

    # ------------------------------------------------------------------
    # Auth dependency
    # ------------------------------------------------------------------

    def _require_bearer(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """Validate the Bearer token in the Authorization header.

        Args:
            authorization: Value of the ``Authorization`` header,
                automatically extracted by FastAPI.

        Raises:
            HTTPException: 401 if the header is absent or the token
                does not match ``api_key``.
        """
        if authorization is None:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, api_key):
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    # ------------------------------------------------------------------
    # Route
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
