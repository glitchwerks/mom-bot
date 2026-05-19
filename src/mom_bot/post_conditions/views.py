"""Paginated Discord UI view for post-condition preference selection.

Provides :class:`PostConditionsView`, a ``discord.ui.View`` subclass that
renders a 3-page multi-select interface — one page per non-empty meta-category
defined in :data:`~mom_bot.post_conditions.grouping.META_GROUPS`.

Page layout (per page)::

    Page X of N — <Meta Label>         Selected: <total>

    [▼ Pick preferences (<Meta Label>) ──────────────── ]
       ☑ Only HP Champions can be used.       [role]
       ☐ Only DEF Champions can be used.      [role]
       ...

    [◀ Prev]  [Next ▶]               [Commit]  [Cancel]

Selections are accumulated in ``self.selections`` — a
``dict[meta_label, set[condition_id]]`` — across page transitions so that
navigating forward and back preserves all choices.  On Commit the dict is
flattened into a single list and submitted via a single PUT call.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import discord
import discord.ui

from mom_bot.post_conditions.grouping import group_by_meta

__all__ = ["PostConditionsView"]

_logger = logging.getLogger(__name__)

# Emojis for condition_type visual cues.
_TYPE_EMOJI: dict[str, str] = {
    "faction": "⚔️",
    "league": "\U0001f310",
    "role": "\U0001f6e1️",
    "affinity": "✨",
    "rarity": "\U0001f48e",
    "effect": "\U0001f52e",
    "other": "\U0001f4cb",
}


def _build_select(
    meta_label: str,
    conditions: list[dict[str, Any]],
    selected_ids: set[int],
) -> discord.ui.Select[Any]:
    """Build a discord.ui.Select for a single meta-group page.

    Args:
        meta_label: The meta-category label (e.g. ``"Faction & League"``).
        conditions: The PostConditionResponse dicts for this page.
        selected_ids: IDs already selected by the user for this meta-group.

    Returns:
        A configured :class:`discord.ui.Select` ready to add to a view.
    """
    options = [
        discord.SelectOption(
            label=str(cond["description"])[:100],
            value=str(cond["id"]),
            description=f"[{cond.get('condition_type', '')}]",
            emoji=_TYPE_EMOJI.get(str(cond.get("condition_type", "")), None),
            default=(int(cond["id"]) in selected_ids),
        )
        for cond in conditions
    ]
    # Guard against an empty options list — a Select with 0 options is
    # non-functional and discord.py raises if max_values < 1.  Callers
    # (group_by_meta) already filter empty groups, but we defend here too.
    if not options:
        raise ValueError(
            f"_build_select called with no options for meta_label={meta_label!r}. "
            "Callers must filter empty groups before building a Select."
        )
    select: discord.ui.Select[Any] = discord.ui.Select(
        placeholder=f"Pick preferences ({meta_label})",
        min_values=0,
        max_values=len(options),
        options=options,
    )
    return select


class PostConditionsView(discord.ui.View):
    """Three-page paginated view for selecting post-condition preferences.

    Each page renders one meta-category's conditions in a single
    :class:`discord.ui.Select` with ``min_values=0`` and
    ``max_values=len(group)``.  Navigation buttons allow moving between
    pages; a Commit button submits the final set via PUT.

    Attributes:
        current_page: Zero-based index of the currently displayed page.
        page_count: Total number of non-empty meta-group pages.
        selections: Accumulated selections keyed by meta label.
            Values are sets of condition IDs (``int``).
    """

    def __init__(
        self,
        catalog: list[dict[str, Any]],
        initial_prefs: list[dict[str, Any]],
        discord_id: str,
        siege_client: Any,
        timeout: float = 300.0,
    ) -> None:
        """Initialise the view with catalog data and the user's current prefs.

        Args:
            catalog: All available PostConditionResponse dicts from
                ``GET /api/post-conditions``.
            initial_prefs: The user's current PostConditionResponse dicts
                from ``GET /api/members/me/preferences``.  Used to
                pre-select options on first render.
            discord_id: The invoking user's Discord snowflake as a string.
                Passed to ``siege_client.set_my_preferences`` on Commit.
            siege_client: A
                :class:`~mom_bot.post_conditions.client.SiegeWebClient`
                instance used for the Commit PUT call.
            timeout: View timeout in seconds.  Defaults to 300 (5 minutes).
        """
        super().__init__(timeout=timeout)
        self._discord_id = discord_id
        self._siege_client = siege_client

        # Build pages: list of (meta_label, [condition_dict, ...])
        self._pages = group_by_meta(catalog)
        self.page_count = len(self._pages)
        self.current_page = 0

        # Pre-populate selections from initial prefs.
        initial_ids: set[int] = {int(p["id"]) for p in initial_prefs}
        self.selections: dict[str, set[int]] = {label: set() for label, _ in self._pages}
        for label, conditions in self._pages:
            for cond in conditions:
                cid = int(cond["id"])
                if cid in initial_ids:
                    self.selections[label].add(cid)

        # Build initial UI items.
        self._rebuild_items()

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def build_header(self) -> str:
        """Build the embed-style header string for the current page.

        Returns:
            A string of the form
            ``"Page X of N — <Meta Label>\\nSelected: <total>"``.
        """
        page_label = self._pages[self.current_page][0] if self._pages else "—"
        total_selected = sum(len(s) for s in self.selections.values())
        return (
            f"Page {self.current_page + 1} of {self.page_count}"
            f" — {page_label}\nSelected: {total_selected}"
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def go_next(self, interaction: discord.Interaction) -> None:
        """Advance to the next page, preserving current page selections.

        Args:
            interaction: The Discord interaction that triggered the button.
        """
        if self.current_page < self.page_count - 1:
            self.current_page += 1
        self._rebuild_items()
        await interaction.response.edit_message(content=self.build_header(), view=self)

    async def go_prev(self, interaction: discord.Interaction) -> None:
        """Return to the previous page, preserving current page selections.

        Args:
            interaction: The Discord interaction that triggered the button.
        """
        if self.current_page > 0:
            self.current_page -= 1
        self._rebuild_items()
        await interaction.response.edit_message(content=self.build_header(), view=self)

    # ------------------------------------------------------------------
    # Commit / Cancel
    # ------------------------------------------------------------------

    async def commit(self, interaction: discord.Interaction) -> None:
        """Flatten all selections and submit a single PUT to siege-web.

        Collects all selected condition IDs across all pages, then calls
        :meth:`~mom_bot.post_conditions.client.SiegeWebClient.\
set_my_preferences`.

        Args:
            interaction: The Discord interaction that triggered the button.
        """
        all_ids: list[int] = sorted({cid for ids in self.selections.values() for cid in ids})
        try:
            await self._siege_client.set_my_preferences(discord_id=self._discord_id, ids=all_ids)
            n = len(all_ids)
            await interaction.response.send_message(
                f"Saved — {n} preference{'s' if n != 1 else ''} set.",
                ephemeral=True,
            )
        except Exception:
            _logger.exception(
                "Failed to save post-condition preferences for discord_id=%s",
                self._discord_id,
            )
            await interaction.response.send_message(
                "Something went wrong saving your preferences. " "Please try again in a moment.",
                ephemeral=True,
            )
        self.stop()

    async def cancel(self, interaction: discord.Interaction) -> None:
        """Dismiss the view without saving.

        Args:
            interaction: The Discord interaction that triggered the button.
        """
        await interaction.response.send_message(
            "Cancelled — no changes were saved.", ephemeral=True
        )
        self.stop()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rebuild_items(self) -> None:
        """Clear all UI items and rebuild them for the current page.

        Called during construction and after every page navigation to
        recreate the Select and navigation buttons.
        """
        self.clear_items()
        if not self._pages:
            return

        meta_label, conditions = self._pages[self.current_page]
        selected_ids = self.selections.get(meta_label, set())

        # --- Select ---
        select = _build_select(meta_label, conditions, selected_ids)

        # Capture label in closure for the callback.
        _label = meta_label

        async def _on_select(
            sel_interaction: discord.Interaction,
        ) -> None:
            # interaction.data is typed as a union; cast to dict[str, Any]
            # so mypy accepts the .get() call without a union-attr error.
            data = cast(dict[str, Any], sel_interaction.data or {})
            values: list[str] = list(data.get("values", []))
            self.selections[_label] = {int(v) for v in values}
            await sel_interaction.response.defer()

        select.callback = _on_select  # type: ignore[assignment]
        self.add_item(select)

        # --- Prev button ---
        prev_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="◄ Prev",
            style=discord.ButtonStyle.secondary,
            disabled=(self.current_page == 0),
            row=1,
        )

        async def _prev(btn_interaction: discord.Interaction) -> None:
            await self.go_prev(btn_interaction)

        prev_btn.callback = _prev  # type: ignore[assignment]
        self.add_item(prev_btn)

        # --- Next button ---
        next_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Next ►",
            style=discord.ButtonStyle.secondary,
            disabled=(self.current_page >= self.page_count - 1),
            row=1,
        )

        async def _next(btn_interaction: discord.Interaction) -> None:
            await self.go_next(btn_interaction)

        next_btn.callback = _next  # type: ignore[assignment]
        self.add_item(next_btn)

        # --- Commit button ---
        commit_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Commit",
            style=discord.ButtonStyle.success,
            row=1,
        )

        async def _commit(btn_interaction: discord.Interaction) -> None:
            await self.commit(btn_interaction)

        commit_btn.callback = _commit  # type: ignore[assignment]
        self.add_item(commit_btn)

        # --- Cancel button ---
        cancel_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            row=1,
        )

        async def _cancel(btn_interaction: discord.Interaction) -> None:
            await self.cancel(btn_interaction)

        cancel_btn.callback = _cancel  # type: ignore[assignment]
        self.add_item(cancel_btn)
