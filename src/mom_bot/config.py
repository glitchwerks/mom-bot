"""Configuration and secret-loading module for mom_bot.

Reads the runtime environment from ``MOM_BOT_ENV`` (``dev`` or ``prod``) and
the Key Vault name from ``MOM_BOT_KEY_VAULT_NAME``, then provides
:func:`load_secret` for fetching prefixed secrets at runtime.

Dev/prod model:
- **Local dev** — developer's laptop; ``MOM_BOT_ENV=dev``; ``az login``
  provides the credential via ``AzureCliCredential``.
- **Prod (Azure)** — Container App with ``mi-mom-bot`` user-assigned MI;
  ``MOM_BOT_ENV=prod``; ``ManagedIdentityCredential`` authenticates via IMDS.

``DefaultAzureCredential`` is intentionally avoided: on developer laptops it
walks a 9-credential chain that times out 25 s on the IMDS endpoint before
reaching ``az``, and the chain has shown to misroute under Key Vault's
challenge-flow even when ``AzureCliCredential`` alone succeeds.

Secret naming convention: ``<env>-<name>`` (e.g. ``dev-discord-token``).
"""

from __future__ import annotations

import os

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient

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
    """Configuration failure — KV secret missing/empty, or other config-state issue.

    Raised when a required secret cannot be loaded from Key Vault, or when
    Discord client state prevents configuration from completing (e.g. no
    guilds at seed time, channel not found).

    Attributes:
        secret_name: Name of the KV secret involved, or ``None`` for
            non-KV failures (e.g. Discord client state errors).
    """

    def __init__(
        self,
        secret_name: str | None = None,
        message: str | None = None,
    ) -> None:
        """Initialise ConfigError.

        Exactly one of ``secret_name`` or ``message`` must be provided.

        Args:
            secret_name: The Key Vault secret name that was not found.
                When provided, a standard KV-error message is generated.
            message: A custom error message for non-KV config failures
                (e.g. Discord client state errors).

        Raises:
            ValueError: If both ``secret_name`` and ``message`` are
                supplied (ambiguous — exactly one is required).
            ValueError: If neither ``secret_name`` nor ``message`` is
                supplied.
        """
        if secret_name is not None and message is not None:
            raise ValueError("ConfigError requires exactly one of secret_name or message, not both")
        self.secret_name = secret_name
        if message is not None:
            super().__init__(message)
        elif secret_name is not None:
            super().__init__(
                f"Required secret {secret_name!r} not found in Key Vault "
                f"{_KEY_VAULT_NAME!r}. "
                "Ensure the secret exists and the identity has "
                "Key Vault Secrets User."
            )
        else:
            raise ValueError("ConfigError requires either secret_name or message")


# ---------------------------------------------------------------------------
# Internal helpers (patchable in tests)
# ---------------------------------------------------------------------------


def _build_credential() -> TokenCredential:
    """Build the right Azure credential for the current environment.

    For ``MOM_BOT_ENV=prod``, returns :class:`~azure.identity.ManagedIdentityCredential`
    — the Container App has a user-assigned managed identity (``mi-mom-bot``)
    that already has *Key Vault Secrets User* on ``kv-mombot-eastus2``.

    For ``MOM_BOT_ENV=dev`` (the default), returns
    :class:`~azure.identity.AzureCliCredential` — the developer has
    ``az login``'d locally and self-granted *Key Vault Secrets User* on the
    same vault.

    ``DefaultAzureCredential`` is deliberately avoided here:

    - On laptops it walks a 9-credential chain that times out 25 s on the
      IMDS endpoint before falling through to ``az``.
    - The chain has shown to misroute under KV's challenge-flow even when
      ``AzureCliCredential`` alone succeeds — so the chain itself is a flake
      source.

    Returns:
        A :class:`~azure.core.credentials.TokenCredential` appropriate for the
        current environment.
    """
    if _ENV == "prod":
        return ManagedIdentityCredential()
    return AzureCliCredential()


def _get_secret_client() -> SecretClient:
    """Return a SecretClient for the configured Key Vault.

    Delegates credential selection to :func:`_build_credential`, which picks
    ``AzureCliCredential`` for ``dev`` and ``ManagedIdentityCredential`` for
    ``prod``.

    Returns:
        An authenticated :class:`azure.keyvault.secrets.SecretClient`.
    """
    vault_url = f"https://{_KEY_VAULT_NAME}.vault.azure.net/"
    return SecretClient(vault_url=vault_url, credential=_build_credential())


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
