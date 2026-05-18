"""mom-bot entry point — boot discord.py client and register /ping.

Reads ``MOM_BOT_ENV`` (``dev`` or ``prod``) and resolves the bot token and
guild ID from Azure Key Vault via :mod:`mom_bot.config`.  Registers a single
guild-scoped ``/ping`` slash command that returns the bot version and uptime.

The :func:`make_client` factory is intentionally separated from
:func:`main` so tests can instantiate the client without invoking
:meth:`discord.Client.run` (which would attempt a live gateway connection).

Scheduler wiring (plan § 6)
----------------------------
:meth:`MomBot.setup_hook` syncs slash commands and then spawns a task that
awaits :meth:`discord.Client.wait_until_ready` before seeding reminders and
starting the scheduler loop.  Awaiting ``wait_until_ready`` directly from
``setup_hook`` would deadlock the bot (#41): discord.py calls ``setup_hook``
before ``connect()``, and the READY event (which unblocks
``wait_until_ready``) only fires after ``connect()`` runs.  The task is
stored on ``bot._reminder_task`` to prevent garbage collection.

Session factory
---------------
:func:`_build_session_factory` delegates to
:func:`mom_bot.db.build_session_factory`, which constructs a SQLAlchemy
:class:`sessionmaker` from the ``MOM_BOT_DATABASE_URL`` environment variable
(falling back to a local SQLite file ``./mom_bot.db`` for developer
convenience).  For Postgres URLs, AAD-token injection is applied
automatically.  Migrations are applied by CI before deploy (Phase 3, #91) —
not at bot startup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import discord
from aiohttp import web
from discord import app_commands

from mom_bot import __version__
from mom_bot.config import load_secret
from mom_bot.db import build_session_factory as _build_session_factory
from mom_bot.health import start_health_server
from mom_bot.post_conditions.client import SiegeWebClient
from mom_bot.post_conditions.commands import register as _register_post_conditions
from mom_bot.reminders.scheduler import ReminderScheduler
from mom_bot.reminders.seed import _maybe_seed_reminders
from mom_bot.roles.seed import seed_day_role_map

_logger = logging.getLogger(__name__)

# Recorded once at module import; used to compute uptime in /ping responses.
_started_at: float = time.monotonic()


def _log_task_exception(task: asyncio.Task[object]) -> None:
    """Done-callback safety net: log CRITICAL if a background task raised.

    Attached via :meth:`asyncio.Task.add_done_callback` to the reminder-init
    task.  Handles the cases where an exception escapes the try/except in
    :meth:`MomBot._start_reminders_after_ready` (belt-and-suspenders) and
    where other future background tasks lack a try/except wrapper.

    Args:
        task: The completed :class:`asyncio.Task` being inspected.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _logger.critical(
            "Background task %r died with exception",
            task.get_name(),
            exc_info=exc,
        )


# ---------------------------------------------------------------------------
# Session factory (patchable in tests via mock of _build_session_factory)
# ---------------------------------------------------------------------------

_DEFAULT_DB_URL = "sqlite:///./mom-bot.db"


def _resolve_db_url() -> str:
    """Return the configured database URL.

    Reads ``MOM_BOT_DATABASE_URL`` from the environment; falls back to a
    local SQLite file ``./mom-bot.db`` when the variable is absent (developer
    convenience).

    Returns:
        The database URL string to pass to ``build_session_factory``.
    """
    return os.environ.get("MOM_BOT_DATABASE_URL", _DEFAULT_DB_URL)


