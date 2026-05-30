"""Unit tests for mom_bot.migrations.acquire_token.

These tests exercise the token-acquisition helper using mocked credentials
so that no real IMDS / Azure network call is made.  The real IMDS endpoint
is only reachable inside an Azure Container Apps environment; running the
helper with a real managed identity locally is intentionally out of scope.

The ``TestMain`` class covers the ``main()`` function (the ``__main__``
entrypoint) using monkeypatching so no subprocess is spawned.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
_CLIENT_ID = "test-client-id-1234"
_FAKE_TOKEN = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.fake"


def _make_token_response(token: str = _FAKE_TOKEN) -> SimpleNamespace:
    """Return a minimal token response object matching AccessToken protocol.

    Args:
        token: Raw token string to embed.

    Returns:
        A SimpleNamespace with a ``.token`` attribute.
    """
    return SimpleNamespace(token=token)


# ---------------------------------------------------------------------------
# Tests for get_postgres_access_token
# ---------------------------------------------------------------------------


class TestGetPostgresAccessToken:
    """Tests for ``get_postgres_access_token``."""

    def test_returns_token_string_on_success(self) -> None:
        """Returns the raw token string when credential succeeds.

        Monkeypatches ManagedIdentityCredential so no IMDS call is made.
        Verifies the helper extracts and returns ``.token`` from the
        credential response.
        """
        from mom_bot.migrations.acquire_token import get_postgres_access_token

        mock_cred = MagicMock()
        mock_cred.get_token.return_value = _make_token_response(_FAKE_TOKEN)

        with patch(
            "mom_bot.migrations.acquire_token.ManagedIdentityCredential",
            return_value=mock_cred,
        ):
            result = get_postgres_access_token(_CLIENT_ID)

        assert result == _FAKE_TOKEN

    def test_passes_correct_scope(self) -> None:
        """Calls ``get_token`` with the Postgres AAD scope.

        Verifies the helper passes the exact scope string required by
        Azure Database for PostgreSQL Flexible Server AAD authentication.
        """
        from mom_bot.migrations.acquire_token import get_postgres_access_token

        mock_cred = MagicMock()
        mock_cred.get_token.return_value = _make_token_response()

        with patch(
            "mom_bot.migrations.acquire_token.ManagedIdentityCredential",
            return_value=mock_cred,
        ):
            get_postgres_access_token(_CLIENT_ID)

        mock_cred.get_token.assert_called_once_with(_SCOPE)

    def test_passes_correct_client_id_to_credential(self) -> None:
        """Constructs ManagedIdentityCredential with the supplied client_id.

        Verifies the helper forwards ``client_id`` as a keyword argument to
        ``ManagedIdentityCredential`` so the correct user-assigned managed
        identity is selected.
        """
        from mom_bot.migrations.acquire_token import get_postgres_access_token

        mock_cred = MagicMock()
        mock_cred.get_token.return_value = _make_token_response()

        with patch(
            "mom_bot.migrations.acquire_token.ManagedIdentityCredential",
        ) as mock_cls:
            mock_cls.return_value = mock_cred
            get_postgres_access_token(_CLIENT_ID)

        mock_cls.assert_called_once_with(client_id=_CLIENT_ID)

    def test_raises_on_credential_error(self) -> None:
        """Propagates exceptions raised by the credential.

        When ``ManagedIdentityCredential.get_token`` raises (e.g., IMDS not
        reachable, identity misconfigured), the helper must not swallow the
        exception — it re-raises so the caller can handle it.
        """
        from mom_bot.migrations.acquire_token import get_postgres_access_token

        mock_cred = MagicMock()
        mock_cred.get_token.side_effect = Exception("IMDS not reachable: connection refused")

        with patch(
            "mom_bot.migrations.acquire_token.ManagedIdentityCredential",
            return_value=mock_cred,
        ):
            with pytest.raises(Exception, match="IMDS not reachable"):
                get_postgres_access_token(_CLIENT_ID)


# ---------------------------------------------------------------------------
# Tests for main() — the __main__ entrypoint
# ---------------------------------------------------------------------------


class TestMain:
    """Tests for ``main()`` (the ``python -m mom_bot.migrations.acquire_token``
    entrypoint).

    All tests call ``main()`` directly rather than launching a subprocess,
    so they are fast and do not require the module to be importable as
    ``__main__``.  Environment manipulation uses ``monkeypatch`` to avoid
    polluting the real process environment.
    """

    def test_missing_client_id_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exits with status 1 when AZURE_CLIENT_ID is absent.

        Verifies that ``main()`` calls ``sys.exit(1)`` and does not attempt
        token acquisition when the variable is not set at all.
        """
        from mom_bot.migrations.acquire_token import main

        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    def test_empty_client_id_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exits with status 1 when AZURE_CLIENT_ID is present but empty.

        A whitespace-only value must also be treated as missing because
        ``strip()`` is applied before the emptiness check.
        """
        from mom_bot.migrations.acquire_token import main

        monkeypatch.setenv("AZURE_CLIENT_ID", "   ")

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    def test_missing_client_id_writes_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Writes an error message to stderr when AZURE_CLIENT_ID is absent.

        The message must mention ``AZURE_CLIENT_ID`` so operators know which
        variable to fix when reviewing job logs.
        """
        from mom_bot.migrations.acquire_token import main

        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)

        with pytest.raises(SystemExit):
            main()

        captured = capsys.readouterr()
        assert "AZURE_CLIENT_ID" in captured.err
        assert captured.out == ""

    def test_happy_path_prints_token_no_newline(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Prints the raw token to stdout with no trailing newline.

        Shell callers capture the output with ``TOKEN=$(...)``.  A trailing
        newline would be stripped by the shell automatically, but the contract
        is ``print(_token, end="")`` — no newline at all — so we assert the
        exact bytes here.
        """
        from mom_bot.migrations.acquire_token import main

        monkeypatch.setenv("AZURE_CLIENT_ID", _CLIENT_ID)

        mock_cred = MagicMock()
        mock_cred.get_token.return_value = _make_token_response(_FAKE_TOKEN)

        with patch(
            "mom_bot.migrations.acquire_token.get_postgres_access_token",
            return_value=_FAKE_TOKEN,
        ):
            main()

        captured = capsys.readouterr()
        assert captured.out == _FAKE_TOKEN
        assert not captured.out.endswith("\n")

    def test_happy_path_no_stderr_output(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Writes nothing to stderr on the happy path.

        Diagnostic output on stderr would pollute CI logs unnecessarily when
        the migration runs successfully.
        """
        from mom_bot.migrations.acquire_token import main

        monkeypatch.setenv("AZURE_CLIENT_ID", _CLIENT_ID)

        with patch(
            "mom_bot.migrations.acquire_token.get_postgres_access_token",
            return_value=_FAKE_TOKEN,
        ):
            main()

        captured = capsys.readouterr()
        assert captured.err == ""
