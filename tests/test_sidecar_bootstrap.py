"""Tests for sidecar bootstrap wiring in MomBot (issue #161).

TDD: tests written before implementation.  Each test covers one discrete
behaviour of the sidecar startup/shutdown lifecycle.

Design notes
------------
- ``start_health_server`` is patched via the ``mock_health_server`` fixture
  to prevent port 8080 conflicts, identical to the pattern in
  ``test_main_wireup.py``.
- ``load_secret`` is patched to return canned values; no Key Vault calls.
- ``wait_until_ready`` is patched to return immediately — the reminder task
  awaits it inside a background coroutine; without the patch the task blocks
  indefinitely on an uninitialised client.
- ``seed_day_role_map`` is patched to a no-op AsyncMock — these tests are not
  exercising guild seed logic and the real implementation needs a live guild.
- ``uvicorn.Server.serve`` is patched to an AsyncMock so no real socket is
  bound during tests.
- The sidecar task is observed via ``bot._sidecar_task`` which must be set
  after ``on_ready`` fires.
- ``bot.get_guild`` is patched to return a minimal fake guild so the sidecar
  bootstrap's ``client.get_guild(int(guild_id))`` call receives a real-ish
  guild object rather than ``None``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mom_bot.db import Base

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_GUILD_ID = 999999999999999999
_CHANNEL_NAME = "reminders"
_ROLE_NAME = "Member"
_API_KEY = "test-sidecar-api-key"

_LOAD_SECRET_VALUES: dict[str, str] = {
    "guild-id": str(_GUILD_ID),
    "reminder-channel-name": _CHANNEL_NAME,
    "reminder-mention-role-name": _ROLE_NAME,
    "discord-bot-api-key": _API_KEY,
}


def _fake_load_secret(name: str) -> str:
    """Return canned secret values keyed by unprefixed name.

    Args:
        name: Unprefixed secret name (e.g. ``"guild-id"``).

    Returns:
        A canned test value.

    Raises:
        KeyError: If ``name`` is not in the canned table.
    """
    return _LOAD_SECRET_VALUES[name]


def _make_engine() -> Any:
    """Create an in-memory SQLite engine with all ORM tables.

    Returns:
        A SQLAlchemy engine backed by an in-memory SQLite database.
    """
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    return engine


def _make_session_factory(engine: Any) -> Any:
    """Return a sessionmaker bound to the given engine.

    Args:
        engine: SQLAlchemy engine to bind.

    Returns:
        A :class:`~sqlalchemy.orm.sessionmaker` bound to ``engine``.
    """
    return sessionmaker(bind=engine)


def _make_fake_guild() -> MagicMock:
    """Build a minimal fake discord.Guild for use in bootstrap tests.

    Returns:
        A MagicMock with ``spec=discord.Guild`` and the test guild ID.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.id = _GUILD_ID
    guild.name = "fake-guild"
    guild.roles = []
    return guild


# ---------------------------------------------------------------------------
# Shared pytest fixture — health server mock
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_health_server() -> Any:
    """Patch start_health_server to prevent real port 8080 binds.

    Mirrors the autouse fixture from ``test_main_wireup.py``.

    Yields:
        The AsyncMock that replaced ``start_health_server``.
    """
    runner_mock = MagicMock()
    runner_mock.cleanup = AsyncMock()
    site_mock = MagicMock()
    health_mock = AsyncMock(return_value=(runner_mock, site_mock))
    with patch("mom_bot.main.start_health_server", health_mock):
        yield health_mock


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _base_patches(
    bot: Any,
    session_factory: Any,
    fake_guild: MagicMock,
) -> list[Any]:
    """Return the base set of patch context managers for on_ready tests.

    All on_ready tests need: load_secret, wait_until_ready, tree.sync,
    _build_session_factory, seed_day_role_map (no-op), get_guild, and
    uvicorn.Server.serve (no-op).

    Args:
        bot: The MomBot instance under test.
        session_factory: In-memory session factory.
        fake_guild: Fake discord.Guild for guild lookups.

    Returns:
        List of patch context managers.
    """
    return [
        patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
        patch("mom_bot.reminders.seed.load_secret", side_effect=_fake_load_secret),
        patch.object(bot, "wait_until_ready", new_callable=AsyncMock),
        patch.object(bot.tree, "sync", new_callable=AsyncMock),
        patch("mom_bot.main._build_session_factory", return_value=session_factory),
        # on_ready calls seed_day_role_map — patch to no-op so no live guild needed.
        patch("mom_bot.main.seed_day_role_map", new_callable=AsyncMock),
        # _start_reminders_after_ready calls _maybe_seed_reminders inside a
        # background task — patch to no-op to avoid real guild/channel resolution.
        patch("mom_bot.main._maybe_seed_reminders"),
        # on_ready will call get_guild to look up the real guild object.
        patch.object(bot, "get_guild", return_value=fake_guild),
        # Prevent real socket bind.
        patch("uvicorn.Server.serve", new_callable=AsyncMock),
    ]


