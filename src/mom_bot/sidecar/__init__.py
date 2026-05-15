"""Sidecar HTTP API for mom-bot (Epic 2 / Epic 2.6).

Exposes a FastAPI application that siege-web calls to trigger Discord
role operations.  The application is Bearer-token-gated using the
``discord_bot_api_key`` secret stored in Azure Key Vault.

The sidecar is started as a separate coroutine alongside the discord.py
gateway in the main asyncio event loop.

Public surface
--------------
``build_app``
    Factory that constructs a :class:`fastapi.FastAPI` instance wired with
    all sidecar routes, the authentication dependency, and the shared
    SQLAlchemy session factory.
"""

from mom_bot.sidecar.app import build_app

__all__ = ["build_app"]