class MomBot(discord.Client):
    """mom-bot Discord client.

    Subclasses :class:`discord.Client` to own the slash-command tree and
    perform guild-scoped command sync inside :meth:`setup_hook`.

    Attributes:
        tree: The slash-command tree bound to this client instance.
        guild: The target guild as a :class:`discord.Object`; populated by
            :meth:`setup_hook` after ``guild-id`` is resolved from Key Vault.
        _reminder_task: The asyncio task running the reminder scheduler;
            stored to prevent garbage collection.
        _health_runner: The aiohttp AppRunner for the /healthz server;
            stored so :meth:`close` can shut it down cleanly.
    """

    def __init__(self, intents: discord.Intents) -> None:
        """Initialise the client with the provided intents.

        Args:
            intents: The :class:`discord.Intents` flag set to request from the
                gateway.  Use :func:`build_intents` to build the locked spec.
        """
        super().__init__(intents=intents)
        self.tree: app_commands.CommandTree = app_commands.CommandTree(self)
        self.guild: discord.Object | None = None
        self._reminder_task: asyncio.Task[None] | None = None
        self._health_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        """Sync slash commands and spawn the post-READY init task.

        Called by discord.py before the gateway connects (between login and
        ``connect``).  Must return promptly so the gateway can connect and
        the READY event can fire — awaiting
        :meth:`~discord.Client.wait_until_ready` directly here would
        deadlock the bot (see #41).

        The reminder-init logic that requires a connected gateway is spawned
        as :meth:`_start_reminders_after_ready`, which awaits READY itself.

        Raises:
            mom_bot.config.ConfigError: If ``guild-id`` is absent from Key
                Vault.
            ValueError: If the stored guild-id cannot be cast to ``int``.
            discord.HTTPException: If the slash-command sync request fails.
        """
        # Start the /healthz HTTP server before gateway connect so the ACA
        # liveness probe can reach it as soon as the container is up.
        runner, _site = await start_health_server()
        self._health_runner = runner
        _logger.info("Health server started on 0.0.0.0:8080")

        guild_id = int(load_secret("guild-id"))
        self.guild = discord.Object(id=guild_id)
        self.tree.copy_global_to(guild=self.guild)
        await self.tree.sync(guild=self.guild)
        _logger.info("Synced slash commands to guild %s", guild_id)

        # Reminder init must happen AFTER gateway READY but cannot be
        # awaited here (would deadlock — see #41).  Spawn as a task and
        # return so discord.py can proceed to connect().
        self._reminder_task = asyncio.create_task(
            self._start_reminders_after_ready(),
            name="reminder-init",
        )
        # Belt-and-suspenders: BOTH this done-callback AND the try/except
        # inside _start_reminders_after_ready will fire on a real exception,
        # producing TWO CRITICAL log records for the same failure.  This is
        # intentional — operator-observability redundancy (#53).  The inner
        # try/except logs at the moment of failure (rich live-stream signal);
        # this callback is a safety net for exception paths the try/except
        # might miss in future code (e.g. if a nested except swallows the
        # re-raise, or a BaseException subclass bypasses ``except Exception``).
        # A maintainer who thinks the double-logging is a bug should consult
        # issue #53 and run test_real_exception_logs_twice_belt_and_suspenders
        # to confirm the behavior is intentionally locked.
        self._reminder_task.add_done_callback(_log_task_exception)

    async def _start_reminders_after_ready(self) -> None:
        """Wait for gateway READY, then seed and run the scheduler loop.

        Spawned as a background task by :meth:`setup_hook`.  Lives for the
        bot's lifetime — the trailing ``await scheduler.run()`` is the main
        scheduler loop, not just an init step.

        The seed step reads ``guild-id`` and ``reminder-channel-name`` from
        Key Vault and resolves the channel name to a snowflake by calling
        ``bot.get_guild(int(guild_id)).text_channels`` (#47, #49).  Guild
        selection is deterministic — the ``guild-id`` KV secret picks the
        right guild even when the bot account is a member of multiple guilds
        simultaneously.  Resolution happens once on first boot; the snowflake
        is stored in the DB.

        Any unexpected exception is logged at CRITICAL with full traceback
        and re-raised so the task ends in an exceptional state — important
        for shutdown-hook awaiters and for the done-callback safety net
        added in #53.

        Raises:
            asyncio.CancelledError: Propagated without logging (normal
                shutdown signal, not an error).
            mom_bot.config.ConfigError: If a required KV secret is absent,
                the bot has no guilds, or the named channel is not found.
                Logged at CRITICAL before re-raising.
            Exception: Any other unexpected exception is logged at CRITICAL
                before re-raising.
        """
        try:
            await self.wait_until_ready()

            factory = _build_session_factory(_resolve_db_url())

            # Seed the reminder table on first boot from Key Vault (plan § 4).
            # Pass self so seed.py can resolve the channel name → snowflake.
            with factory() as session:
                _maybe_seed_reminders(session, self)

            # Run the scheduler loop for the bot's lifetime (plan § 6).
            scheduler = ReminderScheduler(self, factory)
            _logger.info("Reminder scheduler started")
            await scheduler.run()
        except asyncio.CancelledError:
            raise  # shutdown signal — not an error, do not log
        except Exception:
            # Also re-raised so add_done_callback's _log_task_exception sees
            # the exception.  Two CRITICAL records will appear in logs — see
            # issue #53 and test_real_exception_logs_twice_belt_and_suspenders
            # for rationale.
            _logger.critical(
                "Reminder init task failed; scheduler did not start",
                exc_info=True,
            )
            raise

    async def on_ready(self) -> None:
        """Log connection details and seed day-role map on gateway READY.

        Emits a structured INFO record with the bot user, guild count, member
        count, and raw intent bitfield value so operators can verify the
        gateway session at a glance.

        Also seeds (or refreshes) the ``day_role_map`` table via
        :func:`~mom_bot.roles.seed.seed_day_role_map`.  The seed is wrapped in
        a broad ``try/except`` so a transient failure (e.g. KV unavailable,
        Discord API blip) does not bring down the bot — Discord can call
        ``on_ready`` multiple times across reconnects, and missing one seed
        cycle is preferable to crashing.
        """
        member_count = sum(g.member_count or 0 for g in self.guilds)
        _logger.info(
            "Connected as %s (id=%s); guilds=%d members=%d intents=%s",
            self.user,
            self.user.id if self.user else None,
            len(self.guilds),
            member_count,
            self.intents.value,
        )

        # Seed day-role map — best-effort; failure is logged, not raised.
        try:
            factory = _build_session_factory(_resolve_db_url())
            await seed_day_role_map(self, factory)
        except Exception:
            _logger.exception(
                "day_role_map seed failed; bot continues without updated "
                "role map — will retry on next on_ready"
            )

    async def close(self) -> None:
        """Shut down the health server then close the gateway connection.

        Overrides :meth:`discord.Client.close` to ensure the aiohttp runner
        started in :meth:`setup_hook` is cleaned up before the process exits.
        Cleanup is best-effort: a failure here is logged but not re-raised so
        the gateway close still proceeds.
        """
        if self._health_runner is not None:
            try:
                await self._health_runner.cleanup()
                _logger.info("Health server shut down cleanly")
            except Exception:
                _logger.warning("Health server shutdown encountered an error", exc_info=True)
        await super().close()


