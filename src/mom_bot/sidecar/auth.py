"""Reusable Bearer-token authentication dependency for the mom-bot sidecar.

All protected sidecar endpoints (phases 3–6 of Epic #128) depend on the
:func:`make_bearer_dependency` factory to validate the shared ``BOT_API_KEY``
secret.

Failure-mode choice
-------------------
This module enforces **two distinct failure modes**, matching the executable
contract defined in
``siege-web/backend/tests/integration/sidecar/test_auth.py:29-134``:

- **Header absent** → **403 Forbidden**
  (body: ``{"detail": "Not authenticated"}``)
- **Header present, wrong token** → **401 Unauthorized** +
  ``WWW-Authenticate: Bearer`` response header

This split is conformance-driven, not a style choice.  The siege-web
integration suite (the authoritative source per INTERFACE.md's own authority
statement — "When this document and the tests disagree, the tests win")
asserts ``response.status_code == 403`` for all missing-header cases and
``response.status_code == 401`` + ``WWW-Authenticate`` header for wrong-token
cases.  Returning 401 for both (as Phase 2 PR #184 originally implemented)
would cause every ported conformance test to fail.

Implementation note: ``fastapi.security.HTTPBearer(auto_error=True)`` returns
403 for both failure modes (missing AND wrong-scheme headers), losing the
required 401 + ``WWW-Authenticate: Bearer`` on wrong-token.
``HTTPBearer(auto_error=False)`` combined with a ``Depends()``-in-signature
approach triggers ruff B008.  We therefore retain manual header parsing via
``fastapi.Header`` and branch on ``None`` to raise 403 (absent) vs 401
(present but wrong).

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

    Two failure modes (per
    ``siege-web/backend/tests/integration/sidecar/test_auth.py:29-134``):

    - Missing header → **403 Forbidden**
      (body: ``{"detail": "Not authenticated"}``).
    - Present header with wrong scheme or wrong token → **401 Unauthorized**
      + ``WWW-Authenticate: Bearer`` response header.

    Args:
        api_key: The expected Bearer token value.  Compared with
            :func:`secrets.compare_digest` to prevent timing attacks.

    Returns:
        A FastAPI-compatible dependency function.  When the dependency
        resolves without raising, the endpoint handler runs normally.

    Raises:
        HTTPException: 403 if the ``Authorization`` header is absent.
        HTTPException: 401 with ``WWW-Authenticate: Bearer`` if the header
            is present but the scheme is not ``Bearer`` or the token does
            not match ``api_key``.

    Example::

        require_bearer = make_bearer_dependency(
            api_key=os.environ["BOT_API_KEY"]
        )

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
                automatically extracted by FastAPI.  ``None`` when the
                header is absent entirely.

        Raises:
            HTTPException: 403 if the header is absent (``authorization``
                is ``None``).
            HTTPException: 401 with ``WWW-Authenticate: Bearer`` if the
                header is present with a wrong or malformed token.
        """
        if authorization is None:
            raise HTTPException(
                status_code=403,
                detail="Not authenticated",
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, api_key):
            raise HTTPException(
                status_code=401,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return _require_bearer
