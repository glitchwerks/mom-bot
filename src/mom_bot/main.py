"""mom-bot entry point — boot discord.py client and register /ping.

Reads ``MOM_BOT_ENV`` (``dev`` or ``prod``) and resolves the bot token and
guild ID from Azure Key Vault via :mod:`mom_bot.config`.  Registers a single
guild-scoped ``/ping`` slash command that returns the bot version and uptime.

The :func:`make_client` factory is intentionally separated from
:func:`main` so tests can instantiate the client without invoking
:meth:`discord.Client.run` (which would attempt a live gateway connection).
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands

from mom_bot import __version__
from mom_bot.config import load_secret

_logger = logging.getLogger(__name__)

# Recorded once at module import; used to compute uptime in /ping responses.
_started_at: float = time.monotonic()


class MomBot(discord.Client):
    """mom-bot Discord client.

    Subclasses :class:`discord.Client` to own the slash-command tree and
    perform guild-scoped command sync inside :meth:`setup_hook`.

    Attributes:
        tree: The slash-command tree bound to this client instance.
        guild: The target guild as a :class:`discord.Object`; populated by
            :meth:`setup_hook` after ``guild-id`` is resolved from Key Vault.
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

    async def setup_hook(self) -> None:
        """Sync guild-scoped slash commands at startup.

        Called by discord.py before :meth:`on_ready`; this is the canonical
        location for registering and syncing application commands.  Commands
        registered globally (via the tree) are copied to the target guild so
        they appear instantly — guild-scoped commands propagate in seconds,
        whereas globally-registered commands take up to one hour.

        Raises:
            mom_bot.config.ConfigError: If ``guild-id`` is absent from Key
                Vault.
            ValueError: If the stored guild-id cannot be cast to ``int``.
            discord.HTTPException: If the slash-command sync request fails.
        """
        guild_id = int(load_secret("guild-id"))
        self.guild = discord.Object(id=guild_id)
        self.tree.copy_global_to(guild=self.guild)
        await self.tree.sync(guild=self.guild)
        _logger.info("Synced slash commands to guild %s", guild_id)

    async def on_ready(self) -> None:
        """Log connection details once the client is fully connected.

        Emits a structured INFO record with the bot user, guild count, member
        count, and raw intent bitfield value so operators can verify the
        gateway session at a glance.
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


def make_client() -> MomBot:
    """Construct the configured client without running it.

    Registers the ``/ping`` command on the client's command tree.  The
    client is not connected and :meth:`~MomBot.setup_hook` is not called
    until :meth:`discord.Client.run` (or :meth:`~discord.Client.start`)
    is invoked, which means this factory is safe to call in tests without
    a network connection or Key Vault access.

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
