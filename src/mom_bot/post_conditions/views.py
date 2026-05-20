"""Discord UI views for post-condition preference selection.

Provides :class:`PostConditionsGridView` — an ephemeral button-grid view
that lets the user toggle post-condition preferences per meta-group page,
with a live-updating summary embed and a Save button that commits staged
selections in a single call.

Also retains the legacy :class:`EditPreferencesView` (modal-based flow) and
its helpers until Phase 4 wires ``commands.py`` to the new grid view and
deletes the modal infrastructure.

The :func:`build_summary_embed` helper is exported for unit-testing in
isolation; callers outside this module should not need it directly.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
import discord.ui

from mom_bot.post_conditions.client import SiegeWebError
from mom_bot.post_conditions.discord_display import short_label
from mom_bot.post_conditions.grid_layout import GridPage, split_by_meta_group
from mom_bot.post_conditions.grouping import group_by_meta
from mom_bot.post_conditions.modal_layout import ModalPage, split_meta_for_modals

__all__ = [
    "build_summary_embed",
    "EditPreferencesModal",
    "EditPreferencesView",
    "PostConditionsGridView",
    "_flat_to_meta_keyed",
]

# Discord modal title character limit.
# Source: .venv/Lib/site-packages/discord/ui/modal.py docstring (L88).
_MODAL_TITLE_LIMIT = 45

# Discord caps SelectOption.label at 100 characters.
# Source: https://docs.discord.com/developers/components/reference
_SELECT_OPTION_LABEL_LIMIT = 100

# Discord's custom_id max is 100 chars. The prefix "post_conditions_select_"
# is 23 chars, so cap the label portion at 70 to leave comfortable headroom.
_CUSTOM_ID_LABEL_LIMIT = 70

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

# Discord caps a View's component count at 25; see discord.ui.View docs.
_DISCORD_VIEW_COMPONENT_LIMIT = 25


def _selections_to_meta_keyed(
    selections: dict[int, bool],
    pages: list[tuple[str, list[dict[str, Any]]]],
) -> dict[str, set[int]]:
    """Convert flat {id: bool} to {meta_label: {id, ...}} for build_summary_embed.

    This adapter bridges two representations: EditPreferencesView's flat
    boolean dict (convenient for modal updates) and build_summary_embed's
    grouped-by-meta-label dict (convenient for embed rendering).

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
        selected: set[int] = set()
        for cond in conditions:
            cid = int(cond["id"])
            if selections.get(cid, False):
                selected.add(cid)
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


# ---------------------------------------------------------------------------
# PostConditionsGridView helpers and classes (Phase 2)
# ---------------------------------------------------------------------------


def _flat_to_meta_keyed(
    selections_flat: dict[int, bool],
    pages: list[GridPage],
) -> dict[str, set[int]]:
    """Project a flat ``{id: bool}`` dict into the meta-keyed shape.

    The :func:`build_summary_embed` function expects a
    ``{meta_label: set[int]}`` mapping; this adapter converts the flat
    boolean dict used by :class:`PostConditionsGridView` into that shape.

    Sub-paginated meta-group labels (e.g. ``"Faction & League (1/2)"``)
    are stripped of their ``" (i/N)"`` suffix before bucketing so all
    sub-pages of the same meta-group merge into one bucket.

    Args:
        selections_flat: Mapping of condition id → checked state.
        pages: :class:`GridPage` list used to recover the id → meta-label
            mapping.  Only ``id`` and ``meta_label`` fields are accessed.

    Returns:
        Mapping from (base) meta-label to the set of selected condition
        ids.  Meta-groups with no selected conditions are omitted.
    """
    id_to_meta: dict[int, str] = {}
    for page in pages:
        for cond in page.conditions:
            id_to_meta[int(cond["id"])] = page.meta_label

    out: dict[str, set[int]] = {}
    for cid, on in selections_flat.items():
        if not on:
            continue
        meta = id_to_meta.get(cid)
        if meta is None:
            continue
        out.setdefault(meta, set()).add(cid)
    return out


