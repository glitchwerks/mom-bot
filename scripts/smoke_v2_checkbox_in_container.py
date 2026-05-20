"""Phase 0 follow-up smoke — CheckboxGroup nested inside a Container.

The first smoke (``smoke_v2_checkbox.py``, commit 6f33b1c) confirmed that
Discord rejects :class:`discord.ui.CheckboxGroup` (type 22) and bare
:class:`discord.ui.Button` (type 2) placed directly at the top level of a
V2 message — HTTP 400 / error 50035 with the message:

    In data.components.1: Value of field "type" must be one of
    (1, 9, 10, 12, 13, 14, 17).

This script probes the next hypothesis: **does Discord accept
``CheckboxGroup`` when it is wrapped in a ``Container`` (type 17, which
is explicitly allowed at the top level), and bare ``Button``s wrapped in
an ``ActionRow`` (type 1, also allowed at top level)?**

The slash command ``/v2-smoke-container`` responds with a
:class:`discord.ui.LayoutView` structured as follows:

1. :class:`discord.ui.TextDisplay` — header (type 10, known-good at top).
2. :class:`discord.ui.Container` (type 17) containing:

   - :class:`discord.ui.TextDisplay` — "Pick options:" label.
   - :class:`discord.ui.CheckboxGroup` — 5 options, ``opt-2`` pre-checked.

3. :class:`discord.ui.ActionRow` (type 1) containing:

   - **Save** :class:`discord.ui.Button` — logs ``.values``, acks message.
   - **Cancel** :class:`discord.ui.Button` — logs cancel, dismisses message.

Usage::

    python scripts/smoke_v2_checkbox_in_container.py

Secrets resolution, logging format, and bot lifecycle are identical to
``smoke_v2_checkbox.py``.  The two smoke commands can coexist in the same
dev guild simultaneously because they use distinct command names.

Expected smoke outcome (Phase 0):

1. Discord accepts the message without a 400 error.
2. The ephemeral message renders with ``opt-2`` pre-checked.
3. Toggling checkboxes and pressing **Save** logs the selected values.
4. Pressing **Cancel** logs cancel intent and edits the message to
   "cancelled".

Copy-paste the INFO log output into a comment on issue #145 to satisfy
the Phase 0 paper-trail gate before opening any Phase 1 PR.
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


class SaveButton(discord.ui.Button["SmokeContainerView"]):
    """Save button — logs selected CheckboxGroup values and acks the message.

    Reads ``.values`` from the :class:`discord.ui.CheckboxGroup` stored on
    the parent view as ``checkbox_group``, emits an INFO record, then
    in-place edits the message to a plain "ack" string (V2 → non-V2 edit,
    so ``content``/``embeds``/``attachments`` are explicitly cleared).
    """

    def __init__(self) -> None:
        """Initialise SaveButton with primary style and label "Save"."""
        super().__init__(label="Save", style=ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Handle the Save button press.

        Reads ``.values`` from ``self.view.checkbox_group``, logs them at
        INFO, then edits the originating message to a plain acknowledgement
        string.

        Args:
            interaction: The button-press interaction from Discord.
        """
        selected: list[str] = []
        if self.view is not None:
            selected = list(self.view.checkbox_group.values)

        _logger.info("smoke save: selected=%r", selected)

        await interaction.response.edit_message(
            view=None,
            content="ack",
            embeds=[],
            attachments=[],
        )


