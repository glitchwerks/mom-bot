"""Verify Alembic migration 0002 against a live PostgreSQL 16 container.

Runs ``alembic upgrade head`` against a real Postgres instance spun up via
testcontainers and asserts:

- All five expected tables exist after ``upgrade head``.
- The ``ck_fire_time_no_seconds`` CHECK constraint rejects rows where
  ``fire_time_utc`` has a non-zero seconds component.
- A well-formed row (zero seconds) is accepted.

Skipped cleanly when Docker is unavailable so CI without a Docker daemon
does not break.

References: issue #107 (Phase 2 Postgres-portability), issue #91.
"""

from __future__ import annotations

import logging.config
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

# ---------------------------------------------------------------------------
# Session-scoped autouse fixture: prevent alembic from disabling loggers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _no_disable_existing_loggers() -> Any:
    """Force ``disable_existing_loggers=False`` on every ``fileConfig`` call.

    ``alembic.command.upgrade`` invokes ``migrations/env.py``, which calls
    ``logging.config.fileConfig(alembic_ini)``.  The stdlib default for
    ``disable_existing_loggers`` is ``True``, which sets ``.disabled = True``
    on every logger that existed before the call — including ``mom_bot.main``.
    This breaks caplog-based tests that run after this module if they depend
    on those loggers being active.

    Wraps ``fileConfig`` so the ``disable_existing_loggers`` argument is
    always ``False``.
    """
    _real_fileConfig = logging.config.fileConfig

    def _patched_fileConfig(fname: Any, *args: Any, **kwargs: Any) -> None:
        """Delegate to real fileConfig with disable_existing_loggers=False."""
        kwargs["disable_existing_loggers"] = False
        _real_fileConfig(fname, *args, **kwargs)

    with patch("logging.config.fileConfig", side_effect=_patched_fileConfig):
        yield


# ---------------------------------------------------------------------------
# Docker / testcontainers availability guard
# ---------------------------------------------------------------------------

try:
    import docker as _docker

    _docker.from_env().ping()
    _DOCKER_AVAILABLE = True
except Exception:
    _DOCKER_AVAILABLE = False

try:
    from testcontainers.postgres import PostgresContainer  # noqa: E402

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:
    _TESTCONTAINERS_AVAILABLE = False