class PostConditionsGridView(discord.ui.View):
    """Ephemeral button-grid view for staging post-condition preferences.

    One page per META_GROUP (sub-paginated at 20 within a group). Toggle
    buttons (rows 0–3, up to 20 per page) render via
    :func:`~.discord_display.short_label`. Row 4 carries the nav row
    ``[Prev] [Save] [Cancel] [Next]``.

    State is a flat ``dict[int, bool]`` spanning all pages so selections
    survive page navigation. No network call is made until Save.

    Embed title carries the meta-group header (§ 3.11). Embed description
    carries the cross-page summary via :func:`build_summary_embed`.

    Attributes:
        _pages: Ordered :class:`GridPage` list (immutable after init).
        _page_index: 0-based index of the currently-displayed page.
        _selections: Flat ``{condition_id: bool}`` map seeded from
            ``preferences``; mutated in-place by toggle callbacks.
    """

    def __init__(
        self,
        *,
        catalog: list[dict[str, Any]],
        preferences: list[int],
        discord_id: str,
        siege_client: Any,
        timeout: float | None = 300.0,
    ) -> None:
        """Initialise the view from catalog and saved preferences.

        Args:
            catalog: Full PostConditionResponse dicts from
                ``GET /api/post-conditions``.
            preferences: The user's currently-saved condition IDs.  Used
                to seed :attr:`_selections`.
            discord_id: The invoking user's Discord snowflake as a string.
                Forwarded to the siege client on Save.
            siege_client: A
                :class:`~mom_bot.post_conditions.client.SiegeWebClient`
                used by :class:`SaveButton` on commit.
            timeout: View timeout in seconds.  Defaults to 300.
        """
        super().__init__(timeout=timeout)
        self._catalog = catalog
        self._discord_id = discord_id
        self._siege_client = siege_client
        self._pages: list[GridPage] = split_by_meta_group(catalog)
        self._page_index: int = 0

        pref_set: set[int] = set(preferences)
        self._selections: dict[int, bool] = {
            int(c["id"]): (int(c["id"]) in pref_set) for c in catalog
        }

        self._build_components()

    def _summary_pages(
        self,
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        """Collapse ``_pages`` into one ``(base_label, conditions)`` per meta-group.

        Sub-paginated meta-groups (``GridPage.meta_label`` shared across
        multiple pages) are merged so :func:`build_summary_embed` renders
        each meta-group heading exactly once — the B1 regression guard.

        Returns:
            Ordered list of ``(base_label, [condition, ...])`` tuples.
            Sub-pages sharing the same :attr:`GridPage.meta_label` are
            concatenated into a single tuple.
        """
        out: list[tuple[str, list[dict[str, Any]]]] = []
        seen: dict[str, int] = {}
        for page in self._pages:
            meta = page.meta_label
            if meta in seen:
                out[seen[meta]][1].extend(page.conditions)
            else:
                seen[meta] = len(out)
                out.append((meta, list(page.conditions)))
        return out

    def _build_embed_for_current_page(self) -> discord.Embed:
        """Canonical embed-build path used by every render entry-point.

        Builds the live-summary embed (all staged selections across all
        pages) and overrides its title with the current page's meta-group
        heading + page index (§ 3.11 of the issue #145 plan).

        All callbacks (toggle, nav, save) go through this method — there
        is no inline embed construction anywhere else (C4 fix).

        Returns:
            A :class:`discord.Embed` ready to pass to
            ``interaction.response.edit_message``.
        """
        meta_keyed = _flat_to_meta_keyed(self._selections, self._pages)
        embed = build_summary_embed(
            pages=self._summary_pages(),
            selections=meta_keyed,
        )
        if self._pages:
            current = self._pages[self._page_index]
            embed.title = (
                f"Editing — {current.label} " f"(page {self._page_index + 1}/{len(self._pages)})"
            )
        else:
            embed.title = "Preferences"
        return embed

    def _build_components(self) -> None:
        """Clear and rebuild all buttons for the active ``_page_index``.

        Called on construction, toggle, and nav.  Uses
        :func:`~.discord_display.short_label` for button labels so the
        button surface fits ~5 per row at ≤ 25 chars.
        """
        self.clear_items()
        if not self._pages:
            return

        page = self._pages[self._page_index]
        for i, cond in enumerate(page.conditions):
            cid = int(cond["id"])
            on = self._selections.get(cid, False)
            self.add_item(
                _ToggleButton(
                    condition_id=cid,
                    label=short_label(cond),
                    row=i // 5,
                    on=on,
                )
            )

        # Nav row (row 4): Prev, Save, Cancel, Next.
        self.add_item(NavButton(direction="prev", disabled=(self._page_index == 0)))
        self.add_item(SaveButton())
        self.add_item(CancelButton())
        self.add_item(
            NavButton(
                direction="next",
                disabled=(self._page_index >= len(self._pages) - 1),
            )
        )

    def initial_embed(self) -> discord.Embed:
        """Return the initial summary embed for this view.

        Thin wrapper over :meth:`_build_embed_for_current_page` — all
        render paths go through the same canonical helper.

        Returns:
            A :class:`discord.Embed` ready to pass to
            ``interaction.followup.send``.
        """
        return self._build_embed_for_current_page()


class _ToggleButton(discord.ui.Button["PostConditionsGridView"]):
    """Single condition toggle button.

    Style reflects selection state: ``success`` (green) = ON,
    ``secondary`` (grey) = OFF.

    Attributes:
        _condition_id: The catalog condition id this button controls.
    """

    def __init__(
        self,
        *,
        condition_id: int,
        label: str,
        row: int,
        on: bool,
    ) -> None:
        """Initialise the toggle button.

        Args:
            condition_id: The catalog condition id to toggle.
            label: Short display label (≤ 25 chars from
                :func:`~.discord_display.short_label`).
            row: Discord row index (0–3).
            on: Initial selection state.
        """
        super().__init__(
            style=(discord.ButtonStyle.success if on else discord.ButtonStyle.secondary),
            label=label,
            row=row,
            custom_id=f"pc-toggle-{condition_id}",
        )
        self._condition_id = condition_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Flip the condition's selection state and refresh the message.

        Args:
            interaction: The Discord interaction for this button click.
        """
        view = self.view
        assert view is not None
        view._selections[self._condition_id] = not view._selections.get(self._condition_id, False)
        view._build_components()
        embed = view._build_embed_for_current_page()
        await interaction.response.edit_message(embed=embed, view=view)


class NavButton(discord.ui.Button["PostConditionsGridView"]):
    """Prev / Next page navigation.

    Selections persist across page changes — the ``_selections`` dict on
    the parent view is unaffected by navigation.

    Attributes:
        _direction: ``"prev"`` or ``"next"``.
    """

    def __init__(self, *, direction: str, disabled: bool) -> None:
        """Initialise the navigation button.

        Args:
            direction: ``"prev"`` or ``"next"``.
            disabled: Whether the button should be non-interactive
                (e.g. Prev on the first page).

        Raises:
            AssertionError: If ``direction`` is not ``"prev"`` or ``"next"``.
        """
        assert direction in (
            "prev",
            "next",
        ), f"direction must be 'prev' or 'next', got {direction!r}"
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="◀ Prev" if direction == "prev" else "Next ▶",
            row=4,
            disabled=disabled,
            custom_id=f"pc-nav-{direction}",
        )
        self._direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        """Advance or retreat the page index and refresh the message.

        Args:
            interaction: The Discord interaction for this button click.
        """
        view = self.view
        assert view is not None
        if self._direction == "prev" and view._page_index > 0:
            view._page_index -= 1
        elif self._direction == "next" and view._page_index < len(view._pages) - 1:
            view._page_index += 1
        view._build_components()
        embed = view._build_embed_for_current_page()
        await interaction.response.edit_message(embed=embed, view=view)


class SaveButton(discord.ui.Button["PostConditionsGridView"]):
    """Commit staged selections via ``set_my_preferences``."""

    def __init__(self) -> None:
        """Initialise the Save button."""
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Save",
            row=4,
            custom_id="pc-save",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Aggregate ON selections and PUT via the siege client.

        On :class:`~.client.SiegeWebError`, logs the failure and sends a
        retry prompt without stripping the view (user can retry).

        Args:
            interaction: The Discord interaction for this button click.
        """
        view = self.view
        assert view is not None
        ids = [cid for cid, on in view._selections.items() if on]
        try:
            await view._siege_client.set_my_preferences(discord_id=view._discord_id, ids=ids)
        except SiegeWebError:
            _logger.exception(
                "set_my_preferences failed for discord_id=%s",
                view._discord_id,
            )
            await interaction.response.send_message(
                "Could not save preferences. Try again.", ephemeral=True
            )
            return
        embed = view._build_embed_for_current_page()
        embed.title = "Preferences saved"
        await interaction.response.edit_message(embed=embed, view=None)


class CancelButton(discord.ui.Button["PostConditionsGridView"]):
    """Dismiss without committing any preference changes."""

    def __init__(self) -> None:
        """Initialise the Cancel button."""
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Cancel",
            row=4,
            custom_id="pc-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Strip the view without calling the client.

        Args:
            interaction: The Discord interaction for this button click.
        """
        await interaction.response.edit_message(
            content="Cancelled — preferences unchanged.",
            embed=None,
            view=None,
        )


# ---------------------------------------------------------------------------
# Legacy modal-based view (retained until Phase 4 deletes it)
# ---------------------------------------------------------------------------


class EditPreferencesModal(discord.ui.Modal):
    """Modal containing a Label-wrapped StringSelect for a single ModalPage sub-page.

    Displayed when the user clicks an "Edit ..." button in the
    :class:`EditPreferencesView` ephemeral message.  On submit, updates the
    parent view's flat ``selections`` dict for only the IDs in this sub-page,
    pushes the full merged preference set to siege-web, then refreshes the
    ephemeral with a re-rendered summary embed.

    Uses :class:`discord.ui.Label` (type 18) as the top-level modal child,
    wrapping a multi-select :class:`discord.ui.Select` (string-select, type 3).
    ``CheckboxGroup`` (type 22) is rejected by Discord's modal endpoint at the
    top level; ``Label`` is accepted.

    If the PUT fails, the update is rolled back to the pre-submit state and an
    ephemeral error message is sent.  No exception propagates out of
    :meth:`on_submit`.

    Attributes:
        select: The :class:`discord.ui.Select` wrapped inside the modal's
            :class:`discord.ui.Label`.
        page: The :class:`~mom_bot.post_conditions.modal_layout.ModalPage`
            this modal covers.
        parent_view: The owning :class:`EditPreferencesView`.
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
        parent_view: EditPreferencesView,
        siege_client: Any,
        discord_id: str,
        pages: list[tuple[str, list[dict[str, Any]]]],
    ) -> None:
        """Initialise the modal for one ModalPage sub-page.

        Builds a :class:`discord.ui.Select` (string-select) from
        ``page.conditions``, pre-selecting options for IDs that are currently
        ``True`` in ``parent_view.selections``, then wraps it in a
        :class:`discord.ui.Label` so Discord's modal endpoint accepts the
        payload (type 18 is allowed at top level; type 22 is not).

        Option labels are truncated to 100 characters per Discord's
        ``SelectOption.label`` cap.

        Args:
            page: The sub-page of conditions this modal covers.  Must have
                at most 25 entries (Discord's Select option limit).
            parent_view: The :class:`EditPreferencesView` that owns this
                modal.  Must expose a ``selections: dict[int, bool]``
                attribute.
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

        # Build Select options from the sub-page conditions.
        options = []
        for cond in page.conditions:
            desc = str(cond["description"])
            label = (
                desc
                if len(desc) <= _SELECT_OPTION_LABEL_LIMIT
                else desc[: _SELECT_OPTION_LABEL_LIMIT - 1] + "…"
            )
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(cond["id"]),
                    default=bool(parent_view.selections.get(int(cond["id"]), False)),
                )
            )

        # Cap label at 70 chars so the full custom_id stays within Discord's
        # 100-char limit (23-char prefix + up to 70-char label = 93 chars).
        safe_label = page.label[:_CUSTOM_ID_LABEL_LIMIT]
        # Placeholder is truncated to 150 chars (Discord Select.placeholder cap).
        placeholder = f"Select {page.label} preferences…"[:150]
        self.select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=f"post_conditions_select_{safe_label}",
        )

        # Wrap in Label (type 18) so the modal payload's top-level component
        # type is accepted by Discord's modal endpoint (types 1, 10, 18 only).
        # Note: Label.text shares the same 45-char cap as Modal.title — see
        # discord.py Label docstring (.venv/Lib/site-packages/discord/ui/label.py:L60).
        self.add_item(
            discord.ui.Label(
                text=page.label[:_MODAL_TITLE_LIMIT],
                component=self.select,
            )
        )

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
        submitted_ids = {int(v) for v in self.select.values}
        sub_page_ids = {int(c["id"]) for c in self.page.conditions}
        for cid in sub_page_ids:
            self.parent_view.selections[cid] = cid in submitted_ids

        # 3. Push the full preference set to siege-web.
        try:
            await self._siege_client.set_my_preferences(
                self.discord_id,
                ids=[cid for cid, on in self.parent_view.selections.items() if on],
            )
        except SiegeWebError as exc:
            _logger.error(
                "Failed to save preferences for discord_id=%s: %s",
                self.discord_id,
                exc,
            )
            self.parent_view.selections = prior
            await interaction.response.send_message(
                "Could not save preferences — please try again.",
                ephemeral=True,
            )
            return

        # 4. Refresh the ephemeral message with the converted-shape embed.
        meta_keyed = _selections_to_meta_keyed(self.parent_view.selections, self.pages)
        embed = build_summary_embed(self.pages, meta_keyed)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


# ---------------------------------------------------------------------------
# EditPreferencesView internal button helpers
# ---------------------------------------------------------------------------


class _EditMetaButton(discord.ui.Button["EditPreferencesView"]):
    """Button that opens an EditPreferencesModal for one ModalPage sub-page.

    Attributes:
        _modal_page: The :class:`ModalPage` this button covers.
        _parent_view: The :class:`EditPreferencesView` that owns this button.
    """

    def __init__(
        self,
        *,
        page: ModalPage,
        parent_view: EditPreferencesView,
    ) -> None:
        """Initialise the button for one ModalPage.

        Args:
            page: The sub-page of conditions this button opens a modal for.
            parent_view: The owning :class:`EditPreferencesView`.
        """
        super().__init__(
            label=f"Edit {page.label}",
            style=discord.ButtonStyle.primary,
        )
        self._modal_page = page
        self._parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Open the EditPreferencesModal for this sub-page.

        Args:
            interaction: The Discord interaction that triggered the button.
        """
        modal = EditPreferencesModal(
            page=self._modal_page,
            parent_view=self._parent_view,
            siege_client=self._parent_view._siege_client,
            discord_id=self._parent_view._discord_id,
            pages=self._parent_view._pages,
        )
        await interaction.response.send_modal(modal)


