"""Subprocess tests for migrate.sh.

Runs ``migrate.sh`` via ``sh`` with stub scripts substituted for the real
Python interpreter (``PYTHON_BIN``) and the real alembic binary
(``ALEMBIC_BIN``).  The stubs are tiny shell scripts written to a
``tmp_path`` directory so the tests work without the container image.

The whole module is skipped gracefully when ``sh`` is not on ``PATH`` so
it does not hard-fail in unusual environments; CI (Ubuntu) always has
``sh``.

Tested behaviours
-----------------
- Missing ``AZURE_CLIENT_ID`` → non-zero exit, error mentions the variable.
- Missing ``PGHOST`` → non-zero exit, error mentions the variable.
- Empty token from ``PYTHON_BIN`` stub → exit 1, stderr contains the
  "failed to acquire Entra token" message.
- Happy path: token printed by stub, alembic stub receives the expected
  ``MOM_BOT_DATABASE_URL`` and ``PGPASSWORD`` environment variables.
- ``PGDATABASE`` defaults to ``mom_bot`` when the variable is unset.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module-level skip when sh is unavailable
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None,
    reason="sh not found on PATH — migrate.sh tests require a POSIX shell",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_TOKEN = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.stubtoken"
_PGHOST = "pg-mom-bot.postgres.database.azure.com"
_PGDATABASE = "mom_bot"
_CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# Absolute path to migrate.sh, resolved relative to *this* file's directory
# (tests/ → repo root).
_MIGRATE_SH = Path(__file__).parent.parent / "migrate.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_stub(path: Path, body: str) -> Path:
    """Write a POSIX shell stub script and make it executable.

    Args:
        path: Destination path for the stub file.
        body: Shell script body (must include a shebang line).

    Returns:
        ``path`` — the written file, for chaining.
    """
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _python_stub_prints_token(tmp_path: Path, token: str = _FAKE_TOKEN) -> Path:
    """Return a stub that ignores its args and prints *token* to stdout.

    Args:
        tmp_path: Directory for the stub file.
        token: Token string to emit.

    Returns:
        Path to the executable stub.
    """
    stub = tmp_path / "python_stub.sh"
    # The stub must handle ``-m mom_bot.migrations.acquire_token`` args but
    # can safely ignore them — it just prints the token.
    return _write_stub(stub, f"#!/bin/sh\nprintf '%s' '{token}'\n")


def _python_stub_empty_token(tmp_path: Path) -> Path:
    """Return a stub that prints nothing (empty token) to stdout.

    Args:
        tmp_path: Directory for the stub file.

    Returns:
        Path to the executable stub.
    """
    stub = tmp_path / "python_stub_empty.sh"
    return _write_stub(stub, "#!/bin/sh\n# Intentionally prints nothing.\n")


def _alembic_stub_echo_env(tmp_path: Path) -> Path:
    """Return an alembic stub that echoes its env vars to stdout and exits 0.

    The stub prints ``MOM_BOT_DATABASE_URL`` and ``PGPASSWORD`` on separate
    lines so tests can assert on the values the script exported.

    Because ``migrate.sh`` calls ``exec "$ALEMBIC_BIN" upgrade head``, the
    stub's stdout becomes the script's stdout.

    Args:
        tmp_path: Directory for the stub file.

    Returns:
        Path to the executable stub.
    """
    stub = tmp_path / "alembic_stub.sh"
    return _write_stub(
        stub,
        "#!/bin/sh\n"
        'echo "MOM_BOT_DATABASE_URL=${MOM_BOT_DATABASE_URL}"\n'
        'echo "PGPASSWORD=${PGPASSWORD}"\n',
    )


def _base_env(**overrides: str) -> dict[str, str]:
    """Build a minimal environment dict for running migrate.sh.

    Starts from an empty base (not the test process's full environment) so
    tests are hermetic.  Only variables that ``migrate.sh`` reads or that
    ``sh`` needs to run at all are included.

    Args:
        **overrides: Key-value pairs to merge on top of the base.

    Returns:
        A dict suitable for ``subprocess.run(..., env=...)``.
    """
    base: dict[str, str] = {
        # sh needs at least PATH to find builtins on some systems.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    base.update(overrides)
    return base


def _run_migrate(
    env: dict[str, str],
    migrate_sh: Path = _MIGRATE_SH,
) -> subprocess.CompletedProcess[str]:
    """Run migrate.sh via ``sh`` and return the completed process.

    Args:
        env: Environment variables to pass to the subprocess.
        migrate_sh: Path to the ``migrate.sh`` script under test.

    Returns:
        ``CompletedProcess`` with ``returncode``, ``stdout``, and ``stderr``.
    """
    return subprocess.run(
        ["sh", str(migrate_sh)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnvVarGuards:
    """Tests for the ``set -eu`` / ``:?`` env-var validation guards."""

    def test_missing_azure_client_id_exits_nonzero(self, tmp_path: Path) -> None:
        """Exits non-zero when AZURE_CLIENT_ID is absent.

        The ``:?`` expansion in ``migrate.sh`` causes ``sh`` to exit
        immediately when the variable is unset.
        """
        python_stub = _python_stub_prints_token(tmp_path)
        env = _base_env(
            PYTHON_BIN=str(python_stub),
            PGHOST=_PGHOST,
            # AZURE_CLIENT_ID deliberately omitted
        )
        result = _run_migrate(env)
        assert result.returncode != 0

    def test_missing_azure_client_id_stderr_mentions_variable(self, tmp_path: Path) -> None:
        """Stderr contains 'AZURE_CLIENT_ID' when the variable is missing.

        The shell's ``:?`` error message names the variable so operators
        know what to fix without reading the script source.
        """
        python_stub = _python_stub_prints_token(tmp_path)
        env = _base_env(
            PYTHON_BIN=str(python_stub),
            PGHOST=_PGHOST,
        )
        result = _run_migrate(env)
        assert "AZURE_CLIENT_ID" in result.stderr

    def test_missing_pghost_exits_nonzero(self, tmp_path: Path) -> None:
        """Exits non-zero when PGHOST is absent.

        Even with ``AZURE_CLIENT_ID`` set, the script must fail if the
        Postgres host is not provided.
        """
        python_stub = _python_stub_prints_token(tmp_path)
        env = _base_env(
            PYTHON_BIN=str(python_stub),
            AZURE_CLIENT_ID=_CLIENT_ID,
            # PGHOST deliberately omitted
        )
        result = _run_migrate(env)
        assert result.returncode != 0

    def test_missing_pghost_stderr_mentions_variable(self, tmp_path: Path) -> None:
        """Stderr contains 'PGHOST' when the variable is missing.

        Mirrors the ``AZURE_CLIENT_ID`` guard test — ensures the error
        message is actionable for operators.
        """
        python_stub = _python_stub_prints_token(tmp_path)
        env = _base_env(
            PYTHON_BIN=str(python_stub),
            AZURE_CLIENT_ID=_CLIENT_ID,
        )
        result = _run_migrate(env)
        assert "PGHOST" in result.stderr


class TestEmptyTokenGuard:
    """Tests for the empty-token exit-1 guard."""

    def test_empty_token_exits_1(self, tmp_path: Path) -> None:
        """Exits 1 when the python stub outputs an empty token.

        ``migrate.sh`` checks ``[ -z "$TOKEN" ]`` and exits 1 with an error
        message — this test verifies that guard fires when the token
        acquisition script produces no output.
        """
        python_stub = _python_stub_empty_token(tmp_path)
        env = _base_env(
            PYTHON_BIN=str(python_stub),
            AZURE_CLIENT_ID=_CLIENT_ID,
            PGHOST=_PGHOST,
        )
        result = _run_migrate(env)
        assert result.returncode == 1

    def test_empty_token_message_in_output(self, tmp_path: Path) -> None:
        """Stderr or stdout contains the 'failed to acquire Entra token' message.

        The message must name the failure clearly so it surfaces in job logs.
        """
        python_stub = _python_stub_empty_token(tmp_path)
        env = _base_env(
            PYTHON_BIN=str(python_stub),
            AZURE_CLIENT_ID=_CLIENT_ID,
            PGHOST=_PGHOST,
        )
        result = _run_migrate(env)
        combined = result.stdout + result.stderr
        assert "failed to acquire Entra token" in combined


class TestHappyPath:
    """Tests for the successful migration path."""

    def test_reaches_alembic_on_happy_path(self, tmp_path: Path) -> None:
        """The script reaches the alembic stub when all env vars are set.

        Verifies the end-to-end flow completes without an error exit when
        both the token stub and the alembic stub are wired up correctly.
        """
        python_stub = _python_stub_prints_token(tmp_path)
        alembic_stub = _alembic_stub_echo_env(tmp_path)
        env = _base_env(
            PYTHON_BIN=str(python_stub),
            ALEMBIC_BIN=str(alembic_stub),
            AZURE_CLIENT_ID=_CLIENT_ID,
            PGHOST=_PGHOST,
            PGDATABASE=_PGDATABASE,
        )
        result = _run_migrate(env)
        assert result.returncode == 0

    def test_mom_bot_database_url_is_correct(self, tmp_path: Path) -> None:
        """``MOM_BOT_DATABASE_URL`` has the expected value on the happy path.

        The URL must be exactly::

            postgresql+psycopg://mi-mom-bot@<PGHOST>:5432/<PGDATABASE>?sslmode=require

        Any deviation (wrong scheme, missing port, wrong username) would
        silently break the alembic migration in production.
        """
        python_stub = _python_stub_prints_token(tmp_path)
        alembic_stub = _alembic_stub_echo_env(tmp_path)
        env = _base_env(
            PYTHON_BIN=str(python_stub),
            ALEMBIC_BIN=str(alembic_stub),
            AZURE_CLIENT_ID=_CLIENT_ID,
            PGHOST=_PGHOST,
            PGDATABASE=_PGDATABASE,
        )
        result = _run_migrate(env)

        expected_url = (
            f"postgresql+psycopg://mi-mom-bot@{_PGHOST}:5432/{_PGDATABASE}" "?sslmode=require"
        )
        assert f"MOM_BOT_DATABASE_URL={expected_url}" in result.stdout

    def test_pgpassword_is_the_token(self, tmp_path: Path) -> None:
        """``PGPASSWORD`` is set to the token value on the happy path.

        The alembic stub echoes its environment; this test verifies the
        token captured from the python stub was forwarded as ``PGPASSWORD``.
        """
        python_stub = _python_stub_prints_token(tmp_path, token=_FAKE_TOKEN)
        alembic_stub = _alembic_stub_echo_env(tmp_path)
        env = _base_env(
            PYTHON_BIN=str(python_stub),
            ALEMBIC_BIN=str(alembic_stub),
            AZURE_CLIENT_ID=_CLIENT_ID,
            PGHOST=_PGHOST,
            PGDATABASE=_PGDATABASE,
        )
        result = _run_migrate(env)
        assert f"PGPASSWORD={_FAKE_TOKEN}" in result.stdout


class TestPgdatabaseDefault:
    """Tests for the PGDATABASE default value."""

    def test_pgdatabase_defaults_to_mom_bot_when_unset(self, tmp_path: Path) -> None:
        """URL uses ``mom_bot`` as the database name when PGDATABASE is unset.

        The ``:=mom_bot`` default in ``migrate.sh`` must produce a URL with
        ``/mom_bot`` in the path segment.
        """
        python_stub = _python_stub_prints_token(tmp_path)
        alembic_stub = _alembic_stub_echo_env(tmp_path)
        env = _base_env(
            PYTHON_BIN=str(python_stub),
            ALEMBIC_BIN=str(alembic_stub),
            AZURE_CLIENT_ID=_CLIENT_ID,
            PGHOST=_PGHOST,
            # PGDATABASE deliberately omitted — must default to mom_bot
        )
        result = _run_migrate(env)

        expected_url = f"postgresql+psycopg://mi-mom-bot@{_PGHOST}:5432/mom_bot" "?sslmode=require"
        assert f"MOM_BOT_DATABASE_URL={expected_url}" in result.stdout
