"""Acquire an Entra access token for Azure Database for PostgreSQL.

This module replaces the ``curl``-based IMDS token acquisition in
``migrate.sh``.  The ``python:3.12-slim`` image used by the bot does not
ship ``curl``, which caused the Container Apps Job migration to fail with
``curl: not found`` at startup (issue #259).

``azure-identity`` is already a runtime dependency (``pyproject.toml``),
so this module adds no new dependencies.

Typical usage from ``migrate.sh``::

    TOKEN=$(/app/.venv/bin/python -m mom_bot.migrations.acquire_token)

The module-level ``__main__`` block reads ``AZURE_CLIENT_ID`` from the
environment and prints the raw token string to stdout, making it a
drop-in replacement for the old ``curl | python -c "..."`` pipeline.
"""

from __future__ import annotations

import os
import sys

from azure.identity import ManagedIdentityCredential

__all__ = ["get_postgres_access_token", "main"]

# AAD scope for Azure Database for PostgreSQL Flexible Server.
_POSTGRES_AAD_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def get_postgres_access_token(client_id: str) -> str:
    """Acquire an Entra access token for Postgres using a managed identity.

    Uses ``ManagedIdentityCredential`` from ``azure-identity`` to request
    an OAuth 2.0 access token scoped to Azure Database for PostgreSQL.
    ``azure-identity`` handles internal IMDS retries and exponential
    back-off automatically; no additional retry wrapper is needed here.

    Args:
        client_id: The client ID of the user-assigned managed identity
            to use for token acquisition.  Corresponds to the
            ``AZURE_CLIENT_ID`` environment variable set in the Container
            Apps Job Bicep template.

    Returns:
        The raw JWT access token string.  Pass this value as the Postgres
        password (via ``PGPASSWORD``) to authenticate the ``alembic``
        process.

    Raises:
        azure.core.exceptions.ClientAuthenticationError: If the managed
            identity endpoint is unreachable or the identity is not
            authorised.
        Exception: Any unexpected error from the credential or transport
            layer is propagated to the caller without wrapping.
    """
    credential = ManagedIdentityCredential(client_id=client_id)
    token = credential.get_token(_POSTGRES_AAD_SCOPE)
    return str(token.token)


def main() -> None:
    """Entrypoint for ``python -m mom_bot.migrations.acquire_token``.

    Reads ``AZURE_CLIENT_ID`` from the environment, acquires a Postgres
    Entra access token, and writes the raw token to stdout with no
    trailing newline so that shell callers can capture it with
    ``TOKEN=$(...)``.

    Exits with status 1 and writes a human-readable message to stderr
    when ``AZURE_CLIENT_ID`` is absent or empty.
    """
    _client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
    if not _client_id:
        print(
            "[acquire_token] ERROR: AZURE_CLIENT_ID must be set",
            file=sys.stderr,
        )
        sys.exit(1)

    _token = get_postgres_access_token(_client_id)
    print(_token, end="")


if __name__ == "__main__":
    main()
