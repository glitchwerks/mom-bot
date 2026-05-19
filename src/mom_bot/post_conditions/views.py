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

    ┌──────────────────────────────────────────────┐
    │ Selected preferences                         │
    │ **Faction & League**                         │
    │ ⚔️ Only Barbarian Champions.                 │
    │ **Role, Affinity, Rarity**                   │
    │ 🛡️ Only HP Champions.                        │
    └──────────────────────────────────────────────┘

Selections are accumulated in ``self.selections`` — a
``dict[meta_label, set[condition_id]]`` — across page transitions so that
navigating forward and back preserves all choices.  On Commit the dict is
flattened into a single list and submitted via a single PUT call.

The :func:`build_summary_embed` helper is exported for unit-testing in
isolation; callers outside this module should not need it directly.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import discord
import discord.ui
from discord import CheckboxGroupOption

from mom_bot.post_conditions.client import SiegeWebError
from mom_bot.post_conditions.grouping import group_by_meta
from mom_bot.post_conditions.modal_layout import ModalPage

__all__ = ["PostConditionsView", "build_summary_embed", "EditPreferencesModal"]

# Discord modal title character limit.
# Source: .venv/Lib/site-packages/discord/ui/modal.py docstring (L88).
_MODAL_TITLE_LIMIT = 45

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

# Discord embed description hard limit.
_EMBED_MAX_CHARS = 4096

# Truncation suffix template — leave enough headroom for the suffix itself.
_TRUNCATION_SUFFIX = "… and {n} more"


def _selections_to_meta_keyed(
    selections: dict[int, bool],
    pages: list[tuple[str, list[dict[str, Any]]]],
) -> dict[str, set[int]]:
    """Convert flat {id: bool} to {meta_label: {id, ...}} for build_summary_embed.

    Walks ``pages`` (the existing ``group_by_meta(...)`` output) and, for
    each ``(label, conditions)`` pair, collects the IDs that are truthy in
    ``selections`` into a set keyed by ``label``.  Labels whose collected
    set would be empty are omitted from the result entirely.

    Args:
        selections: A flat mapping from condition ID (``int``) to a boolean
            indicating whether that condition is selected.
        pages: The ``group_by_meta``-produced list of
            ``(meta_label, [condition_dict, ...])`` pairs.  Determines
            iteration order and which IDs belong to which label.

    Returns:
        A dict mapping each meta-label that has at least one selected
        condition to the set of selected condition IDs for that label.
        Returns ``{}`` when no IDs are selected or ``selections`` is empty.
    """
    result: dict[str, set[int]] = {}
    for label, conditions in pages:
        selected: set[int] = {
            int(cond["id"])
            for cond in conditions
            if selections.get(int(cond["id"]), False)
        }
        if selected:
            result[label] = selected
    return result


