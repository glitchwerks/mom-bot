"""SQLAlchemy declarative base and engine factory for mom-bot.

This module establishes the shared ``Base`` that all ORM models in Epic 1+
must inherit from, and provides ``build_session_factory`` for constructing
a SQLAlchemy session factory with AAD-token injection for Postgres.

Usage::

    from mom_bot.db import Base, build_session_factory

    class MyModel(Base):
        __tablename__ = "my_model"
        ...
"""

from __future__ import annotations

import os

from azure.identity import ManagedIdentityCredential
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

__all__ = ["Base", "build_session_factory"]


class Base(DeclarativeBase):
    """Shared declarative base for all mom-bot ORM models.

    All SQLAlchemy model classes should inherit from this ``Base`` so that
    Alembic can discover them via ``Base.metadata`` and generate accurate
    autogenerate migrations.

    This class is intentionally empty — model definitions live in their
    respective feature modules (Epic 1+).
    """


# ---------------------------------------------------------------------------
# Engine factory — added in Phase 3 (#91). The Base class above remains
# unchanged; migrations/env.py imports it as `from mom_bot.db import Base`.
# ---------------------------------------------------------------------------

_OSSDB_AAD_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# pool_recycle ceiling: observed token TTL is 86 min (5160 s). Use 4800 s
# (80 min) for a 6-min safety margin. Citation:
# docs/spike/2026-05-17-postgres-aad-findings.md § Charge 3.
_POOL_RECYCLE_SECONDS = 4800


def build_session_factory(
    db_url: str,
    *,
    aad_client_id: str | None = None,
) -> sessionmaker[Session]:
    """Build a session factory; for Postgres URLs, inject AAD token on connect.

    For Postgres URLs, an AAD access token (audience
    ``https://ossrdbms-aad.database.windows.net/.default``) is acquired from
    the configured user-assigned managed identity on every new physical
    connection and stamped as the ``password`` connect parameter.

    Token TTL observed in spike #101 is ~86 minutes (5147 s).
    ``pool_recycle=4800`` (80 min) forces SQLAlchemy to close and recreate
    physical connections before the token expires. QueuePool does not invoke
    ``do_connect`` on every session checkout — only on new physical
    connections — so ``pool_recycle`` is the primary guard.

    ``pool_pre_ping=True`` issues a cheap ``SELECT 1`` on every checkout and
    transparently reconnects if the connection is dead. This catches token
    expiry, server failover, and network flaps — strictly more robust than
    ``pool_recycle`` alone.

    Connection-pool sizing: ``pool_size=5, max_overflow=5`` (10 connections
    max). B1ms user-accessible ceiling is 35 connections. Deploy-window
    worst-case: old revision pool (10) + new revision pool (10) + CI alembic
    conn (1) + operator psql (1) = 22/35.

    For non-Postgres URLs (sqlite, used in unit tests and local dev), the
    hook is not registered and pool_recycle / pool_size are not set.

    Args:
        db_url: SQLAlchemy URL. ``postgresql+psycopg://...`` triggers
            AAD-token injection and pool_recycle; anything else (notably
            ``sqlite://``) is opened with no password injection.
        aad_client_id: Client ID of the user-assigned managed identity to
            use for token acquisition. Required when ``db_url`` is Postgres.
            Defaults to ``$AZURE_CLIENT_ID`` when not provided.

    Returns:
        A sessionmaker bound to the configured engine.

    Raises:
        RuntimeError: If ``db_url`` is a Postgres URL and neither
            ``aad_client_id`` is provided nor ``AZURE_CLIENT_ID`` is set.
    """
    if db_url.startswith(("postgresql://", "postgresql+psycopg://")):
        engine: Engine = create_engine(
            db_url,
            echo=False,
            pool_recycle=_POOL_RECYCLE_SECONDS,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )
        client_id = aad_client_id or os.environ.get("AZURE_CLIENT_ID")
        if not client_id:
            raise RuntimeError(
                "AZURE_CLIENT_ID must be set (or aad_client_id passed) "
                "when MOM_BOT_DATABASE_URL is a Postgres URL."
            )
        credential = ManagedIdentityCredential(client_id=client_id)

        @event.listens_for(engine, "do_connect")
        def _inject_aad_token(  # type: ignore[no-untyped-def]
            dialect, conn_rec, cargs, cparams
        ) -> None:
            token = credential.get_token(_OSSDB_AAD_SCOPE)
            cparams["password"] = token.token

    else:
        engine = create_engine(db_url, echo=False)

    return sessionmaker(bind=engine)
