"""Tests for the mom_bot config module (Key Vault secret loading).

TDD: these tests were written before the implementation.  Each test covers one
discrete behaviour of ``config.py``; run them first to confirm they all fail
(ImportError / AttributeError), then implement the module to make them green.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_secret_client(secret_value: str | None) -> MagicMock:
    """Return a mock SecretClient whose get_secret behaves as configured.

    Args:
        secret_value: The string value to return, or ``None`` to simulate a
            404 (ResourceNotFoundError).

    Returns:
        A ``MagicMock`` that mimics ``azure.keyvault.secrets.SecretClient``.
    """
    client = MagicMock()
    if secret_value is None:
        from azure.core.exceptions import ResourceNotFoundError

        client.get_secret.side_effect = ResourceNotFoundError("secret not found")
    else:
        mock_bundle = MagicMock()
        mock_bundle.value = secret_value
        client.get_secret.return_value = mock_bundle
    return client


# ---------------------------------------------------------------------------
# Test 1 — invalid MOM_BOT_ENV raises ValueError
# ---------------------------------------------------------------------------


def test_invalid_env_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOM_BOT_ENV=staging must raise ValueError with a clear message.

    Only 'dev' and 'prod' are valid values.  Any other string must be rejected
    at module load so the misconfiguration is caught immediately at startup
    rather than silently at secret-fetch time.

    The validation runs at module-level on every (re)load, so we import and
    reload inside the pytest.raises context manager to capture the error
    regardless of whether the module was already cached.
    """
    monkeypatch.setenv("MOM_BOT_ENV", "staging")
    monkeypatch.setenv("MOM_BOT_KEY_VAULT_NAME", "kv-mombot-eastus2")

    import sys

    with pytest.raises(ValueError, match="staging"):
        # Remove cached module so reload re-executes module-level code.
        sys.modules.pop("mom_bot.config", None)
        import mom_bot.config  # noqa: F401  # triggers module-level validation


# ---------------------------------------------------------------------------
# Test 2 — load_secret raises ConfigError on 404
# ---------------------------------------------------------------------------


def test_load_secret_raises_config_error_on_missing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_secret must raise ConfigError when the KV secret does not exist.

    Simulates azure.core.exceptions.ResourceNotFoundError (HTTP 404) from the
    SecretClient and asserts that config.ConfigError is raised with the secret
    name in the message.
    """
    monkeypatch.setenv("MOM_BOT_ENV", "dev")
    monkeypatch.setenv("MOM_BOT_KEY_VAULT_NAME", "kv-mombot-eastus2")

    import importlib

    import mom_bot.config as config_module

    importlib.reload(config_module)

    mock_client = _make_secret_client(secret_value=None)

    with patch.object(config_module, "_get_secret_client", return_value=mock_client):
        with pytest.raises(config_module.ConfigError, match="dev-discord-token"):
            config_module.load_secret("discord-token")


# ---------------------------------------------------------------------------
# Test 3 — correct prefix is applied
# ---------------------------------------------------------------------------


def test_load_secret_applies_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_secret('discord-token') with MOM_BOT_ENV=dev must query 'dev-discord-token'.

    The prefix scheme is ``<env>-<name>``, where env is either 'dev' or 'prod'.
    Verifying the exact key passed to SecretClient ensures the prefix is applied
    and not just tacked on silently.
    """
    monkeypatch.setenv("MOM_BOT_ENV", "dev")
    monkeypatch.setenv("MOM_BOT_KEY_VAULT_NAME", "kv-mombot-eastus2")

    import importlib

    import mom_bot.config as config_module

    importlib.reload(config_module)

    mock_client = _make_secret_client(secret_value="tok-abc123")

    with patch.object(config_module, "_get_secret_client", return_value=mock_client):
        result = config_module.load_secret("discord-token")

    assert result == "tok-abc123"
    mock_client.get_secret.assert_called_once_with("dev-discord-token")


