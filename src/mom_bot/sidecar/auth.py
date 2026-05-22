"""Reusable Bearer-token authentication dependency for the mom-bot sidecar.

All protected sidecar endpoints (phases 3–6 of Epic #128) depend on the
:func:`make_bearer_dependency` factory to validate the shared ``BOT_API_KEY``
secret.

Failure-mode choice
-------------------
This module returns **401 Unauthorized** for both failure modes:

- **Header absent** → 401 (body: ``{"detail": "Missing Authorization header"}``)
- **Wrong token** → 401 + ``WWW-Authenticate: Bearer`` header

The INTERFACE.md conformance note allows either 401 or 403 for a missing
header (the backend's ``BotClient`` treats any 4xx as an auth failure).
Mom-bot chooses 401 in both cases for consistency: a single status code
simplifies operator alerting and avoids a 401/403 split that carries no
semantic benefit for an internal service.

The ``WWW-Authenticate: Bearer`` header is only set on wrong-token responses,
matching the behaviour specified in INTERFACE.md § Authentication.

Usage::

    dep = make_bearer_dependency(api_key="secret")

    @app.get("/api/protected", dependencies=[Depends(dep)])
    async def protected() -> dict:
        ...
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import Header, HTTPException


def make_bearer_dependency(api_key: str) -> Callable[..., None]:
    """Return a FastAPI dependency that validates Bearer tokens.

    The returned callable is safe to use as a FastAPI ``Depends(...)``
    target.  It reads the ``Authorization`` header injected by FastAPI and
    validates it against ``api_key`` using a timing-safe comparison
    (:func:`secrets.compare_digest`).

    Args:
        api_key: The expected Bearer token value.  Compared with
            :func:`secrets.compare_digest` to prevent timing attacks.

    Returns:
        A FastAPI-compatible dependency function.  When the dependency
        resolves without raising, the endpoint handler runs normally.

    Raises:
        HTTPException: 401 if the ``Authorization`` header is absent.
        HTTPException: 401 with ``WWW-Authenticate: Bearer`` if the header
            is present but the scheme is not ``Bearer`` or the token does
            not match ``api_key``.

    Example::

        require_bearer = make_bearer_dependency(api_key=os.environ["BOT_API_KEY"])

        @app.get("/api/protected", dependencies=[Depends(require_bearer)])
        async def handler() -> dict:
            return {"ok": True}
    """

    def _require_bearer(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """Validate the Bearer token in the Authorization header.

        Args:
            authorization: Value of the ``Authorization`` header,
                automatically extracted by FastAPI.

        Raises:
            HTTPException: 401 if the header is absent.
            HTTPException: 401 with ``WWW-Authenticate: Bearer`` if the
                header is present with a wrong or malformed token.
        """
        if authorization is None:
            raise HTTPException(
                status_code=401,
                detail="Missing Authorization header",
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, api_key):
            raise HTTPException(
                status_code=401,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return _require_bearer
