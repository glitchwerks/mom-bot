"""Discord UI views for post-condition preference selection.

Provides :class:`PostConditionsGridView` — an ephemeral button-grid view
that lets the user toggle post-condition preferences per meta-group page,
with a live-updating summary embed and a Save button that commits staged
selections in a single call.

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

__all__ = [
    "build_summary_embed",
    "PostConditionsGridView",
]

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

        if lines:  # not the first emitted group — add blank separator
            lines.append("")

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
