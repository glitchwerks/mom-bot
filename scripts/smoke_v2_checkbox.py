"""Phase 0 smoke script — manually verify Components V2 / CheckboxGroup on a dev guild.

Logs into the bot, registers a guild-scoped ``/v2-smoke`` slash command on the
configured dev guild, and responds to it with a :class:`discord.ui.LayoutView`
containing:

- a :class:`discord.ui.TextDisplay` header
- a :class:`discord.ui.CheckboxGroup` with 5 sample options (option 2 pre-checked)
- a **Save** button (logs selected values, then edits the message to "ack")
- a **Cancel** button (logs cancel intent, then edits the message to "cancelled")

Usage::

    python scripts/smoke_v2_checkbox.py

The script resolves the Discord token and guild ID from Azure Key Vault via the
same :func:`mom_bot.config.load_secret` helper the production bot uses.  Ensure
``MOM_BOT_ENV`` (default: ``"dev"``) and ``MOM_BOT_KEY_VAULT_NAME`` are set and
that your ``az login`` credential has *Key Vault Secrets User* on the vault.

Expected smoke outcome (per Phase 0 checklist, plan § 5):

1. The ephemeral message renders with a CheckboxGroup — ``opt-2`` pre-checked.
2. Toggling checkboxes and pressing **Save** logs the selected values.
3. Pressing **Cancel** logs the cancel intent and edits the message to "cancelled".
4. Discord returns no 400 error on any of the above interactions.

Copy-paste the INFO log output into a comment on issue #145 to satisfy the
Phase 0 paper-trail gate before opening any Phase 1 PR.
"""

from __future__ import annotations

import logging
import pathlib

import discord
from discord import ButtonStyle, app_commands

import mom_bot
from mom_bot.config import load_secret

# ---------------------------------------------------------------------------
# Tripwire — guard against running with the wrong .venv's Python.
#
# If the resolved mom_bot package is NOT inside this script's repo tree, the
# caller is using a different checkout's interpreter (e.g. the parent
# worktree's .venv/Scripts/python.exe).  Raise loudly rather than silently
# smoke-testing the wrong source.
# ---------------------------------------------------------------------------

_SCRIPT_PATH = pathlib.Path(__file__).resolve()
_MOM_BOT_PATH = pathlib.Path(mom_bot.__file__).resolve()
_REPO_ROOT = _SCRIPT_PATH.parent.parent  # scripts/ -> repo root

if _REPO_ROOT not in _MOM_BOT_PATH.parents:
    raise RuntimeError(
        f"mom_bot shadow detected: script lives under {_REPO_ROOT}, "
        f"but the active 'mom_bot' package loaded from {_MOM_BOT_PATH}. "
        f"You're probably running the wrong .venv's Python. "
        f"Use {_REPO_ROOT / '.venv' / 'Scripts' / 'python.exe'}."
    )

# ---------------------------------------------------------------------------
# Logging — INFO so smoke output is copy-pasteable into the issue comment.
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
_logger = logging.getLogger(__name__)
_logger.info("mom_bot loaded from: %s", _MOM_BOT_PATH)
_logger.info("script running from: %s", _SCRIPT_PATH)

# ---------------------------------------------------------------------------
# UI components
# ---------------------------------------------------------------------------


