"""Spike #101 — Verify PGPASSWORD + AAD token + psycopg3 against Azure Postgres Flexible Server.

Standalone CLI: mints an AAD token via DefaultAzureCredential, injects it as
PGPASSWORD, opens a psycopg3 connection with sslmode=require, runs a smoke
SELECT, and optionally shells out to ``alembic upgrade head``.

This script is THROWAWAY — delete after spike #101 closes.

Usage::

    .venv/Scripts/python.exe scripts/spike/postgres_aad_probe.py \\
        --host pg-mom-bot-spike.postgres.database.azure.com \\
        --user cmb_dev@outlook.com

Prerequisites:
    uv pip install psycopg[binary]   # psycopg3; not in pyproject.toml
    az login && az account set --subscription <sub-id>
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote as _url_quote

# ---------------------------------------------------------------------------
# Logging setup — configured before argument parsing so the format is
# applied globally; level is adjusted after args are parsed.
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# The repo / worktree root is two levels above scripts/spike/
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    p = argparse.ArgumentParser(
        description=(
            "Verify AAD-token + PGPASSWORD + psycopg3 against"
            " Azure Postgres Flexible Server."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", required=True, help="Postgres Flexible Server FQDN.")
    p.add_argument("--db", default="postgres", help="Database name (default: postgres).")
    p.add_argument(
        "--user",
        required=True,
        help="Entra principal name, e.g. cmb_dev@outlook.com.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=5432,
        help="Postgres port (default: 5432).",
    )
    p.add_argument(
        "--token-resource",
        default="https://ossrdbms-aad.database.windows.net",
        help="AAD resource URL for token acquisition.",
    )
    p.add_argument(
        "--alembic",
        action="store_true",
        help="After smoke SELECT, also run `alembic upgrade head`.",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return p


# ---------------------------------------------------------------------------
# Token minting
# ---------------------------------------------------------------------------


def _mask_token(token: str) -> str:
    """Return a masked representation showing only first 10 and last 4 chars.

    Args:
        token: Raw bearer token string.

    Returns:
        Masked string, e.g. ``eyJ0eXAiOi...abcd``.
    """
    if len(token) <= 14:
        return "***"
    return f"{token[:10]}...{token[-4:]}"


def mint_token(resource: str) -> str:
    """Acquire an AAD access token via DefaultAzureCredential.

    Args:
        resource: The AAD resource URL (scope) to request.

    Returns:
        Raw access token string.

    Raises:
        SystemExit: If azure-identity is unavailable or token acquisition fails.
    """
    log.info("=" * 60)
    log.info("STEP 1 — Minting AAD token")
    log.info("  resource : %s", resource)

    try:
        from azure.identity import DefaultAzureCredential  # type: ignore[import-untyped]
    except ImportError:
        log.error(
            "azure-identity is not installed.  Run: uv pip install azure-identity"
        )
        sys.exit(1)

    try:
        cred = DefaultAzureCredential()
        token_obj = cred.get_token(resource)
    except Exception as exc:  # noqa: BLE001
        log.error("Token acquisition failed: %s", exc)
        sys.exit(1)

    raw_token: str = token_obj.token
    expires_unix: int = token_obj.expires_on  # seconds since epoch

    expires_dt = datetime.datetime.fromtimestamp(expires_unix, tz=datetime.UTC)
    now_dt = datetime.datetime.now(tz=datetime.UTC)
    ttl_seconds = int((expires_dt - now_dt).total_seconds())

    log.info("  token    : %s  (len=%d)", _mask_token(raw_token), len(raw_token))
    log.info("  expires  : %d  (%s UTC)", expires_unix, expires_dt.isoformat())
    log.info("  ttl      : %d s", ttl_seconds)

    if ttl_seconds < 30:
        log.warning("Token expires in < 30 s — connection attempt may fail.")

    return raw_token


# ---------------------------------------------------------------------------
# psycopg3 connection + smoke SELECT
# ---------------------------------------------------------------------------


def connect_and_probe(
    host: str,
    port: int,
    db: str,
    user: str,
    token: str,
) -> None:
    """Open a psycopg3 connection using PGPASSWORD=<token> and run smoke SELECTs.

    Sets ``PGPASSWORD`` in the process environment for the duration of the
    call, then removes it.

    Args:
        host: Postgres Flexible Server FQDN.
        port: TCP port.
        db: Database name.
        user: Entra principal name (used as Postgres username).
        token: Raw AAD bearer token used as the password.

    Raises:
        SystemExit: On connection or query failure.
    """
    log.info("=" * 60)
    log.info("STEP 2 — Connecting via psycopg3")

    try:
        import psycopg  # type: ignore[import-untyped]
    except ImportError:
        log.error(
            "psycopg (psycopg3) is not installed.  Run: uv pip install 'psycopg[binary]'"
        )
        sys.exit(1)

    encoded_user = _url_quote(user, safe="")
    log.info(
        "  user     : %s  (encoded: %s)",
        user,
        encoded_user,
    )
    dsn = f"postgresql://{encoded_user}@{host}:{port}/{db}?sslmode=require"
    log.info("  dsn      : %s  (no password logged)", dsn)

    # Inject token as password via PGPASSWORD — scoped, not printed.
    original_pgpassword = os.environ.get("PGPASSWORD")
    os.environ["PGPASSWORD"] = token

    conn: Any = None
    try:
        conn = psycopg.connect(dsn)
        log.info("  status   : connected")

        # Server version banner
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            row = cur.fetchone()
            version_str: str = row[0] if row else "(unknown)"
        log.info("  version  : %s", version_str)

        # Smoke SELECT
        log.info("=" * 60)
        log.info("STEP 3 — Smoke SELECT")
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, current_database(), now()")
            smoke_row = cur.fetchone()
        log.info(
            "  current_user=%s  current_database=%s  now=%s",
            smoke_row[0] if smoke_row else "?",
            smoke_row[1] if smoke_row else "?",
            smoke_row[2] if smoke_row else "?",
        )

    except Exception as exc:  # noqa: BLE001
        log.error("Database probe failed: %s", exc)
        _restore_pgpassword(original_pgpassword)
        sys.exit(1)
    finally:
        if conn is not None:
            conn.close()
            log.debug("Connection closed.")

    _restore_pgpassword(original_pgpassword)


def _restore_pgpassword(original: str | None) -> None:
    """Restore PGPASSWORD to its prior value (or unset it).

    Args:
        original: The value that was in PGPASSWORD before the probe, or None.
    """
    if original is None:
        os.environ.pop("PGPASSWORD", None)
    else:
        os.environ["PGPASSWORD"] = original


# ---------------------------------------------------------------------------
# Alembic upgrade head
# ---------------------------------------------------------------------------


def run_alembic(dsn: str, token: str) -> bool:
    """Shell out to ``alembic upgrade head`` with PGPASSWORD and SQLALCHEMY_DATABASE_URL set.

    Args:
        dsn: SQLAlchemy-compatible DSN for the target database.
        token: Raw AAD bearer token used as the password env var.

    Returns:
        True if alembic exited 0, False otherwise.
    """
    log.info("=" * 60)
    log.info("STEP 4 — alembic upgrade head")
    log.info("  cwd      : %s", _REPO_ROOT)
    log.info("  dsn      : %s  (no password logged)", dsn)

    env = os.environ.copy()
    env["PGPASSWORD"] = token
    env["SQLALCHEMY_DATABASE_URL"] = dsn
    # alembic's env.py reads MOM_BOT_DATABASE_URL; set both to be safe.
    env["MOM_BOT_DATABASE_URL"] = dsn
    # Suppress user-site-packages to avoid collisions with editable installs
    # under %APPDATA%\Roaming\Python (e.g. claude-usage) that caused SRE
    # module-mismatch crashes when bare 'alembic' resolved to a different
    # Python than the venv interpreter running this script.
    env["PYTHONNOUSERSITE"] = "1"

    cmd = [sys.executable, "-m", "alembic", "upgrade", "head"]
    log.info("  cmd      : %s  (PYTHONNOUSERSITE=1)", cmd)

    result = subprocess.run(
        cmd,
        env=env,
        cwd=str(_REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )

    log.info("  return_code : %d", result.returncode)
    if result.stdout:
        for line in result.stdout.splitlines():
            log.info("  [alembic stdout] %s", line)
    if result.stderr:
        for line in result.stderr.splitlines():
            log.warning("  [alembic stderr] %s", line)

    return result.returncode == 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the AAD probe sequence and print a final RESULT line."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    fail_reason: str | None = None

    # Build DSN once — reused for alembic step if requested.
    # Percent-encode the user to handle guest UPNs that contain @ and #
    # (e.g. foo_bar.com#EXT#@tenant.onmicrosoft.com).  RFC 3986 userinfo
    # reserves those chars as delimiters; psycopg3 takes the last @ as the
    # host delimiter and produces a bogus hostname otherwise.
    encoded_user = _url_quote(args.user, safe="")
    log.info(
        "  user     : %s  (encoded: %s)",
        args.user,
        encoded_user,
    )
    dsn = (
        f"postgresql://{encoded_user}@{args.host}:{args.port}/{args.db}"
        "?sslmode=require"
    )
    # Alembic passes this DSN to SQLAlchemy, which defaults postgresql:// to
    # the psycopg2 dialect. This installation only has psycopg3, so override
    # the scheme to postgresql+psycopg:// (the psycopg v3 dialect selector).
    alembic_dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    log.info(
        "  alembic dsn scheme : postgresql+psycopg  (raw psycopg dsn keeps postgresql://)"
    )

    try:
        token = mint_token(args.token_resource)
        connect_and_probe(
            host=args.host,
            port=args.port,
            db=args.db,
            user=args.user,
            token=token,
        )

        if args.alembic:
            ok = run_alembic(dsn=alembic_dsn, token=token)
            if not ok:
                fail_reason = "alembic upgrade head returned non-zero"

    except SystemExit as exc:
        # connect_and_probe / mint_token call sys.exit on failure;
        # capture to emit summary line before re-raising.
        fail_reason = f"sys.exit({exc.code})"
        _print_result(fail_reason)
        raise

    _print_result(fail_reason)


def _print_result(fail_reason: str | None) -> None:
    """Emit the final RESULT summary line.

    Args:
        fail_reason: None if all steps succeeded, otherwise a short
            description of what went wrong.
    """
    log.info("=" * 60)
    if fail_reason is None:
        log.info("RESULT: ok")
    else:
        log.info("RESULT: fail: %s", fail_reason)


if __name__ == "__main__":
    main()