def build_summary_embed(
    pages: list[tuple[str, list[dict[str, Any]]]],
    selections: dict[str, set[int]],
) -> discord.Embed:
    """Build a discord.Embed summarising every currently-selected preference.

    Items are grouped by meta-label, with a bold heading per non-empty group
    and one line per selected item (type-emoji prefix + full description).
    The embed description is capped at 4 096 characters; if the rendered text
    would exceed that limit, a truncation marker is appended and surplus lines
    are omitted.

    Args:
        pages: The view's ``_pages`` list — each element is a
            ``(meta_label, [condition_dict, ...])`` pair drawn from the full
            catalog.  Determines both the iteration order and the label used
            as a heading.
        selections: The view's ``selections`` dict — maps meta-label to the
            set of selected condition IDs for that group.

    Returns:
        A :class:`discord.Embed` ready to pass to
        ``interaction.response.edit_message(embed=...)``.
    """
    embed = discord.Embed(title="Selected preferences", color=discord.Color.blurple())

    # Build a fast lookup: condition_id → (meta_label, description,
    # condition_type) to avoid O(N²) scans when rendering.
    id_to_cond: dict[int, dict[str, Any]] = {}
    for _label, conditions in pages:
        for cond in conditions:
            id_to_cond[int(cond["id"])] = cond

    # Collect lines grouped in META_GROUPS order (which is the pages order).
    lines: list[str] = []
    total_selected = sum(len(s) for s in selections.values())
    if total_selected == 0:
        embed.description = "_None selected yet._"
        return embed

    for meta_label, conditions in pages:
        selected_ids = selections.get(meta_label, set())
        if not selected_ids:
            continue

        # Build ordered list of matching conditions for this group.
        group_lines: list[str] = []
        for cond in conditions:
            cid = int(cond["id"])
            if cid in selected_ids:
                emoji = _TYPE_EMOJI.get(str(cond.get("condition_type", "")), "")
                prefix = f"{emoji} " if emoji else ""
                group_lines.append(f"{prefix}{cond['description']}")

        if not group_lines:
            continue

        lines.append(f"**{meta_label}**")
        lines.extend(group_lines)

    # Join into a single string, then enforce the 4 096-char limit.
    description = "\n".join(lines)
    if len(description) <= _EMBED_MAX_CHARS:
        embed.description = description
        return embed

    # Truncate: drop lines from the end until we fit, then add suffix.
    # We count remaining omitted items for the "… and N more" marker.
    # Because we drop whole lines (some are headings, some are items), we
    # compute how many *item* lines (non-bold) were dropped.
    kept: list[str] = []
    dropped_items = 0
    # Pre-count total item lines (non-heading).
    total_item_lines = sum(1 for ln in lines if not ln.startswith("**"))

    for ln in lines:
        tentative = kept + [ln]
        # Reserve space for the suffix.
        suffix_len = len(_TRUNCATION_SUFFIX.format(n=total_item_lines))
        if len("\n".join(tentative)) + 1 + suffix_len > _EMBED_MAX_CHARS:
            break
        kept.append(ln)

    # Count how many item lines were dropped.
    kept_items = sum(1 for ln in kept if not ln.startswith("**"))
    dropped_items = total_item_lines - kept_items

    # Remove any trailing heading that has no items under it.
    while kept and kept[-1].startswith("**"):
        kept.pop()

    suffix = _TRUNCATION_SUFFIX.format(n=dropped_items)
    embed.description = "\n".join(kept) + "\n" + suffix
    return embed


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

    A :class:`discord.Embed` is rendered alongside the view on every
    interaction (Select toggle, Prev, Next) to display the full text of
    every currently-selected preference, grouped by meta-label.  This
    works around Discord's truncation of collapsed Select chips.

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

    def build_embed(self) -> discord.Embed:
        """Build the live selection-summary embed for the current state.

        Delegates to :func:`build_summary_embed` with the view's current
        ``_pages`` catalog and ``selections`` state.

        Returns:
            A :class:`discord.Embed` listing every selected preference,
            grouped by meta-label.
        """
        return build_summary_embed(self._pages, self.selections)

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
        await interaction.response.edit_message(
            content=self.build_header(),
            embed=self.build_embed(),
            view=self,
        )

    async def go_prev(self, interaction: discord.Interaction) -> None:
        """Return to the previous page, preserving current page selections.

        Args:
            interaction: The Discord interaction that triggered the button.
        """
        if self.current_page > 0:
            self.current_page -= 1
        self._rebuild_items()
        await interaction.response.edit_message(
            content=self.build_header(),
            embed=self.build_embed(),
            view=self,
        )

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
            await sel_interaction.response.edit_message(
                content=self.build_header(),
                embed=self.build_embed(),
                view=self,
            )

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