# ---------------------------------------------------------------------------
# Test 4 — cache: second call does not re-hit Key Vault
# ---------------------------------------------------------------------------


def test_load_secret_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second load_secret call with the same name must not call SecretClient again.

    The in-memory cache prevents redundant KV round-trips within a single
    process lifetime.  We confirm that get_secret is called exactly once for
    two consecutive load_secret('discord-token') calls.
    """
    monkeypatch.setenv("MOM_BOT_ENV", "dev")
    monkeypatch.setenv("MOM_BOT_KEY_VAULT_NAME", "kv-mombot-eastus2")

    import importlib

    import mom_bot.config as config_module

    importlib.reload(config_module)

    mock_client = _make_secret_client(secret_value="tok-xyz")

    with patch.object(config_module, "_get_secret_client", return_value=mock_client):
        first = config_module.load_secret("discord-token")
        second = config_module.load_secret("discord-token")

    assert first == second == "tok-xyz"
    # SecretClient.get_secret must be called exactly once (cache hit on second call).
    mock_client.get_secret.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5 — MOM_BOT_ENV=dev yields AzureCliCredential
# ---------------------------------------------------------------------------


def test_dev_env_uses_azure_cli_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_credential() must return AzureCliCredential when MOM_BOT_ENV=dev.

    On developer laptops, az login provides the token.  AzureCliCredential
    targets the az CLI directly, bypassing DefaultAzureCredential's 9-step
    chain (which times out 25 s on the IMDS endpoint before reaching az).
    """
    import importlib
    import sys

    from azure.identity import AzureCliCredential

    monkeypatch.setenv("MOM_BOT_ENV", "dev")
    sys.modules.pop("mom_bot.config", None)
    import mom_bot.config as config_module

    importlib.reload(config_module)

    credential = config_module._build_credential()

    assert isinstance(credential, AzureCliCredential)


# ---------------------------------------------------------------------------
# Test 6 — MOM_BOT_ENV=prod yields ManagedIdentityCredential
# ---------------------------------------------------------------------------


def test_prod_env_uses_managed_identity_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_credential() must return ManagedIdentityCredential when MOM_BOT_ENV=prod.

    In production the Container App has a user-assigned managed identity
    (mi-mom-bot) with Key Vault Secrets User on kv-mombot-eastus2.
    ManagedIdentityCredential uses the IMDS endpoint on the Container App
    host where it is always reachable, avoiding the az CLI entirely.
    """
    import importlib
    import sys

    from azure.identity import ManagedIdentityCredential

    monkeypatch.setenv("MOM_BOT_ENV", "prod")
    sys.modules.pop("mom_bot.config", None)
    import mom_bot.config as config_module

    importlib.reload(config_module)

    credential = config_module._build_credential()

    assert isinstance(credential, ManagedIdentityCredential)


# ---------------------------------------------------------------------------
# Test 7 — ConfigError rejects both secret_name and message
# ---------------------------------------------------------------------------


def test_config_error_rejects_both_secret_name_and_message() -> None:
    """ConfigError contract: exactly one of secret_name or message, not both.

    Passing both arguments is ambiguous — ``message`` would silently win while
    ``secret_name`` is ignored.  The constructor must raise ``ValueError``
    immediately so callers cannot accidentally construct a misleading error.
    """
    from mom_bot.config import ConfigError

    with pytest.raises(ValueError, match="exactly one"):
        ConfigError(secret_name="foo", message="bar")


# ---------------------------------------------------------------------------
# Test 8 — ConfigError rejects no arguments
# ---------------------------------------------------------------------------


def test_config_error_rejects_no_arguments() -> None:
    """ConfigError contract: at least one of secret_name or message is required.

    Calling ``ConfigError()`` with no arguments would produce a meaningless
    exception with no diagnostic information.  The constructor must raise
    ``ValueError`` to surface the programming error early.
    """
    from mom_bot.config import ConfigError

    with pytest.raises(ValueError, match="either secret_name or message"):
        ConfigError()