def build_intents() -> discord.Intents:
    """Build the locked-spec intents flag set.

    Enables exactly the three intents agreed in Epic 0 session decisions:
    ``GUILDS``, ``GUILD_MEMBERS``, and ``GUILD_SCHEDULED_EVENTS``.  All
    other privileged intents (``MESSAGE_CONTENT``, ``GUILD_PRESENCES``)
    are intentionally left off.

    Returns:
        A :class:`discord.Intents` instance with the locked flags set.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.guild_scheduled_events = True
    return intents


def configure_logging() -> None:
    """Configure root logging to stdout with a structured format.

    Sets the root logger level to INFO and applies a plain-text format
    that is grep-friendly and JSON-ingestible once App Insights export
    is wired in Epic 1.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def make_client(
    siege_client: SiegeWebClient | None = None,
) -> MomBot:
    """Construct the configured client without running it.

    Registers the ``/ping`` command and the three post-condition commands on
    the client's command tree.  The client is not connected and
    :meth:`~MomBot.setup_hook` is not called until
    :meth:`discord.Client.run` (or :meth:`~discord.Client.start`) is
    invoked.

    Args:
        siege_client: A pre-constructed :class:`~mom_bot.post_conditions.\
client.SiegeWebClient` to use for the post-condition commands.  When
            ``None`` (the default for production) the client is built
            here by calling ``load_secret`` to resolve the siege-web URL
            and bot token from Azure Key Vault.  Pass an explicit instance
            in tests to avoid Key Vault round-trips.

    Returns:
        A fully-configured :class:`MomBot` instance ready to be started.
    """
    client = MomBot(intents=build_intents())

    @client.tree.command(
        name="ping",
        description="Health check — replies with pong + bot version + uptime",
    )
    async def ping(interaction: discord.Interaction) -> None:  # noqa: RUF006
        """Respond to /ping with the bot version and process uptime.

        Args:
            interaction: The slash-command interaction from Discord.
        """
        uptime_seconds = int(time.monotonic() - _started_at)
        await interaction.response.send_message(
            f"pong! version={__version__} uptime={uptime_seconds}s",
            ephemeral=True,
        )

    # Register post-condition slash commands.  The SiegeWebClient is
    # constructed once at boot so the token is resolved from Key Vault once
    # and reused across all command invocations.
    if siege_client is None:
        siege_client = SiegeWebClient(
            base_url=load_secret("siege-web-url"),
            token=load_secret("siege-web-bot-token"),
        )
    _register_post_conditions(tree=client.tree, siege_client=siege_client)

    return client


def main() -> None:
    """Entry point — boot the client and run until disconnect.

    Loads the Discord bot token from Key Vault, constructs the client via
    :func:`make_client`, and blocks until the gateway connection closes.
    """
    configure_logging()
    token = load_secret("discord-token")
    client = make_client()
    client.run(token)


if __name__ == "__main__":
    main()
