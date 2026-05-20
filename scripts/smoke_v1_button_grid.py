"""Phase 0 smoke for issue #145 V1 button-grid path.

Registers two slash commands on the dev guild:

``/v1-smoke``
    Responds ephemerally with a :class:`discord.ui.View` carrying 20 toggle
    buttons (rows 0-3) + 4 nav buttons (row 4).  Toggle callbacks flip
    :class:`~discord.ButtonStyle` between ``success`` and ``secondary`` in
    place.  Save logs the selected set and strips the buttons.

``/v1-grid-smoke-multipage``
    Regression guard for the B1 double-label sub-pagination bug.  Builds 25
    fake conditions split across two synthetic pages (page 0: opt-0..opt-19,
    page 1: opt-20..opt-24).  Exercises Prev/Next navigation, cross-page
    selection persistence, and verifies the summary embed renders the
    meta-group heading **exactly once** across both pages.

Run::

    .venv/Scripts/python.exe scripts/smoke_v1_button_grid.py

Confirms (verify manually in the dev guild):
  1. ``/v1-smoke`` renders ephemerally with 25 buttons visible.
  2. Three of the toggle buttons are pre-styled ``success`` (default-on,
     indices 2, 7, 14).
  3. Clicking a ``secondary`` button turns it ``success`` with no flicker.
  4. Clicking a ``success`` button turns it ``secondary``.
  5. Save logs the selected ids; Cancel dismisses the message.
  6. Discord returns no 400 across at least 10 toggle clicks.
  7. ``/v1-grid-smoke-multipage`` renders 24 components (20 toggle + 4 nav).
     Prev is disabled on page 0, Next is enabled.
  8. Toggle three on page 0 (e.g. opt-2, opt-7, opt-14). Embed shows three
     selected under the ``Faction & League`` heading.
  9. Click Next. Page 1 renders with 9 components (5 toggle + 4 nav). Next
     is disabled, Prev is enabled. Embed still shows the three from page 0.
  10. Toggle opt-22 and opt-24 on page 1. Embed now shows five selected, with
      the meta-group heading appearing **only once** (B1 regression guard).
  11. Click Prev. Page 0 re-renders. opt-2, opt-7, opt-14 still show
      ``success`` style. Embed unchanged.
"""

from __future__ import annotations

import dataclasses
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
# Shared dataclass for multipage smoke
# ---------------------------------------------------------------------------

_DEFAULT_ON: frozenset[int] = frozenset({2, 7, 14})


@dataclasses.dataclass(frozen=True)
class _FakeCondition:
    """Minimal stand-in for a PostConditionResponse dict.

    Phase 0 must be standalone so the smoke runs even if ``views.py`` is
    broken mid-implementation.  This dataclass carries only the fields the
    button-grid smoke needs: ``id``, ``label``, ``condition_type``, and
    ``meta_label``.

    Attributes:
        id: Numeric identifier for the condition (matches toggle
            ``custom_id`` suffix).
        label: Human-readable button label shown in Discord.
        condition_type: Condition category string (e.g. ``"faction"``).
        meta_label: The meta-group heading rendered in the summary embed.
    """

    id: int
    label: str
    condition_type: str
    meta_label: str


@dataclasses.dataclass
class _GridPage:
    """A single page of conditions for the multipage smoke view.

    Intentionally mirrors the shape of ``GridPage`` from ``grid_layout.py``
    (which is created in Phase 1) without importing from ``mom_bot`` beyond
    what the tripwire already validated.

    Attributes:
        label: Human-readable page title shown in the embed title.
        conditions: Ordered list of :class:`_FakeCondition` for this page.
    """

    label: str
    conditions: list[_FakeCondition]


# ---------------------------------------------------------------------------
# /v1-smoke — single-page toggle grid
# ---------------------------------------------------------------------------


