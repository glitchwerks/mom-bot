"""Configuration and secret-loading module for mom_bot.

Reads the runtime environment from ``MOM_BOT_ENV`` (``dev`` or ``prod``) and
the Key Vault name from ``MOM_BOT_KEY_VAULT_NAME``, then provides
:func:`load_secret` for fetching prefixed secrets at runtime.

Dev/prod model (A++):
- **Local dev** — developer's laptop; ``MOM_BOT_ENV=dev``; ``az login``
  provides the credential that ``DefaultAzureCredential`` picks up.
- **Prod (Azure)** — Container App with ``mi-mom-bot`` user-assigned MI;
  ``MOM_BOT_ENV=prod``; ``DefaultAzureCredential`` picks up the MI.

Secret naming convention: ``<env>-<name>`` (e.g. ``dev-discord-token``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_ENVS = frozenset({"dev", "prod"})

# ---------------------------------------------------------------------------
# Module-level state (validated at import time)
# ---------------------------------------------------------------------------

_ENV: str = os.environ.get("MOM_BOT_ENV", "dev")
if _ENV not in _VALID_ENVS:
    raise ValueError(
        f"MOM_BOT_ENV={_ENV!r} is not a valid environment. "
        f"Must be one of: {sorted(_VALID_ENVS)}"
    )

_KEY_VAULT_NAME: str = os.environ.get("MOM_BOT_KEY_VAULT_NAME", "kv-mombot-eastus2")

# In-memory cache: maps fully-qualified secret name → value.
_cache: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when a required secret cannot be loaded from Key Vault.

    Attributes:
        secret_name: The fully-qualified KV secret name that was missing.
    """

    def __init__(self, secret_name: str) -> None:
        """Initialise ConfigError.

        Args:
            secret_name: The Key Vault secret name that was not found.
        """
        self.secret_name = secret_name
        super().__init__(
            f"Required secret {secret_name!r} not found in Key Vault "
            f"{_KEY_VAULT_NAME!r}. "
            "Ensure the secret exists and the identity has Key Vault Secrets User."
        )


# ---------------------------------------------------------------------------
# Internal helpers (patchable in tests)
# ---------------------------------------------------------------------------


def _get_secret_client() -> SecretClient:
    """Return a SecretClient for the configured Key Vault.

    Uses ``DefaultAzureCredential``, which resolves the credential source in
    order: environment variables → workload identity → managed identity →
    Azure CLI (``az login``).  On developer laptops, ``az login`` suffices.
    On Azure, the Container App's user-assigned MI (``mi-mom-bot``) is used.

    Returns:
        An authenticated :class:`azure.keyvault.secrets.SecretClient`.
    """
    vault_url = f"https://{_KEY_VAULT_NAME}.vault.azure.net/"
    credential = DefaultAzureCredential()
    return SecretClient(vault_url=vault_url, credential=credential)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_secret(name: str) -> str:
    """Load a secret from Azure Key Vault, applying the env prefix.

    The secret is fetched as ``<env>-<name>`` (e.g. ``dev-discord-token``
    when ``MOM_BOT_ENV=dev``).  Successful fetches are cached in-process so
    repeated calls do not incur additional Key Vault round-trips.

    Args:
        name: The unprefixed secret name (e.g. ``"discord-token"``).

    Returns:
        The secret value string.

    Raises:
        ConfigError: If the secret does not exist in Key Vault or the identity
            lacks the required ``Key Vault Secrets User`` role.
    """
    qualified_name = f"{_ENV}-{name}"

    if qualified_name in _cache:
        return _cache[qualified_name]

    client = _get_secret_client()
    try:
        bundle = client.get_secret(qualified_name)
    except ResourceNotFoundError as exc:
        raise ConfigError(qualified_name) from exc

    value: str = bundle.value  # type: ignore[assignment]
    _cache[qualified_name] = value
    return value