class CancelButton(discord.ui.Button["SmokeContainerView"]):
    """Cancel button — logs cancel intent and dismisses the message.

    In-place edits the message to "cancelled" (V2 → non-V2 edit, so
    ``content``/``embeds``/``attachments`` are explicitly cleared).
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


class SmokeContainerView(discord.ui.LayoutView):
    """LayoutView for the ``/v2-smoke-container`` command.

    Structure (in ``add_item`` order):

    1. :class:`discord.ui.TextDisplay` — top-level header.
    2. :class:`discord.ui.Container` — type 17 (allowed at top level)
       holding:

       - :class:`discord.ui.TextDisplay` — "Pick options:" sub-label.
       - :class:`discord.ui.CheckboxGroup` — 5 sample options,
         ``opt-2`` pre-checked.

    3. :class:`discord.ui.ActionRow` — type 1 (allowed at top level)
       holding :class:`SaveButton` and :class:`CancelButton`.

    The ``checkbox_group`` attribute is exposed so the button callbacks
    can read ``.values`` without iterating the entire view tree.

    discord.py 2.7 auto-sets ``MessageFlags.components_v2`` when
    :meth:`~discord.ui.LayoutView.has_components_v2` returns ``True``
    — no explicit ``flags=`` kwarg is needed on ``send_message``.

    Attributes:
        checkbox_group: The :class:`discord.ui.CheckboxGroup` instance
            nested inside the container; held as an attribute so button
            callbacks can read ``.values`` directly.
        timeout: View expiry in seconds (300 — five minutes).
    """

    def __init__(self) -> None:
        """Construct the smoke layout with a header, container, and action row."""
        super().__init__(timeout=300)

        # -- top-level header (type 10 — known-good at top level) -----------
        self.add_item(
            discord.ui.TextDisplay(
                "Smoke: V2 CheckboxGroup nested in Container"
            )
        )

        # -- CheckboxGroup options -------------------------------------------
        options = [
            discord.CheckboxGroupOption(
                label=f"opt-{i}",
                value=str(i),
                default=(i == 2),
            )
            for i in range(5)
        ]
        self.checkbox_group = discord.ui.CheckboxGroup(
            options=options,
            min_values=0,
            max_values=5,
        )

        # -- Container (type 17) wrapping TextDisplay + CheckboxGroup --------
        container = discord.ui.Container(
            discord.ui.TextDisplay("Pick options:"),
            self.checkbox_group,
        )
        self.add_item(container)

        # -- ActionRow (type 1) wrapping Save + Cancel buttons ---------------
        action_row = discord.ui.ActionRow(
            SaveButton(),
            CancelButton(),
        )
        self.add_item(action_row)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


class SmokeBot(discord.Client):
    """Minimal discord.Client for the Phase 0 container-smoke run.

    Registers a single guild-scoped ``/v2-smoke-container`` command on
    ``setup_hook`` and responds to it with a :class:`SmokeContainerView`.

    Attributes:
        tree: The app_commands tree bound to this client.
        _guild_id: The target dev-guild snowflake resolved from Key Vault.
    """

    def __init__(self) -> None:
        """Initialise SmokeBot with minimal guild intents."""
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.tree: app_commands.CommandTree = app_commands.CommandTree(self)
        self._guild_id: int = int(load_secret("guild-id"))

    async def setup_hook(self) -> None:
        """Register and sync ``/v2-smoke-container`` to the dev guild.

        Called by discord.py after login but before the gateway connects.
        Registers the command and syncs it to the configured guild only
        (not globally) so the command appears within seconds rather than
        waiting for global propagation.

        Raises:
            discord.HTTPException: If the command sync request fails.
        """
        guild = discord.Object(id=self._guild_id)

        @self.tree.command(
            name="v2-smoke-container",
            description=(
                "Phase 0 smoke — CheckboxGroup nested in Container (type 17)"
            ),
            guild=guild,
        )
        async def v2_smoke_container(
            interaction: discord.Interaction,
        ) -> None:
            """Respond to ``/v2-smoke-container`` with SmokeContainerView.

            Args:
                interaction: The slash-command interaction from Discord.
            """
            _logger.info(
                "smoke: /v2-smoke-container invoked by %s (id=%s)",
                interaction.user,
                interaction.user.id,
            )
            view = SmokeContainerView()
            await interaction.response.send_message(
                view=view, ephemeral=True
            )

        await self.tree.sync(guild=guild)
        _logger.info(
            "Synced /v2-smoke-container to guild %d", self._guild_id
        )

    async def on_ready(self) -> None:
        """Log connection info once the gateway is ready.

        Args: none (discord.py callback — no parameters).
        """
        _logger.info(
            "Smoke bot ready: %s (id=%s) — invoke /v2-smoke-container"
            " in guild %d",
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
    :func:`mom_bot.config.load_secret`, constructs a :class:`SmokeBot`,
    and blocks until the process is interrupted (Ctrl-C / SIGINT).
    """
    token = load_secret("discord-token")
    bot = SmokeBot()
    bot.run(token)


if __name__ == "__main__":
    main()