class _SinglePageToggleButton(discord.ui.Button["SinglePageSmokeView"]):
    """Toggle button for the single-page smoke view.

    Flips its :class:`~discord.ButtonStyle` between ``success`` (ON) and
    ``secondary`` (OFF) on each click, then refreshes the message in-place
    so the user sees the new visual state immediately.

    Attributes:
        _opt_index: The zero-based index identifying this toggle (matches the
            ``opt-N`` label and the ``toggle-N`` ``custom_id`` suffix).
    """

    def __init__(
        self,
        *,
        opt_index: int,
        on: bool,
    ) -> None:
        """Initialise a toggle button for the given option index.

        Args:
            opt_index: Zero-based option index; drives ``label``,
                ``custom_id``, and initial ``row`` placement.
            on: Whether this button starts in the ON (``success``) state.
        """
        super().__init__(
            style=ButtonStyle.success if on else ButtonStyle.secondary,
            label=f"opt-{opt_index}",
            row=opt_index // 5,
            custom_id=f"toggle-{opt_index}",
        )
        self._opt_index: int = opt_index

    async def callback(self, interaction: discord.Interaction) -> None:
        """Flip the button style and refresh the message.

        Toggles between :attr:`~discord.ButtonStyle.success` and
        :attr:`~discord.ButtonStyle.secondary`, then calls
        :meth:`~discord.InteractionResponse.edit_message` with the updated
        view so Discord re-renders the button grid.

        Args:
            interaction: The button-press interaction from Discord.
        """
        if self.style == ButtonStyle.success:
            self.style = ButtonStyle.secondary
        else:
            self.style = ButtonStyle.success

        await interaction.response.edit_message(view=self.view)