class EditPreferencesModal(discord.ui.Modal):
    """Modal containing one CheckboxGroup for a single ModalPage sub-page.

    Displayed when the user clicks an "Edit ..." button in the
    :class:`EditPreferencesView` ephemeral message.  On submit, updates the
    parent view's flat ``selections`` dict for only the IDs in this sub-page,
    pushes the full merged preference set to siege-web, then refreshes the
    ephemeral with a re-rendered summary embed.

    If the PUT fails, the update is rolled back to the pre-submit state and an
    ephemeral error message is sent.  No exception propagates out of
    :meth:`on_submit`.

    Attributes:
        group: The :class:`discord.ui.CheckboxGroup` added to this modal.
        page: The :class:`~mom_bot.post_conditions.modal_layout.ModalPage`
            this modal covers.
        parent_view: The owning view (typed as ``Any`` at module load time;
            ``EditPreferencesView`` is not yet imported to avoid a
            forward-reference error — see Phase 3).
        pages: Full ``group_by_meta``-shaped pages list threaded through so
            ``on_submit`` can call :func:`_selections_to_meta_keyed` and
            :func:`build_summary_embed`.
        discord_id: Discord snowflake for the acting user as a string, passed to
            ``set_my_preferences``.
    """

    def __init__(
        self,
        *,
        page: ModalPage,
        parent_view: Any,
        siege_client: Any,
        discord_id: str,
        pages: list[tuple[str, list[dict[str, Any]]]],
    ) -> None:
        """Initialise the modal for one ModalPage sub-page.

        Builds a single :class:`discord.ui.CheckboxGroup` from
        ``page.conditions``, pre-checking boxes for IDs that are currently
        ``True`` in ``parent_view.selections``.

        Args:
            page: The sub-page of conditions this modal covers.  Must have
                at most 10 entries (enforced by :class:`discord.ui.CheckboxGroup`).
            parent_view: The :class:`EditPreferencesView` that owns this
                modal.  Must expose a ``selections: dict[int, bool]``
                attribute.  Typed as ``Any`` here because
                ``EditPreferencesView`` is defined in Phase 3.
            siege_client: A
                :class:`~mom_bot.post_conditions.client.SiegeWebClient`
                instance used for the PUT call on submit.
            discord_id: The invoking user's Discord snowflake as a string.
                Forwarded to ``siege_client.set_my_preferences``.
            pages: The full ``group_by_meta``-shaped
                ``list[tuple[str, list[dict[str, Any]]]]``.  Required so
                ``on_submit`` can call :func:`_selections_to_meta_keyed` and
                :func:`build_summary_embed` for the embed refresh.
        """
        title = page.label[:_MODAL_TITLE_LIMIT]
        super().__init__(title=title)

        self.page = page
        self.parent_view = parent_view
        self._siege_client = siege_client
        self.discord_id = discord_id
        self.pages = pages

        # Build CheckboxGroup options from the sub-page conditions.
        options = [
            CheckboxGroupOption(
                label=str(cond["description"]),
                value=str(cond["id"]),
                default=bool(
                    parent_view.selections.get(int(cond["id"]), False)
                ),
            )
            for cond in page.conditions
        ]

        self.group: discord.ui.CheckboxGroup[Any] = discord.ui.CheckboxGroup(
            options=options,
            min_values=0,
            max_values=len(options),
            required=False,
        )
        self.add_item(self.group)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle modal submission: update selections, PUT to siege-web, refresh embed.

        Steps:
            1. Snapshot current selections for rollback on PUT failure.
            2. Update flat selections for this sub-page's IDs only.
            3. Push the full preference set to siege-web via
               ``set_my_preferences``.  On :class:`~.client.SiegeWebError`,
               roll back selections and send an ephemeral error; then return.
            4. Re-render the ephemeral summary embed via
               :func:`build_summary_embed` and call
               ``interaction.response.edit_message``.

        Args:
            interaction: The Discord interaction for this modal submission.
        """
        # 1. Snapshot for rollback on PUT failure.
        prior = dict(self.parent_view.selections)

        # 2. Update flat selections for this sub-page only.
        submitted_ids = {int(v) for v in self.group.values}
        sub_page_ids = {int(c["id"]) for c in self.page.conditions}
        for cid in sub_page_ids:
            self.parent_view.selections[cid] = cid in submitted_ids

        # 3. Push the full preference set to siege-web.
        try:
            await self._siege_client.set_my_preferences(
                self.discord_id,
                ids=[
                    cid
                    for cid, on in self.parent_view.selections.items()
                    if on
                ],
            )
        except SiegeWebError:
            self.parent_view.selections = prior
            await interaction.response.send_message(
                "Could not save preferences — please try again.",
                ephemeral=True,
            )
            return

        # 4. Refresh the ephemeral message with the converted-shape embed.
        meta_keyed = _selections_to_meta_keyed(
            self.parent_view.selections, self.pages
        )
        embed = build_summary_embed(self.pages, meta_keyed)
        await interaction.response.edit_message(
            embed=embed, view=self.parent_view
        )