_SKIP_REASON = (
    "Docker daemon is unavailable or testcontainers not installed — "
    "skipping Postgres integration tests"
)
requires_docker = pytest.mark.skipif(
    not (_DOCKER_AVAILABLE and _TESTCONTAINERS_AVAILABLE),
    reason=_SKIP_REASON,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = str(_REPO_ROOT / "alembic.ini")

# Tables expected after ``alembic upgrade head``.
_EXPECTED_TABLES = frozenset(
    {
        "alembic_version",
        "reminders",
        "reminder_sent",
        "day_role_map",
        "member_role_sync_state",
    }
)


def _make_pg_alembic_config(url: str) -> Config:
    """Create an Alembic Config pointed at the given Postgres URL.

    Args:
        url: SQLAlchemy-compatible ``postgresql+psycopg://…`` connection
            string.

    Returns:
        An Alembic ``Config`` instance with the URL override applied.
    """
    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def pg_url() -> Any:
    """Start a PostgreSQL 16 container and yield its connection URL.

    Uses a unique database name per test run (UUID4 suffix) so parallel
    runs do not collide.

    Yields:
        A ``postgresql+psycopg://…`` connection string string pointing at
        the ephemeral container.

    Skips:
        If Docker is unavailable or testcontainers cannot be imported.
    """
    if not (_DOCKER_AVAILABLE and _TESTCONTAINERS_AVAILABLE):
        pytest.skip(_SKIP_REASON)

    db_name = f"test_{uuid.uuid4().hex[:8]}"
    with PostgresContainer("postgres:16", dbname=db_name) as pg:
        # testcontainers may return postgresql+psycopg2:// or postgresql://.
        # Normalise to postgresql+psycopg:// (psycopg v3 binary wheel).
        raw = pg.get_connection_url()
        url = raw.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
        yield url


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@requires_docker
class TestPostgresMigration:
    """Integration tests for migration 0002 against a live Postgres 16 instance.

    All tests in this class share a single container (``pg_url`` is
    module-scoped).  The schema is migrated exactly once per class via the
    ``_migrated_schema`` autouse fixture — the container itself is the unit
    of isolation.  Individual tests must not assume a clean schema state;
    they assert against the already-migrated database.
    """

    @pytest.fixture(scope="class", autouse=True)
    def _migrated_schema(self, pg_url: str) -> None:
        """Run ``alembic upgrade head`` exactly once for this test class.

        Makes the schema dependency explicit so every test in the class can
        assume migrations have been applied, regardless of execution order.
        No teardown is needed — the container itself is discarded when the
        module fixture exits.

        Args:
            pg_url: Connection string for the ephemeral Postgres container.
        """
        cfg = _make_pg_alembic_config(pg_url)
        command.upgrade(cfg, "head")

    def test_upgrade_head_creates_expected_tables(self, pg_url: str) -> None:
        """``alembic upgrade head`` creates all expected tables on Postgres.

        Asserts that all five expected tables — ``alembic_version``,
        ``reminders``, ``reminder_sent``, ``day_role_map``, and
        ``member_role_sync_state`` — exist after running the full migration
        chain.  Schema is already migrated by the ``_migrated_schema`` class
        fixture.

        Args:
            pg_url: Connection string for the ephemeral Postgres container.
        """
        engine = sa.create_engine(pg_url)
        inspector = sa.inspect(engine)
        actual = set(inspector.get_table_names())
        engine.dispose()

        missing = _EXPECTED_TABLES - actual
        assert not missing, (
            f"Tables missing after 'alembic upgrade head': {missing}. " f"Found: {actual}"
        )

    def test_check_rejects_nonzero_seconds(self, pg_url: str) -> None:
        """CHECK constraint rejects ``fire_time_utc`` with non-zero seconds.

        Attempts to INSERT a row where ``fire_time_utc = '12:00:30'`` and
        expects ``IntegrityError`` from the ``ck_fire_time_no_seconds``
        constraint.

        Args:
            pg_url: Connection string for the ephemeral Postgres container.
        """
        engine = sa.create_engine(pg_url)
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO reminders "
                        "(name, channel_id, weekday, fire_time_utc,"
                        " message_template, created_at, updated_at) "
                        "VALUES ('BadSeconds', 111, 0, '12:00:30',"
                        " 'test', NOW(), NOW())"
                    )
                )
        engine.dispose()

    def test_check_accepts_zero_seconds(self, pg_url: str) -> None:
        """CHECK constraint accepts ``fire_time_utc`` with zero seconds.

        Inserts a row where ``fire_time_utc = '12:00:00'`` and expects no
        error, confirming the constraint allows well-formed values.

        Args:
            pg_url: Connection string for the ephemeral Postgres container.
        """
        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO reminders "
                    "(name, channel_id, weekday, fire_time_utc,"
                    " message_template, created_at, updated_at) "
                    "VALUES ('GoodSeconds', 111, 0, '12:00:00',"
                    " 'test', NOW(), NOW())"
                )
            )
        engine.dispose()

    def test_inserts_realistic_discord_snowflake_round_trip(self, pg_url: str) -> None:
        """19-digit Discord snowflakes survive a Postgres INSERT/SELECT
        round-trip without truncation or overflow.

        Regression test for issue #122 / PR #123, where
        ``reminders.channel_id`` and ``reminders.role_mention_id`` were
        declared as ``INTEGER`` (32-bit) on PostgreSQL, causing
        ``psycopg.errors.NumericValueOutOfRange`` at INSERT time when a real
        Discord snowflake value (e.g. ``1385263344684109955``) was used.
        Migration ``0003_widen_reminder_snowflakes`` widened both columns to
        ``BIGINT`` to fix this.

        Uses the exact snowflake from the #122 prod crash
        (``1385263344684109955``) for both ``channel_id`` and
        ``role_mention_id`` to prove no silent truncation occurs.

        Args:
            pg_url: Connection string for the ephemeral Postgres container.
        """
        snowflake = 1385263344684109955
        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO reminders "
                    "(name, channel_id, weekday, fire_time_utc,"
                    " message_template, role_mention_id,"
                    " created_at, updated_at) "
                    "VALUES ('SnowflakeTest', :channel_id, 0, '09:00:00',"
                    " 'test', :role_mention_id, NOW(), NOW())"
                ).bindparams(
                    channel_id=snowflake,
                    role_mention_id=snowflake,
                )
            )
            row = conn.execute(
                sa.text(
                    "SELECT channel_id, role_mention_id FROM reminders "
                    "WHERE name = 'SnowflakeTest'"
                )
            ).fetchone()

        engine.dispose()

        assert row is not None, "Expected inserted row to be retrievable"
        assert row[0] == snowflake, (
            f"channel_id round-trip failed: " f"inserted {snowflake}, got {row[0]}"
        )
        assert row[1] == snowflake, (
            f"role_mention_id round-trip failed: " f"inserted {snowflake}, got {row[1]}"
        )