class _SinglePageSaveButton(discord.ui.Button["SinglePageSmokeView"]):
    """Save button for the single-page smoke view.

    Reads the current style of every toggle button in the view, logs the
    indices where the style is ``success`` (ON), then strips the view from
    the message by editing with ``view=None``.
    """

    def __init__(self) -> None:
        """Initialise the Save button with primary style."""
        super().__init__(
            style=ButtonStyle.primary,
            label="Save",
            row=4,
            custom_id="single-save",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Log selected indices and acknowledge the message.

        Iterates the view's children, collects indices where the toggle style
        is ``success``, logs them at INFO, then edits the message to "ack"
        with no view.

        Args:
            interaction: The button-press interaction from Discord.
        """
        view = self.view
        selected: list[int] = []
        if view is not None:
            for child in view.children:
                if (
                    isinstance(child, _SinglePageToggleButton)
                    and child.style == ButtonStyle.success
                ):
                    selected.append(child._opt_index)

        _logger.info("v1-smoke save: selected=%r", selected)
        await interaction.response.edit_message(content="ack", view=None)


class _SinglePageCancelButton(discord.ui.Button["SinglePageSmokeView"]):
    """Cancel button for the single-page smoke view.

    Logs the cancel at INFO, then edits the message to "cancelled" with no
    view, stripping the button grid from the ephemeral message.
    """

    def __init__(self) -> None:
        """Initialise the Cancel button with danger style."""
        super().__init__(
            style=ButtonStyle.danger,
            label="Cancel",
            row=4,
            custom_id="single-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Log cancel intent and dismiss the message.

        Args:
            interaction: The button-press interaction from Discord.
        """
        _logger.info("v1-smoke cancel")
        await interaction.response.edit_message(
            content="cancelled", view=None
        )


class SinglePageSmokeView(discord.ui.View):
    """Legacy :class:`discord.ui.View` for the ``/v1-smoke`` command.

    Layout:
    - Rows 0-3: 20 toggle buttons (5 per row), indices 0-19.
    - Row 4: ``[Prev (disabled)] [Save] [Cancel] [Next]`` nav strip.

    Buttons at indices ``{2, 7, 14}`` start in ``success`` (ON) style to
    exercise the pre-checked state confirmed in smoke item 2.

    Attributes:
        timeout: View expiry in seconds (300 — five minutes).
    """

    def __init__(self) -> None:
        """Construct the view, add 20 toggle buttons and 4 nav buttons."""
        super().__init__(timeout=300)

        for i in range(20):
            self.add_item(
                _SinglePageToggleButton(
                    opt_index=i,
                    on=(i in _DEFAULT_ON),
                )
            )

        # Nav row — Prev starts disabled (no previous page in single-page
        # smoke); Next has no action here but occupies the slot to confirm
        # the 25-component cap is respected with a full nav row.
        self.add_item(
            discord.ui.Button(
                label="Prev",
                style=ButtonStyle.secondary,
                row=4,
                disabled=True,
                custom_id="single-prev",
            )
        )
        self.add_item(_SinglePageSaveButton())
        self.add_item(_SinglePageCancelButton())
        self.add_item(
            discord.ui.Button(
                label="Next",
                style=ButtonStyle.secondary,
                row=4,
                custom_id="single-next",
            )
        )


# ---------------------------------------------------------------------------
# /v1-grid-smoke-multipage — two-page selection-persistence smoke
# ---------------------------------------------------------------------------

_FAKE_CONDITIONS: list[_FakeCondition] = [
    _FakeCondition(
        id=i,
        label=f"opt-{i}",
        condition_type="faction",
        meta_label="Faction & League",
    )
    for i in range(25)
]

_FAKE_PAGES: list[_GridPage] = [
    _GridPage(
        label="Faction & League",
        conditions=_FAKE_CONDITIONS[:20],
    ),
    _GridPage(
        label="Faction & League",
        conditions=_FAKE_CONDITIONS[20:],
    ),
]


def _build_summary_embed(
    pages: list[_GridPage],
    selections: dict[int, bool],
) -> discord.Embed:
    """Build a summary embed listing all staged selections.

    Groups selected condition ids under their ``meta_label`` heading.  The
    heading is rendered **exactly once** per meta-group, even when conditions
    for that group span multiple pages — this is the B1 regression guard.

    Args:
        pages: The full list of pages (both current and off-screen).
        selections: Mapping of condition id → checked state.

    Returns:
        A :class:`discord.Embed` whose description lists selected ids under
        a single meta-group heading, or ``"(none)"`` if nothing is selected.
    """
    # Collect all conditions across all pages in one pass, then bucket by
    # meta_label.  Because we deduplicate by meta_label (not by page),
    # multi-page meta-groups produce a single heading regardless of how many
    # pages they span.
    by_meta: dict[str, list[str]] = {}
    for page in pages:
        for cond in page.conditions:
            if not selections.get(cond.id, False):
                continue
            bucket = by_meta.setdefault(cond.meta_label, [])
            bucket.append(cond.label)

    if not by_meta:
        description = "(none selected)"
    else:
        lines: list[str] = []
        for meta_label, labels in by_meta.items():
            lines.append(f"**{meta_label}**")
            lines.append(", ".join(labels))
        description = "\n".join(lines)

    return discord.Embed(
        title="Staged selections",
        description=description,
        colour=discord.Colour.blurple(),
    )


class _MultiPageToggleButton(discord.ui.Button["MultiPageSmokeView"]):
    """Toggle button for the multipage smoke view.

    Flips the ``_selections`` dict on the parent view, triggers a full
    component rebuild via :meth:`MultiPageSmokeView._render`, and edits the
    message with both the updated view and the rebuilt summary embed.

    Attributes:
        _condition_id: The condition id this button represents; used to key
            into :attr:`MultiPageSmokeView._selections`.
    """

    def __init__(
        self,
        *,
        condition: _FakeCondition,
        row: int,
        on: bool,
    ) -> None:
        """Initialise a toggle button for the given fake condition.

        Args:
            condition: The :class:`_FakeCondition` this button represents.
            row: The Discord row (0-3) to place this button in.
            on: Whether this button starts in the ON (``success``) state.
        """
        super().__init__(
            style=ButtonStyle.success if on else ButtonStyle.secondary,
            label=condition.label,
            row=row,
            custom_id=f"mp-toggle-{condition.id}",
        )
        self._condition_id: int = condition.id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Flip selection state, rebuild components, and edit the message.

        Args:
            interaction: The button-press interaction from Discord.
        """
        view = self.view
        assert view is not None, "view must be set by discord.py before callback"
        view._selections[self._condition_id] = not view._selections.get(
            self._condition_id, False
        )
        view._render()
        embed = _build_summary_embed(_FAKE_PAGES, view._selections)
        await interaction.response.edit_message(embed=embed, view=view)


class _MultiPageNavButton(discord.ui.Button["MultiPageSmokeView"]):
    """Prev / Next page navigation for the multipage smoke view.

    Adjusting ``_page_index`` on the parent view, re-renders the components
    for the new page, and edits the message.  Selections persist across page
    changes — the ``_selections`` dict on the view is not cleared.

    Attributes:
        _direction: Either ``"prev"`` or ``"next"`` — controls which
            direction the page index moves.
    """

    def __init__(self, *, direction: str, disabled: bool) -> None:
        """Initialise a nav button.

        Args:
            direction: ``"prev"`` or ``"next"``.
            disabled: Whether the button starts in the disabled state (True
                for Prev on page 0, True for Next on the last page).
        """
        assert direction in ("prev", "next"), (
            f"direction must be 'prev' or 'next', got {direction!r}"
        )
        super().__init__(
            style=ButtonStyle.secondary,
            label="Prev" if direction == "prev" else "Next",
            row=4,
            disabled=disabled,
            custom_id=f"mp-nav-{direction}",
        )
        self._direction: str = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        """Change the page index, rebuild components, and edit the message.

        Args:
            interaction: The button-press interaction from Discord.
        """
        view = self.view
        assert view is not None, "view must be set by discord.py before callback"
        if self._direction == "prev" and view._page_index > 0:
            view._page_index -= 1
        elif (
            self._direction == "next"
            and view._page_index < len(_FAKE_PAGES) - 1
        ):
            view._page_index += 1
        view._render()
        embed = _build_summary_embed(_FAKE_PAGES, view._selections)
        await interaction.response.edit_message(embed=embed, view=view)


class _MultiPageSaveButton(discord.ui.Button["MultiPageSmokeView"]):
    """Save button for the multipage smoke view.

    Logs all selected condition ids across both pages and strips the view.
    """

    def __init__(self) -> None:
        """Initialise the Save button with primary style."""
        super().__init__(
            style=ButtonStyle.primary,
            label="Save",
            row=4,
            custom_id="mp-save",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Log staged selections across all pages and acknowledge.

        Args:
            interaction: The button-press interaction from Discord.
        """
        view = self.view
        selected: list[int] = []
        if view is not None:
            selected = [
                cid for cid, on in view._selections.items() if on
            ]
        _logger.info("v1-grid-smoke-multipage save: selected=%r", selected)
        await interaction.response.edit_message(content="ack", view=None)


class _MultiPageCancelButton(discord.ui.Button["MultiPageSmokeView"]):
    """Cancel button for the multipage smoke view."""

    def __init__(self) -> None:
        """Initialise the Cancel button with danger style."""
        super().__init__(
            style=ButtonStyle.danger,
            label="Cancel",
            row=4,
            custom_id="mp-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Dismiss the message without saving.

        Args:
            interaction: The button-press interaction from Discord.
        """
        _logger.info("v1-grid-smoke-multipage cancel")
        await interaction.response.edit_message(
            content="cancelled", view=None
        )


class MultiPageSmokeView(discord.ui.View):
    """Legacy :class:`discord.ui.View` for ``/v1-grid-smoke-multipage``.

    Holds the full selection state across all pages in ``_selections``.
    Re-renders its component list on every toggle or nav click via
    :meth:`_render`.

    Attributes:
        _page_index: Zero-based index of the currently displayed page.
        _selections: Mapping of condition id → checked state, spanning all
            pages.  Survives page navigation; cleared only by Save or Cancel.
        timeout: View expiry in seconds (300 — five minutes).
    """

    def __init__(self) -> None:
        """Construct the view, seed selections, and render page 0."""
        super().__init__(timeout=300)
        self._page_index: int = 0
        # Seed _DEFAULT_ON indices as selected for the initial render.
        self._selections: dict[int, bool] = {
            cond.id: (cond.id in _DEFAULT_ON)
            for page in _FAKE_PAGES
            for cond in page.conditions
        }
        self._render()

    def _render(self) -> None:
        """Rebuild all view children for the current ``_page_index``.

        Clears existing items, then adds:
        - One :class:`_MultiPageToggleButton` per condition on the current
          page, arranged into rows 0-3 (5 buttons per row).
        - One :class:`_MultiPageNavButton` for Prev (disabled on page 0).
        - One :class:`_MultiPageSaveButton`.
        - One :class:`_MultiPageCancelButton`.
        - One :class:`_MultiPageNavButton` for Next (disabled on last page).
        """
        self.clear_items()
        page = _FAKE_PAGES[self._page_index]
        for idx, cond in enumerate(page.conditions):
            self.add_item(
                _MultiPageToggleButton(
                    condition=cond,
                    row=idx // 5,
                    on=self._selections.get(cond.id, False),
                )
            )

        last_page = len(_FAKE_PAGES) - 1
        self.add_item(
            _MultiPageNavButton(
                direction="prev",
                disabled=(self._page_index == 0),
            )
        )
        self.add_item(_MultiPageSaveButton())
        self.add_item(_MultiPageCancelButton())
        self.add_item(
            _MultiPageNavButton(
                direction="next",
                disabled=(self._page_index >= last_page),
            )
        )


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


class SmokeBot(discord.Client):
    """Minimal :class:`discord.Client` for the Phase 0 V1 button-grid smoke.

    Registers two guild-scoped slash commands on ``setup_hook``:

    - ``/v1-smoke`` — single-page toggle grid.
    - ``/v1-grid-smoke-multipage`` — two-page persistence and B1 regression
      guard.

    Both commands can coexist with the earlier V2 smoke commands in the same
    dev guild because they use distinct names.

    Attributes:
        tree: The :class:`~discord.app_commands.CommandTree` bound to this
            client.
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
        """Register and sync both smoke commands to the dev guild.

        Called by discord.py after login, before the gateway connects.
        Both commands are registered as guild-scoped so they appear in the
        dev guild within seconds rather than waiting for global propagation.

        Raises:
            discord.HTTPException: If the command sync request fails.
        """
        guild = discord.Object(id=self._guild_id)

        @self.tree.command(
            name="v1-smoke",
            description=(
                "Phase 0 smoke — V1 button-grid (single-page toggle grid)"
            ),
            guild=guild,
        )
        async def v1_smoke(interaction: discord.Interaction) -> None:
            """Respond to ``/v1-smoke`` with a :class:`SinglePageSmokeView`.

            Args:
                interaction: The slash-command interaction from Discord.
            """
            _logger.info(
                "smoke: /v1-smoke invoked by %s (id=%s)",
                interaction.user,
                interaction.user.id,
            )
            view = SinglePageSmokeView()
            await interaction.response.send_message(
                view=view, ephemeral=True
            )

        @self.tree.command(
            name="v1-grid-smoke-multipage",
            description=(
                "Phase 0 smoke — multipage grid (B1 regression guard)"
            ),
            guild=guild,
        )
        async def v1_grid_smoke_multipage(
            interaction: discord.Interaction,
        ) -> None:
            """Respond to ``/v1-grid-smoke-multipage``.

            Constructs a :class:`MultiPageSmokeView` seeded with default-on
            indices, builds the initial summary embed, and sends them as an
            ephemeral message.

            Args:
                interaction: The slash-command interaction from Discord.
            """
            _logger.info(
                "smoke: /v1-grid-smoke-multipage invoked by %s (id=%s)",
                interaction.user,
                interaction.user.id,
            )
            view = MultiPageSmokeView()
            embed = _build_summary_embed(_FAKE_PAGES, view._selections)
            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True
            )

        await self.tree.sync(guild=guild)
        _logger.info(
            "Synced /v1-smoke and /v1-grid-smoke-multipage to guild %d",
            self._guild_id,
        )

    async def on_ready(self) -> None:
        """Log connection info once the gateway is ready.

        Args: none (discord.py callback — no parameters).
        """
        _logger.info(
            "Smoke bot ready: %s (id=%s) — invoke /v1-smoke or"
            " /v1-grid-smoke-multipage in guild %d",
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