class _DismissButton(discord.ui.Button["EditPreferencesView"]):
    """Button that strips buttons from the ephemeral message (keeps embed)."""

    def __init__(self) -> None:
        """Initialise the Dismiss button."""
        super().__init__(
            label="Dismiss",
            style=discord.ButtonStyle.danger,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Remove the view from the ephemeral message, preserving the embed.

        Args:
            interaction: The Discord interaction that triggered the button.
        """
        await interaction.response.edit_message(view=None)


# ---------------------------------------------------------------------------
# EditPreferencesView
# ---------------------------------------------------------------------------


class EditPreferencesView(discord.ui.View):
    """Persistent ephemeral-message view with Edit buttons + Dismiss.

    Holds the user's flat ``selections`` dict and exposes one
    :class:`_EditMetaButton` per :class:`ModalPage` produced by
    :func:`~.modal_layout.split_meta_for_modals`, plus a single
    :class:`_DismissButton`.

    Each Edit button opens an :class:`EditPreferencesModal` for its
    sub-page.  On modal submit, ``selections`` is updated in place and
    the ephemeral is refreshed via the parent view reference threaded
    through the modal.

    Attributes:
        selections: Flat mapping from catalog condition id (``int``) to
            ``bool`` — ``True`` if the condition is currently selected,
            ``False`` otherwise.  Updated in-place by each modal submit.
    """

    def __init__(
        self,
        *,
        catalog: list[dict[str, Any]],
        preferences: list[int],
        siege_client: Any,
        discord_id: str,
        timeout: float | None = 300.0,
    ) -> None:
        """Initialise the view from catalog data and saved preferences.

        Args:
            catalog: All available PostConditionResponse dicts from
                ``GET /api/post-conditions``.
            preferences: The user's currently-saved condition IDs from
                ``GET /api/members/me/preferences``.  Used to seed
                ``selections``.  IDs not present in ``catalog`` are
                silently ignored.
            siege_client: A
                :class:`~mom_bot.post_conditions.client.SiegeWebClient`
                instance threaded to each modal for the PUT call.
            discord_id: The invoking user's Discord snowflake as a string.
                Forwarded to ``siege_client.set_my_preferences`` by each
                modal on submit.
            timeout: View timeout in seconds.  Defaults to 300 (5 minutes).
        """
        super().__init__(timeout=timeout)

        self._siege_client = siege_client
        self._discord_id = discord_id

        # Full group_by_meta pages for embed rendering inside modals.
        self._pages = group_by_meta(catalog)

        # Flat {id: bool} selections seeded from saved preferences.
        preferred: set[int] = set(preferences)
        self.selections: dict[int, bool] = {}
        for cond in catalog:
            cid = int(cond["id"])
            self.selections[cid] = cid in preferred

        # One button per ModalPage sub-page.
        self._modal_pages = split_meta_for_modals(catalog)
        for page in self._modal_pages:
            self.add_item(_EditMetaButton(page=page, parent_view=self))

        # Dismiss button.
        self.add_item(_DismissButton())

    def initial_embed(self) -> discord.Embed:
        """Build a selection-summary embed from the view's current state.

        Converts the flat ``selections`` dict into the meta-keyed shape
        expected by :func:`build_summary_embed` and returns the resulting
        :class:`discord.Embed`.  Intended to be called once at message-send
        time so the initial ephemeral already reflects pre-existing
        preferences.

        Returns:
            A :class:`discord.Embed` ready to pass as ``embed=`` in the
            ``interaction.followup.send`` call that opens this view.
        """
        meta_keyed = _selections_to_meta_keyed(self.selections, self._pages)
        return build_summary_embed(self._pages, meta_keyed)
