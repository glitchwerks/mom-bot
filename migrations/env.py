"""Alembic migration environment for mom-bot.

Wires ``mom_bot.db.Base.metadata`` into the Alembic context so that
``alembic revision --autogenerate`` can detect ORM model changes.

The database URL is resolved in this priority order:

1. ``MOM_BOT_DATABASE_URL`` environment variable — used in production and
   staging (value is injected from Azure Key Vault at deploy time).
2. ``sqlalchemy.url`` in ``alembic.ini`` — local-dev SQLite default.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import mom_bot.reminders.models  # noqa: F401 — registers Reminder + ReminderSent on Base.metadata
from mom_bot.db import Base  # noqa: E402 — must follow alembic context setup

# ---------------------------------------------------------------------------
# Alembic Config object — provides access to values in alembic.ini.
# ---------------------------------------------------------------------------

config = context.config

# Set up Python logging from the alembic.ini [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Wire the declarative base so autogenerate can diff against real models.
target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# URL resolution helper
# ---------------------------------------------------------------------------


def _get_url() -> str | None:
    """Return the database URL, preferring the env-var override.

    Returns:
        The URL string if resolved, or ``None`` if neither the env var nor
        the ini value is present (alembic will raise its own error).
    """
    env_url = os.environ.get("MOM_BOT_DATABASE_URL")
    if env_url:
        return env_url
    return config.get_main_option("sqlalchemy.url")


# ---------------------------------------------------------------------------
# Offline mode
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL — no engine or DBAPI connection
    is required.  SQL statements are emitted to the script output instead.
    """
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode
# ---------------------------------------------------------------------------


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an Engine from config and associates a live connection with the
    Alembic context so migrations execute immediately against the database.
    """
    # Override the URL in the section dict before passing to engine_from_config
    # so that MOM_BOT_DATABASE_URL takes precedence over alembic.ini.
    section = dict(config.get_section(config.config_ini_section) or {})
    env_url = os.environ.get("MOM_BOT_DATABASE_URL")
    if env_url:
        section["sqlalchemy.url"] = env_url

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
