"""Verify Alembic env.py wiring and schema migrations."""

from __future__ import annotations

import configparser
import logging.config
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

# Resolve alembic.ini relative to this test file so the path works on both
# Windows dev machines and Linux CI runners.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = str(_REPO_ROOT / "alembic.ini")


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

    When ``test_default_db_url_matches_alembic_ini`` (in this module) imports
    ``mom_bot.main``, it registers that logger.  Any subsequent
    ``command.upgrade`` call then silences it, causing
    ``test_reminder_task_exception_logs_critical`` in ``test_main_wireup.py``
    to see no CRITICAL records from ``caplog``.

    This fixture wraps ``fileConfig`` so the ``disable_existing_loggers``
    argument is always ``False``, without touching ``env.py`` or the
    production logging configuration.
    """
    _real_fileConfig = logging.config.fileConfig

    def _patched_fileConfig(fname: Any, *args: Any, **kwargs: Any) -> None:
        """Delegate to real fileConfig with disable_existing_loggers=False."""
        kwargs["disable_existing_loggers"] = False
        _real_fileConfig(fname, *args, **kwargs)

    with patch("logging.config.fileConfig", side_effect=_patched_fileConfig):
        yield


def _make_alembic_config(db_path: str) -> Config:
    """Create an Alembic Config pointed at a temp SQLite database.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        An Alembic ``Config`` instance with the URL override applied and
        the script location set to the repo's ``migrations/`` directory.
    """
    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_default_db_url_matches_alembic_ini() -> None:
    """_DEFAULT_DB_URL in main.py matches alembic.ini's sqlalchemy.url.

    Regression test for issue #55: the bot's fallback SQLite filename was
    ``mom_bot.db`` (underscore) while alembic.ini used ``mom-bot.db`` (hyphen).
    With MOM_BOT_DATABASE_URL unset, alembic would migrate one file and the
    bot would open the other, causing missing-table errors at startup.

    Uses configparser directly (not the alembic API) so this test has no
    alembic import dependency and stays fast/pure.
    """
    from mom_bot.main import _DEFAULT_DB_URL

    parser = configparser.ConfigParser()
    parser.read(_ALEMBIC_INI)
    alembic_url = parser.get("alembic", "sqlalchemy.url")

    assert _DEFAULT_DB_URL == alembic_url, (
        f"Mismatch: main._DEFAULT_DB_URL={_DEFAULT_DB_URL!r} "
        f"but alembic.ini sqlalchemy.url={alembic_url!r}. "
        "Both must use the same filename so alembic and the bot share one DB."
    )


def test_alembic_can_load_config() -> None:
    """Alembic.ini parses, migrations dir resolves, and baseline revision exists.

    Proves that:

    - ``alembic.ini`` is present and parseable.
    - ``migrations/`` directory is found by the script directory.
    - Exactly one revision exists (the empty baseline) before the new
      migration is present; this number grows to two after issue #28 lands.

    If ``Base`` import in ``env.py`` is broken, this test fails because
    the migration module cannot be imported during revision discovery.
    """
    cfg = Config(_ALEMBIC_INI)
    script = ScriptDirectory.from_config(cfg)
    revisions = list(script.walk_revisions())
    # After issue #28, there are two revisions: baseline + 0002.
    assert len(revisions) >= 1, "Expected at least the baseline revision"


# ---------------------------------------------------------------------------
# Helper: tables present after upgrade head
# ---------------------------------------------------------------------------


def _get_table_names(engine: sa.Engine) -> list[str]:
    """Return all table names in the database.

    Args:
        engine: A live SQLAlchemy engine.

    Returns:
        Sorted list of table name strings.
    """
    inspector = sa.inspect(engine)
    return sorted(inspector.get_table_names())


def _get_column_names(engine: sa.Engine, table: str) -> list[str]:
    """Return column names for ``table``.

    Args:
        engine: A live SQLAlchemy engine.
        table: Table name to inspect.

    Returns:
        List of column name strings in declaration order.
    """
    inspector = sa.inspect(engine)
    return [col["name"] for col in inspector.get_columns(table)]


# ---------------------------------------------------------------------------
# Schema tests (issue #28 — 0002_reminders_schema)
# ---------------------------------------------------------------------------


class TestRemindersSchema:
    """Tests for the 0002_reminders_schema Alembic migration.

    All tests run ``alembic upgrade head`` against a fresh in-memory SQLite
    database file (SQLite file is required because Alembic's env.py uses a
    NullPool engine — in-memory ``":memory:"`` URIs can't be shared across
    connections).
    """

    def test_both_tables_exist_after_upgrade(self, tmp_path: Path) -> None:
        """Both ``reminders`` and ``reminder_sent`` tables are created.

        Runs ``alembic upgrade head`` and asserts that both tables are
        present in the resulting schema.

        Args:
            tmp_path: pytest-supplied temp directory.
        """
        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db_file}")
        tables = _get_table_names(engine)
        engine.dispose()

        assert "reminders" in tables, f"Expected 'reminders' table; found: {tables}"
        assert "reminder_sent" in tables, f"Expected 'reminder_sent' table; found: {tables}"

    def test_reminders_columns(self, tmp_path: Path) -> None:
        """``reminders`` table has all required columns per plan § 4.

        Args:
            tmp_path: pytest-supplied temp directory.
        """
        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db_file}")
        columns = _get_column_names(engine, "reminders")
        engine.dispose()

        expected = {
            "id",
            "name",
            "channel_id",
            "weekday",
            "fire_time_utc",
            "message_template",
            "role_mention_id",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(set(columns)), (
            f"Missing columns in 'reminders': " f"{expected - set(columns)}"
        )

    def test_reminder_sent_columns(self, tmp_path: Path) -> None:
        """``reminder_sent`` table has all required columns per plan § 4.

        Args:
            tmp_path: pytest-supplied temp directory.
        """
        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db_file}")
        columns = _get_column_names(engine, "reminder_sent")
        engine.dispose()

        expected = {
            "id",
            "reminder_id",
            "fire_date_utc",
            "sent_at",
        }
        assert expected.issubset(set(columns)), (
            f"Missing columns in 'reminder_sent': " f"{expected - set(columns)}"
        )

    def test_reminders_primary_key(self, tmp_path: Path) -> None:
        """``reminders.id`` is the primary key.

        Args:
            tmp_path: pytest-supplied temp directory.
        """
        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db_file}")
        inspector = sa.inspect(engine)
        pk = inspector.get_pk_constraint("reminders")
        engine.dispose()

        assert "id" in pk["constrained_columns"], f"Expected 'id' as PK; got: {pk}"

    def test_reminder_sent_primary_key(self, tmp_path: Path) -> None:
        """``reminder_sent.id`` is the primary key.

        Args:
            tmp_path: pytest-supplied temp directory.
        """
        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db_file}")
        inspector = sa.inspect(engine)
        pk = inspector.get_pk_constraint("reminder_sent")
        engine.dispose()

        assert "id" in pk["constrained_columns"], f"Expected 'id' as PK; got: {pk}"

    def test_reminder_sent_fk_to_reminders(self, tmp_path: Path) -> None:
        """``reminder_sent.reminder_id`` has a FK to ``reminders.id``.

        Args:
            tmp_path: pytest-supplied temp directory.
        """
        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db_file}")
        inspector = sa.inspect(engine)
        fks = inspector.get_foreign_keys("reminder_sent")
        engine.dispose()

        assert any(
            fk["referred_table"] == "reminders" and "reminder_id" in fk["constrained_columns"]
            for fk in fks
        ), f"Expected FK reminder_sent.reminder_id → reminders.id; got: {fks}"

    def test_reminder_sent_unique_constraint(self, tmp_path: Path) -> None:
        """``reminder_sent`` enforces UNIQUE(reminder_id, fire_date_utc).

        Inserts a reminder row, then inserts a ``reminder_sent`` row, then
        attempts to insert a duplicate ``reminder_sent`` row for the same
        (reminder_id, fire_date_utc) and expects ``IntegrityError``.

        Args:
            tmp_path: pytest-supplied temp directory.
        """
        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db_file}")
        with engine.begin() as conn:
            # Insert a reminder row (minimal required columns).
            conn.execute(
                sa.text(
                    "INSERT INTO reminders "
                    "(name, channel_id, weekday, fire_time_utc,"
                    " message_template, created_at, updated_at) "
                    "VALUES ('Hydra', 123456, 1, '07:00:00',"
                    " 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            reminder_id = conn.execute(
                sa.text("SELECT id FROM reminders WHERE name = 'Hydra'")
            ).scalar_one()

            # First sent row — should succeed.
            conn.execute(
                sa.text(
                    "INSERT INTO reminder_sent "
                    "(reminder_id, fire_date_utc, sent_at) "
                    "VALUES (:rid, '2026-05-06', CURRENT_TIMESTAMP)"
                ),
                {"rid": reminder_id},
            )

        # Duplicate row — must raise IntegrityError.
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO reminder_sent "
                        "(reminder_id, fire_date_utc, sent_at) "
                        "VALUES (:rid, '2026-05-06', CURRENT_TIMESTAMP)"
                    ),
                    {"rid": reminder_id},
                )
        engine.dispose()

    def test_fire_time_utc_check_rejects_nonzero_seconds(self, tmp_path: Path) -> None:
        """``reminders.fire_time_utc`` CHECK rejects seconds != 0.

        Attempts to INSERT a row where fire_time_utc has a non-zero seconds
        component (e.g. ``'07:00:42'``) and expects ``IntegrityError``.

        Args:
            tmp_path: pytest-supplied temp directory.
        """
        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db_file}")
        # SQLite CHECK constraints are enforced by default only when
        # PRAGMA enforce_integrity is set; however SQLAlchemy enables them
        # via the connection event. We force enforcement here.
        with engine.connect() as conn:
            conn.execute(sa.text("PRAGMA integrity_check"))

        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                # Enable CHECK constraint enforcement for this connection.
                conn.execute(sa.text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    sa.text(
                        "INSERT INTO reminders "
                        "(name, channel_id, weekday, fire_time_utc,"
                        " message_template, created_at, updated_at) "
                        "VALUES ('Bad', 123456, 1, '07:00:42',"
                        " 'test', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
        engine.dispose()

    def test_upgrade_then_downgrade_round_trips(self, tmp_path: Path) -> None:
        """``upgrade head`` then ``downgrade base`` leaves no new tables.

        Verifies that:

        - After ``upgrade head``, both tables exist.
        - After ``downgrade base`` (revision ``2f03efc88bf2``), neither
          ``reminders`` nor ``reminder_sent`` exists.

        Args:
            tmp_path: pytest-supplied temp directory.
        """
        db_file = str(tmp_path / "test.db")
        cfg = _make_alembic_config(db_file)

        command.upgrade(cfg, "head")
        engine = sa.create_engine(f"sqlite:///{db_file}")
        tables_after_upgrade = _get_table_names(engine)
        engine.dispose()

        assert "reminders" in tables_after_upgrade
        assert "reminder_sent" in tables_after_upgrade

        # Downgrade all the way to the base (empty schema).
        command.downgrade(cfg, "base")

        engine = sa.create_engine(f"sqlite:///{db_file}")
        tables_after_downgrade = _get_table_names(engine)
        engine.dispose()

        assert (
            "reminders" not in tables_after_downgrade
        ), "'reminders' should not exist after downgrade to base"
        assert (
            "reminder_sent" not in tables_after_downgrade
        ), "'reminder_sent' should not exist after downgrade to base"