async def _run_setup_and_ready(
    bot: Any,
    session_factory: Any,
    fake_guild: MagicMock,
    extra_patches: list[Any] | None = None,
) -> ExitStack:
    """Apply standard patches, run setup_hook then on_ready.

    Applies all base patches plus any extras, calls ``setup_hook`` and
    ``on_ready``, then yields twice to the event loop so the sidecar task is
    created and running.  Returns the open ExitStack so callers can add
    assertions inside the patch context if needed.

    The caller is responsible for entering the returned stack (as a context
    manager) or for calling ``stack.close()`` when done.

    Args:
        bot: The MomBot instance under test.
        session_factory: In-memory session factory.
        fake_guild: Fake discord.Guild for guild lookups.
        extra_patches: Additional patch context managers.

    Returns:
        The open :class:`contextlib.ExitStack` with all patches active.
    """
    patches = _base_patches(bot, session_factory, fake_guild)
    if extra_patches:
        patches.extend(extra_patches)

    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)

    await bot.setup_hook()
    await bot.on_ready()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    return stack


async def _cleanup(bot: Any) -> None:
    """Cancel and await sidecar and reminder tasks on the bot.

    Args:
        bot: The MomBot instance whose tasks to cancel.
    """
    for attr in ("_sidecar_task", "_reminder_task"):
        task = getattr(bot, attr, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Test class: sidecar task created in on_ready
# ---------------------------------------------------------------------------


class TestSidecarStartupInOnReady:
    """After on_ready fires, the sidecar uvicorn server is running."""

    @pytest.mark.asyncio
    async def test_on_ready_creates_sidecar_task(
        self,
        mock_health_server: AsyncMock,
    ) -> None:
        """on_ready() must create and store an asyncio.Task for the sidecar.

        After ``on_ready`` completes, ``bot._sidecar_task`` must be a
        non-None :class:`asyncio.Task`.  The task is stored to prevent
        garbage-collection and to allow ``close()`` to cancel it.
        """
        from mom_bot.main import MomBot, build_intents

        engine = _make_engine()
        session_factory = _make_session_factory(engine)
        fake_guild = _make_fake_guild()
        bot = MomBot(intents=build_intents())

        stack = await _run_setup_and_ready(bot, session_factory, fake_guild)
        stack.close()

        assert bot._sidecar_task is not None, "bot._sidecar_task must be set after on_ready() fires"
        assert isinstance(
            bot._sidecar_task, asyncio.Task
        ), "bot._sidecar_task must be an asyncio.Task"

        await _cleanup(bot)

    @pytest.mark.asyncio
    async def test_on_ready_sidecar_task_is_not_immediately_done(
        self,
        mock_health_server: AsyncMock,
    ) -> None:
        """The sidecar task must still be running after on_ready returns.

        The task wraps an in-process uvicorn server that runs for the bot's
        lifetime.  It must not complete immediately on creation.
        """
        from mom_bot.main import MomBot, build_intents

        engine = _make_engine()
        session_factory = _make_session_factory(engine)
        fake_guild = _make_fake_guild()

        async def _hold_open_serve(*_args: Any, **_kwargs: Any) -> None:
            """Hold the serve coroutine open to simulate a running server."""
            await asyncio.sleep(3600)

        bot = MomBot(intents=build_intents())
        stack = await _run_setup_and_ready(
            bot,
            session_factory,
            fake_guild,
            extra_patches=[patch("uvicorn.Server.serve", side_effect=_hold_open_serve)],
        )
        stack.close()

        assert not bot._sidecar_task.done(), (  # type: ignore[union-attr]
            "sidecar task must still be running after on_ready returns"
        )

        await _cleanup(bot)


# ---------------------------------------------------------------------------
# Test class: sidecar port and bind address
# ---------------------------------------------------------------------------


class TestSidecarUvicornConfig:
    """The uvicorn server is configured for port 8001 on 0.0.0.0."""

    @pytest.mark.asyncio
    async def test_sidecar_server_binds_port_8001_on_all_interfaces(
        self,
        mock_health_server: AsyncMock,
    ) -> None:
        """uvicorn.Config must be created with host='0.0.0.0' and port=8001.

        Container Apps ingress requires bind-all (not localhost).  Port 8001
        matches the Bicep ingress ``targetPort`` from the companion PR for #76.

        Intercepts ``uvicorn.Config`` construction to capture the ``host`` and
        ``port`` kwargs passed by :meth:`MomBot._start_sidecar`.
        """
        from mom_bot.main import MomBot, build_intents

        engine = _make_engine()
        session_factory = _make_session_factory(engine)
        fake_guild = _make_fake_guild()

        captured_kwargs: list[dict[str, Any]] = []
        original_config = __import__("uvicorn").Config

        def _capture_config(*args: Any, **kwargs: Any) -> Any:
            captured_kwargs.append(kwargs)
            return original_config(*args, **kwargs)

        bot = MomBot(intents=build_intents())
        stack = await _run_setup_and_ready(
            bot,
            session_factory,
            fake_guild,
            extra_patches=[
                patch("mom_bot.main.uvicorn.Config", side_effect=_capture_config),
            ],
        )
        stack.close()

        assert captured_kwargs, "uvicorn.Config must have been instantiated during on_ready"
        kwargs = captured_kwargs[0]
        assert kwargs.get("host") == "0.0.0.0", (
            f"uvicorn must bind to 0.0.0.0 (Container Apps requires bind-all); "
            f"got host={kwargs.get('host')!r}"
        )
        assert kwargs.get("port") == 8001, (
            f"uvicorn must listen on port 8001 (matches Bicep ingress targetPort); "
            f"got port={kwargs.get('port')!r}"
        )

        await _cleanup(bot)


# ---------------------------------------------------------------------------
# Test class: bearer key sourced from Key Vault
# ---------------------------------------------------------------------------


class TestSidecarBearerKeySource:
    """The sidecar Bearer key is loaded via load_secret('discord-bot-api-key')."""

    @pytest.mark.asyncio
    async def test_sidecar_uses_discord_bot_api_key_secret(
        self,
        mock_health_server: AsyncMock,
    ) -> None:
        """build_app() must receive the value of 'discord-bot-api-key' from KV.

        Patches ``build_app`` to capture the ``api_key`` argument and verifies
        it matches the canned value for ``'discord-bot-api-key'`` returned by
        ``_fake_load_secret``.
        """
        from mom_bot.main import MomBot, build_intents

        engine = _make_engine()
        session_factory = _make_session_factory(engine)
        fake_guild = _make_fake_guild()

        captured_kwargs: list[dict[str, Any]] = []
        original_build_app = __import__("mom_bot.sidecar.app", fromlist=["build_app"]).build_app

        def _capture_build_app(**kwargs: Any) -> Any:
            captured_kwargs.append(kwargs)
            return original_build_app(**kwargs)

        bot = MomBot(intents=build_intents())
        stack = await _run_setup_and_ready(
            bot,
            session_factory,
            fake_guild,
            extra_patches=[
                patch("mom_bot.main.build_app", side_effect=_capture_build_app),
            ],
        )
        stack.close()

        assert captured_kwargs, "build_app must be called during on_ready"
        assert captured_kwargs[0]["api_key"] == _API_KEY, (
            f"build_app must receive api_key from load_secret('discord-bot-api-key'); "
            f"got api_key={captured_kwargs[0]['api_key']!r}"
        )

        await _cleanup(bot)


# ---------------------------------------------------------------------------
# Test class: graceful shutdown
# ---------------------------------------------------------------------------


class TestSidecarGracefulShutdown:
    """MomBot.close() shuts down the sidecar server cleanly."""

    @pytest.mark.asyncio
    async def test_close_sets_server_should_exit(
        self,
        mock_health_server: AsyncMock,
    ) -> None:
        """MomBot.close() must set server.should_exit = True.

        Container Apps sends SIGTERM; the bot must signal uvicorn to stop
        accepting new requests and drain in-flight ones before exiting.

        Accesses ``bot._sidecar_server`` (stored by ``_start_sidecar``) after
        ``close()`` returns to verify ``should_exit`` was set to ``True``.
        """
        from mom_bot.main import MomBot, build_intents

        engine = _make_engine()
        session_factory = _make_session_factory(engine)
        fake_guild = _make_fake_guild()
        bot = MomBot(intents=build_intents())

        stack = await _run_setup_and_ready(bot, session_factory, fake_guild)
        stack.close()

        assert bot._sidecar_server is not None, (
            "bot._sidecar_server must be set after on_ready() — needed to "
            "verify should_exit is set by close()"
        )
        server = bot._sidecar_server

        # Cancel reminder task before close.
        if bot._reminder_task is not None:
            bot._reminder_task.cancel()
            try:
                await bot._reminder_task
            except (asyncio.CancelledError, Exception):
                pass

        with patch("discord.Client.close", new_callable=AsyncMock):
            await bot.close()

        assert (
            server.should_exit is True
        ), "bot.close() must set server.should_exit = True to signal uvicorn shutdown"

    @pytest.mark.asyncio
    async def test_close_awaits_sidecar_task(
        self,
        mock_health_server: AsyncMock,
    ) -> None:
        """MomBot.close() must await the sidecar task so it drains cleanly.

        The sidecar task must be done after ``close()`` returns.  This ensures
        the uvicorn server has had a chance to flush in-flight requests before
        the process exits.

        The ``uvicorn.Server.serve`` mock returns immediately (AsyncMock
        default), so once ``should_exit`` is set the task finishes on the
        next event-loop turn.
        """
        from mom_bot.main import MomBot, build_intents

        engine = _make_engine()
        session_factory = _make_session_factory(engine)
        fake_guild = _make_fake_guild()
        bot = MomBot(intents=build_intents())

        stack = await _run_setup_and_ready(bot, session_factory, fake_guild)
        stack.close()

        if bot._reminder_task is not None:
            bot._reminder_task.cancel()
            try:
                await bot._reminder_task
            except (asyncio.CancelledError, Exception):
                pass

        with patch("discord.Client.close", new_callable=AsyncMock):
            await bot.close()

        sidecar_task = bot._sidecar_task
        assert sidecar_task is not None
        assert sidecar_task.done(), (
            "sidecar_task must be done after bot.close() — the task must be "
            "awaited so uvicorn drains before the process exits"
        )

    @pytest.mark.asyncio
    async def test_close_without_sidecar_does_not_raise(
        self,
        mock_health_server: AsyncMock,
    ) -> None:
        """MomBot.close() must not raise if on_ready was never called.

        If the bot shuts down before the gateway READY event fires (e.g.
        a startup failure), ``_sidecar_task`` will be ``None``.  The close
        path must handle this gracefully.
        """
        from mom_bot.main import MomBot, build_intents

        bot = MomBot(intents=build_intents())

        with (
            patch("mom_bot.main.load_secret", side_effect=_fake_load_secret),
            patch("mom_bot.reminders.seed.load_secret", side_effect=_fake_load_secret),
            patch.object(bot.tree, "sync", new_callable=AsyncMock),
        ):
            await bot.setup_hook()

        # Cancel reminder task to avoid leaks.
        if bot._reminder_task is not None:
            bot._reminder_task.cancel()
            try:
                await bot._reminder_task
            except (asyncio.CancelledError, Exception):
                pass

        # _sidecar_task is None at this point — close must handle it silently.
        with patch("discord.Client.close", new_callable=AsyncMock):
            await bot.close()  # Must not raise.


# ---------------------------------------------------------------------------
# Test class: sidecar task exception logging
# ---------------------------------------------------------------------------


class TestSidecarExceptionLogging:
    """Fatal exceptions in the sidecar task are logged at CRITICAL."""

    @pytest.mark.asyncio
    async def test_sidecar_task_exception_logs_critical(
        self,
        mock_health_server: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A fatal exception in the sidecar serve coroutine must log CRITICAL.

        The task uses the same done-callback pattern as the reminder task
        (issue #53): a ``_log_task_exception`` done-callback logs CRITICAL
        with exc_info so operators can see the failure immediately in the
        log stream.
        """
        from mom_bot.main import MomBot, build_intents

        engine = _make_engine()
        session_factory = _make_session_factory(engine)
        fake_guild = _make_fake_guild()

        async def _exploding_serve(*_args: Any, **_kwargs: Any) -> None:
            """Simulate a fatal error in the uvicorn serve coroutine."""
            raise RuntimeError("sidecar fatal serve failure")

        bot = MomBot(intents=build_intents())

        with caplog.at_level(logging.CRITICAL, logger="mom_bot.main"):
            stack = await _run_setup_and_ready(
                bot,
                session_factory,
                fake_guild,
                extra_patches=[
                    patch("uvicorn.Server.serve", side_effect=_exploding_serve),
                ],
            )
            # One more yield for the done-callback to fire.
            await asyncio.sleep(0)
            stack.close()

        critical_records = [
            r for r in caplog.records if r.levelno == logging.CRITICAL and r.name == "mom_bot.main"
        ]
        assert critical_records, (
            "Expected a CRITICAL log record from mom_bot.main when the sidecar "
            "serve coroutine raises, but none was emitted."
        )

        # Clean up the reminder task.
        if bot._reminder_task is not None:
            bot._reminder_task.cancel()
            try:
                await bot._reminder_task
            except (asyncio.CancelledError, Exception):
                pass