class SaveButton(discord.ui.Button["LayoutView"]):
    """Save button — logs selected CheckboxGroup values and acks the message.

    Reads ``.values`` from the :class:`discord.ui.CheckboxGroup` sibling in
    the view, emits an INFO record, then in-place edits the message to a plain
    "ack" string (V2 → non-V2 edit, so content/embeds/attachments are
    explicitly cleared per plan § 2.3).
    """

    def __init__(self) -> None:
        """Initialise SaveButton with primary style and label "Save"."""
        super().__init__(label="Save", style=ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Handle the Save button press.

        Collects ``.values`` from every :class:`discord.ui.CheckboxGroup`
        child in the parent view, logs them at INFO, then edits the originating
        message to a plain acknowledgement string.

        Args:
            interaction: The button-press interaction from Discord.
        """
        selected: list[str] = []
        if self.view is not None:
            for child in self.view.children:
                if isinstance(child, discord.ui.CheckboxGroup):
                    selected.extend(child.values)

        _logger.info("smoke save: selected=%r", selected)

        await interaction.response.edit_message(
            view=None,
            content="ack",
            embeds=[],
            attachments=[],
        )


class CancelButton(discord.ui.Button["LayoutView"]):
    """Cancel button — logs cancel intent and dismisses the message.

    In-place edits the message to "cancelled" (V2 → non-V2 edit, so
    content/embeds/attachments are explicitly cleared per plan § 2.3).
    """

    def __init__(self) -> None:
        """Initialise CancelButton with secondary style and label "Cancel"."""
        super().__init__(label="Cancel", style=ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Handle the Cancel button press.

        Logs the cancel at INFO, then edits the originating message to a
        plain "cancelled" string.

        Args:
            interaction: The button-press interaction from Discord.
        """
        _logger.info("smoke cancel")

        await interaction.response.edit_message(
            view=None,
            content="cancelled",
            embeds=[],
            attachments=[],
        )


class SmokeLayoutView(discord.ui.LayoutView):
    """LayoutView for the /v2-smoke command.

    Contains (in add_item order):

    1. :class:`discord.ui.TextDisplay` — header label.
    2. :class:`discord.ui.CheckboxGroup` — 5 sample options, ``opt-2``
       pre-checked via ``default=True``.
    3. :class:`SaveButton` — logs ``.values`` and acks.
    4. :class:`CancelButton` — logs cancel and dismisses.

    discord.py 2.7 auto-sets ``MessageFlags.components_v2`` when
    :meth:`~discord.ui.LayoutView.has_components_v2` returns ``True`` — no
    explicit ``flags=`` kwarg is needed on the ``send_message`` call
    (plan § 2.2).

    Attributes:
        timeout: View expiry in seconds (300 — five minutes).
    """

    def __init__(self) -> None:
        """Construct the smoke layout with a TextDisplay, CheckboxGroup, and two buttons."""
        super().__init__(timeout=300)

        self.add_item(discord.ui.TextDisplay("Smoke: V2 CheckboxGroup"))

        options = [
            discord.CheckboxGroupOption(
                label=f"opt-{i}",
                value=str(i),
                default=(i == 2),
            )
            for i in range(5)
        ]
        self.add_item(
            discord.ui.CheckboxGroup(
                options=options,
                min_values=0,
                max_values=5,
            )
        )

        self.add_item(SaveButton())
        self.add_item(CancelButton())


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


class SmokeBot(discord.Client):
    """Minimal discord.Client for the Phase 0 smoke run.

    Registers a single guild-scoped ``/v2-smoke`` command on ``on_ready``
    and responds to it with a :class:`SmokeLayoutView`.

    Attributes:
        tree: The app_commands tree bound to this client.
        _guild_id: The target dev-guild snowflake resolved from Key Vault.
    """

    def __init__(self) -> None:
        """Initialise SmokeBot with guild + members intents."""
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.tree: app_commands.CommandTree = app_commands.CommandTree(self)
        self._guild_id: int = int(load_secret("guild-id"))

    async def setup_hook(self) -> None:
        """Register and sync the /v2-smoke command to the dev guild.

        Called by discord.py after login but before the gateway connects.
        Registers the command and syncs it to the configured guild only
        (not globally) so the command appears within seconds rather than
        waiting for global propagation.

        Raises:
            discord.HTTPException: If the command sync request fails.
        """
        guild = discord.Object(id=self._guild_id)

        @self.tree.command(
            name="v2-smoke",
            description="Phase 0 smoke test — renders a V2 CheckboxGroup ephemerally",
            guild=guild,
        )
        async def v2_smoke(interaction: discord.Interaction) -> None:
            """Respond to /v2-smoke with the SmokeLayoutView.

            Args:
                interaction: The slash-command interaction from Discord.
            """
            _logger.info(
                "smoke: /v2-smoke invoked by %s (id=%s)",
                interaction.user,
                interaction.user.id,
            )
            view = SmokeLayoutView()
            await interaction.response.send_message(view=view, ephemeral=True)

        await self.tree.sync(guild=guild)
        _logger.info("Synced /v2-smoke to guild %d", self._guild_id)

    async def on_ready(self) -> None:
        """Log connection info once the gateway is ready.

        Args: none (discord.py callback — no parameters).
        """
        _logger.info(
            "Smoke bot ready: %s (id=%s) — invoke /v2-smoke in guild %d",
            self.user,
            self.user.id if self.user else None,
            self._guild_id,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Load secrets and run the smoke bot until interrupted.

    Resolves the Discord bot token from Key Vault via
    :func:`mom_bot.config.load_secret`, constructs a :class:`SmokeBot`, and
    blocks until the process is interrupted (Ctrl-C / SIGINT).
    """
    token = load_secret("discord-token")
    bot = SmokeBot()
    bot.run(token)


if __name__ == "__main__":
    main()
