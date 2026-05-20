"""Tests for mom_bot.post_conditions.views.

Covers: page navigation preserving selections, pre-population from initial
GET state, Commit flattening, Cancel discarding, using a fake interaction,
the live-updating selection-summary embed, and EditPreferencesView.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from mom_bot.post_conditions.modal_layout import split_meta_for_modals
from mom_bot.post_conditions.views import (
    EditPreferencesView,
    _selections_to_meta_keyed,
    build_summary_embed,
)

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_ALL_CONDITIONS: list[dict[str, Any]] = [
    # Faction & League (page 0)
    {
        "id": 1,
        "description": "Only Barbarian Champions.",
        "stronghold_level": 1,
        "condition_type": "faction",
    },
    {
        "id": 2,
        "description": "Only Telerian League Champions.",
        "stronghold_level": 1,
        "condition_type": "league",
    },
    # Role, Affinity, Rarity (page 1)
    {
        "id": 3,
        "description": "Only HP Champions.",
        "stronghold_level": 1,
        "condition_type": "role",
    },
    {
        "id": 4,
        "description": "Only Void Champions.",
        "stronghold_level": 2,
        "condition_type": "affinity",
    },
    # Effects & Other (page 2)
    {
        "id": 5,
        "description": "Immune to Turn Meter reduction.",
        "stronghold_level": 1,
        "condition_type": "effect",
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_interaction() -> MagicMock:
    """Return a minimal fake discord.Interaction for view tests."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


# ---------------------------------------------------------------------------
# build_summary_embed — unit tests
# ---------------------------------------------------------------------------

# A pages structure mirroring what group_by_meta produces.
_PAGES: list[tuple[str, list[dict[str, Any]]]] = [
    (
        "Faction & League",
        [
            {
                "id": 1,
                "description": "Only Barbarian Champions.",
                "condition_type": "faction",
            },
            {
                "id": 2,
                "description": "Only Telerian League Champions.",
                "condition_type": "league",
            },
        ],
    ),
    (
        "Role, Affinity, Rarity",
        [
            {
                "id": 3,
                "description": "Only HP Champions.",
                "condition_type": "role",
            },
            {
                "id": 4,
                "description": "Only Void Champions.",
                "condition_type": "affinity",
            },
        ],
    ),
    (
        "Effects & Other",
        [
            {
                "id": 5,
                "description": "Immune to Turn Meter reduction.",
                "condition_type": "effect",
            },
        ],
    ),
]


def test_build_summary_embed_empty() -> None:
    """No selections → embed has '_None selected yet.' description."""
    selections: dict[str, set[int]] = {
        "Faction & League": set(),
        "Role, Affinity, Rarity": set(),
        "Effects & Other": set(),
    }
    embed = build_summary_embed(_PAGES, selections)
    assert isinstance(embed, discord.Embed)
    assert embed.description is not None
    assert "_None selected yet._" in embed.description


def test_build_summary_embed_single_meta() -> None:
    """All selections in one meta-group → single bold heading with items listed."""
    selections: dict[str, set[int]] = {
        "Faction & League": {1, 2},
        "Role, Affinity, Rarity": set(),
        "Effects & Other": set(),
    }
    embed = build_summary_embed(_PAGES, selections)
    assert embed.description is not None
    # Bold heading for the group should appear.
    assert "**Faction & League**" in embed.description
    # Both descriptions should be present.
    assert "Only Barbarian Champions." in embed.description
    assert "Only Telerian League Champions." in embed.description
    # The group with no selections should not add a heading.
    assert "**Role, Affinity, Rarity**" not in embed.description
    assert "**Effects & Other**" not in embed.description


def test_build_summary_embed_multi_meta() -> None:
    """Selections in two meta-groups → both bold headings with items listed."""
    selections: dict[str, set[int]] = {
        "Faction & League": {1},
        "Role, Affinity, Rarity": {3},
        "Effects & Other": set(),
    }
    embed = build_summary_embed(_PAGES, selections)
    assert embed.description is not None
    assert "**Faction & League**" in embed.description
    assert "Only Barbarian Champions." in embed.description
    assert "**Role, Affinity, Rarity**" in embed.description
    assert "Only HP Champions." in embed.description
    # Empty group omitted.
    assert "**Effects & Other**" not in embed.description


def test_build_summary_embed_overflow_truncates() -> None:
    """When many items are selected, description stays within 4096 chars."""
    # Build a large fake pages/selections structure.
    big_pages: list[tuple[str, list[dict[str, Any]]]] = [
        (
            "Faction & League",
            [
                {
                    "id": i,
                    "description": "A" * 90,  # near max label length
                    "condition_type": "faction",
                }
                for i in range(1, 101)  # 100 items
            ],
        ),
    ]
    selections: dict[str, set[int]] = {"Faction & League": set(range(1, 101))}
    embed = build_summary_embed(big_pages, selections)
    assert embed.description is not None
    assert len(embed.description) <= 4096
    # Truncation marker must appear somewhere in the description.
    assert "more" in embed.description


# ---------------------------------------------------------------------------
# _selections_to_meta_keyed — unit tests
# ---------------------------------------------------------------------------


def test_selections_to_meta_keyed_returns_empty_for_empty_input() -> None:
    """Empty selections dict and non-empty pages → empty result dict."""
    result = _selections_to_meta_keyed({}, _PAGES)
    assert result == {}


def test_selections_to_meta_keyed_returns_empty_for_all_false_selections() -> None:
    """All-False selections → empty result (falsy entries produce no labels)."""
    selections: dict[int, bool] = {1: False, 2: False, 3: False}
    result = _selections_to_meta_keyed(selections, _PAGES)
    assert result == {}


def test_selections_to_meta_keyed_distributes_mixed_selections() -> None:
    """Mixed True/False selections are distributed into the correct meta buckets."""
    # id 1 → Faction & League, id 3 → Role Affinity Rarity, id 5 → Effects & Other
    selections: dict[int, bool] = {1: True, 2: False, 3: True, 4: False, 5: True}
    result = _selections_to_meta_keyed(selections, _PAGES)
    assert result == {
        "Faction & League": {1},
        "Role, Affinity, Rarity": {3},
        "Effects & Other": {5},
    }


def test_selections_to_meta_keyed_ignores_ids_not_in_pages() -> None:
    """IDs in selections that are absent from pages are silently ignored."""
    # id 999 is not present in _PAGES — should not crash or appear in result.
    selections: dict[int, bool] = {1: True, 999: True}
    result = _selections_to_meta_keyed(selections, _PAGES)
    assert result == {"Faction & League": {1}}
    assert all(999 not in ids for ids in result.values())


def test_selections_to_meta_keyed_omits_empty_meta_labels() -> None:
    """Meta labels whose bucket would be empty do not appear as keys in result."""
    # Only id 5 selected (Effects & Other); Faction & League and RAR should be absent.
    selections: dict[int, bool] = {5: True}
    result = _selections_to_meta_keyed(selections, _PAGES)
    assert "Faction & League" not in result
    assert "Role, Affinity, Rarity" not in result
    assert result == {"Effects & Other": {5}}


# ---------------------------------------------------------------------------
# EditPreferencesView — construction
# ---------------------------------------------------------------------------


def _make_siege_client() -> MagicMock:
    """Return a minimal fake SiegeWebClient for view tests."""
    client = MagicMock()
    client.set_my_preferences = AsyncMock(return_value=[])
    return client


def test_edit_preferences_view_constructs_without_error() -> None:
    """EditPreferencesView can be instantiated with catalog and preferences."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[1, 3],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    assert view is not None


def test_edit_preferences_view_selections_dict_keys_cover_all_catalog_ids() -> None:
    """selections dict has a key for every catalog condition id."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[1, 3],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    expected_ids = {int(c["id"]) for c in _ALL_CONDITIONS}
    assert set(view.selections.keys()) == expected_ids


def test_edit_preferences_view_selections_true_for_preferred_ids() -> None:
    """selections[id] is True for each id in preferences."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[1, 3],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    assert view.selections[1] is True
    assert view.selections[3] is True


def test_edit_preferences_view_selections_false_for_unpreferred_ids() -> None:
    """selections[id] is False for catalog ids not in preferences."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[1, 3],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    assert view.selections[2] is False
    assert view.selections[4] is False
    assert view.selections[5] is False


def test_edit_preferences_view_selections_all_false_when_no_preferences() -> None:
    """selections dict is all-False when preferences list is empty."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    assert all(v is False for v in view.selections.values())


# ---------------------------------------------------------------------------
# EditPreferencesView — button composition
# ---------------------------------------------------------------------------


def _count_buttons_by_type(
    children: list[Any],
    label_prefix: str,
) -> int:
    """Count discord.ui.Button children whose label starts with label_prefix."""
    return sum(
        1
        for child in children
        if isinstance(child, discord.ui.Button)
        and child.label is not None
        and child.label.startswith(label_prefix)
    )


def test_edit_preferences_view_has_one_edit_button_per_modal_page() -> None:
    """One EditMetaButton is added per ModalPage from split_meta_for_modals."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    expected_pages = split_meta_for_modals(_ALL_CONDITIONS)
    edit_button_count = _count_buttons_by_type(view.children, "Edit ")
    assert edit_button_count == len(expected_pages)


def test_edit_preferences_view_edit_button_labels_match_modal_page_labels() -> None:
    """Each EditMetaButton label is 'Edit <page.label>'."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    expected_pages = split_meta_for_modals(_ALL_CONDITIONS)
    expected_labels = {f"Edit {page.label}" for page in expected_pages}
    actual_labels = {
        child.label
        for child in view.children
        if isinstance(child, discord.ui.Button)
        and child.label is not None
        and child.label.startswith("Edit ")
    }
    assert actual_labels == expected_labels


def test_edit_preferences_view_has_exactly_one_dismiss_button() -> None:
    """EditPreferencesView always has exactly one Dismiss button."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    dismiss_count = _count_buttons_by_type(view.children, "Dismiss")
    assert dismiss_count == 1


def test_edit_preferences_view_empty_catalog_has_no_edit_buttons() -> None:
    """Empty catalog produces no Edit buttons — just the Dismiss button."""
    view = EditPreferencesView(
        catalog=[],
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    edit_button_count = _count_buttons_by_type(view.children, "Edit ")
    assert edit_button_count == 0
    dismiss_count = _count_buttons_by_type(view.children, "Dismiss")
    assert dismiss_count == 1


def test_edit_preferences_view_large_catalog_button_count_matches_sub_pages() -> None:
    """Catalog forcing sub-pagination gives button count = sub-page count."""
    # Build 12 conditions in the same meta-group → 2 sub-pages.
    large_catalog: list[dict[str, Any]] = [
        {
            "id": i,
            "description": f"Effect condition {i}.",
            "condition_type": "effect",
            "stronghold_level": 1,
        }
        for i in range(1, 13)
    ]
    view = EditPreferencesView(
        catalog=large_catalog,
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    expected_pages = split_meta_for_modals(large_catalog)
    assert len(expected_pages) == 2
    edit_button_count = _count_buttons_by_type(view.children, "Edit ")
    assert edit_button_count == len(expected_pages)


# ---------------------------------------------------------------------------
# EditPreferencesView — button callbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_meta_button_sends_modal_on_click() -> None:
    """Clicking an EditMetaButton calls interaction.response.send_modal."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[1],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    # Find the first Edit button.
    edit_button: discord.ui.Button[Any] | None = None
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.label is not None:
            if child.label.startswith("Edit "):
                edit_button = child
                break
    assert edit_button is not None, "No Edit button found"

    interaction = _make_interaction()
    interaction.response.send_modal = AsyncMock()

    await edit_button.callback(interaction)  # type: ignore[misc]

    interaction.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_meta_button_sends_correct_modal_page() -> None:
    """EditMetaButton sends an EditPreferencesModal whose page matches the button."""
    from mom_bot.post_conditions.views import EditPreferencesModal

    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    expected_pages = split_meta_for_modals(_ALL_CONDITIONS)
    # Check the first Edit button's modal has the matching page.
    first_page = expected_pages[0]
    first_edit_label = f"Edit {first_page.label}"

    edit_button: discord.ui.Button[Any] | None = None
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.label == first_edit_label:
            edit_button = child
            break
    assert edit_button is not None, f"No button with label {first_edit_label!r}"

    interaction = _make_interaction()
    interaction.response.send_modal = AsyncMock()

    await edit_button.callback(interaction)  # type: ignore[misc]

    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.call_args[0][0]
    assert isinstance(sent_modal, EditPreferencesModal)
    assert sent_modal.page == first_page


@pytest.mark.asyncio
async def test_dismiss_button_edits_message_with_no_view() -> None:
    """Clicking Dismiss calls interaction.response.edit_message(view=None)."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    dismiss_button: discord.ui.Button[Any] | None = None
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.label == "Dismiss":
            dismiss_button = child
            break
    assert dismiss_button is not None, "No Dismiss button found"

    interaction = _make_interaction()
    await dismiss_button.callback(interaction)  # type: ignore[misc]

    interaction.response.edit_message.assert_awaited_once()
    call_kwargs = interaction.response.edit_message.call_args.kwargs
    assert call_kwargs.get("view") is None


# ---------------------------------------------------------------------------
# EditPreferencesView.initial_embed — convenience method
# ---------------------------------------------------------------------------


def test_initial_embed_returns_discord_embed() -> None:
    """initial_embed() returns a discord.Embed instance."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    embed = view.initial_embed()
    assert isinstance(embed, discord.Embed)


def test_initial_embed_empty_preferences_shows_none_selected() -> None:
    """initial_embed() with no preferences shows '_None selected yet.' text."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    embed = view.initial_embed()
    assert embed.description is not None
    assert "_None selected yet._" in embed.description


def test_initial_embed_with_preferences_shows_selection_text() -> None:
    """initial_embed() with preferences shows selected items, not empty state."""
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=[1, 3],
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    embed = view.initial_embed()
    assert embed.description is not None
    assert "_None selected yet._" not in embed.description
    assert "Only Barbarian Champions." in embed.description
    assert "Only HP Champions." in embed.description


def test_initial_embed_equals_build_summary_embed_output() -> None:
    """initial_embed() output equals calling build_summary_embed directly."""
    preferences = [1, 3]
    view = EditPreferencesView(
        catalog=_ALL_CONDITIONS,
        preferences=preferences,
        siege_client=_make_siege_client(),
        discord_id="123",
    )
    embed_from_method = view.initial_embed()

    # Build the expected embed the long way.
    pages = view._pages
    meta_keyed = _selections_to_meta_keyed(view.selections, pages)
    expected_embed = build_summary_embed(pages, meta_keyed)

    assert embed_from_method.description == expected_embed.description
    assert embed_from_method.title == expected_embed.title


# ---------------------------------------------------------------------------
# Discord component-limit safety net
# ---------------------------------------------------------------------------


def test_edit_preferences_view_stays_within_discord_component_limit() -> None:
    """View total components (edit buttons + dismiss) must not exceed Discord's 25 cap.

    Constructs a large synthetic catalog that exercises sub-pagination across
    multiple meta-pages and asserts the resulting child-count stays within
    the limit defined by _DISCORD_VIEW_COMPONENT_LIMIT.
    """
    from mom_bot.post_conditions.views import _DISCORD_VIEW_COMPONENT_LIMIT

    # 20 conditions per condition_type across all canonical types → 140 total,
    # many sub-pages.  IDs are globally unique so no collision across types.
    condition_types = (
        "faction",
        "league",
        "role",
        "affinity",
        "rarity",
        "effect",
        "other",
    )
    large_catalog: list[dict[str, Any]] = []
    uid = 0
    for ctype in condition_types:
        for _ in range(20):
            large_catalog.append(
                {
                    "id": uid,
                    "condition_type": ctype,
                    "description": f"Condition {uid}",
                }
            )
            uid += 1

    view = EditPreferencesView(
        catalog=large_catalog,
        preferences=[],
        siege_client=_make_siege_client(),
        discord_id="42",
    )
    assert len(view.children) <= _DISCORD_VIEW_COMPONENT_LIMIT, (
        f"View has {len(view.children)} components, exceeding Discord's "
        f"{_DISCORD_VIEW_COMPONENT_LIMIT}-component cap.  "
        "The view design needs a hard-limit guard."
    )


# ---------------------------------------------------------------------------
# PostConditionsGridView — Phase 2 tests
# ---------------------------------------------------------------------------

# Minimal catalog with real short-label mappings for tests that exercise
# the button-label path.  Each condition carries both "label" (required
# by short_label) and "description" (required by build_summary_embed).
_GRID_CATALOG_FACTION: list[dict[str, Any]] = [
    {
        "id": 1,
        "label": "Only Banner Lord Champions can be used.",
        "description": "Only Banner Lord Champions can be used.",
        "condition_type": "faction",
    },
    {
        "id": 2,
        "label": "Only Sylvan Watcher Champions can be used.",
        "description": "Only Sylvan Watcher Champions can be used.",
        "condition_type": "faction",
    },
]

_GRID_CATALOG_ROLE: list[dict[str, Any]] = [
    {
        "id": 3,
        "label": "Only HP Champions can be used.",
        "description": "Only HP Champions can be used.",
        "condition_type": "role",
    },
]

_GRID_CATALOG_TWO_GROUPS: list[dict[str, Any]] = _GRID_CATALOG_FACTION + _GRID_CATALOG_ROLE


def test_grid_view_construction_seeds_selections_from_preferences() -> None:
    """View is constructed with current preferences pre-toggled ON."""
    from mom_bot.post_conditions.views import PostConditionsGridView

    view = PostConditionsGridView(
        catalog=_GRID_CATALOG_TWO_GROUPS,
        preferences=[1, 3],
        discord_id="123",
        siege_client=object(),
    )
    assert view._selections == {1: True, 2: False, 3: True}
    assert view._page_index == 0
    # 2 pages: one for Faction & League (ids 1, 2), one for Role/Affinity/Rarity (id 3).
    assert len(view._pages) == 2


def test_grid_view_component_count_within_25() -> None:
    """A worst-case page (20 toggles) + 4 nav buttons = 24 ≤ 25."""
    from mom_bot.post_conditions.views import PostConditionsGridView

    # Build 20 faction conditions that have real short-label mappings.
    # Use the 19-entry faction group from _SHORT_LABELS and one league entry.
    faction_labels = [
        "Only Champions from the Telerian League can be used.",
        "Only Champions from the Gaellen Pact can be used.",
        "Only Champions from The Corrupted can be used.",
        "Only Champions from the Nyresan Union can be used.",
        "Only Banner Lord Champions can be used.",
        "Only High Elves Champions can be used.",
        "Only Sacred Order Champions can be used.",
        "Only Barbarian Champions can be used.",
        "Only Ogryn Tribe Champions can be used.",
        "Only Lizardmen Champions can be used.",
        "Only Skinwalker Champions can be used.",
        "Only Orc Champions can be used.",
        "Only Demonspawn Champions can be used.",
        "Only Undead Horde Champions can be used.",
        "Only Dark Elves Champions can be used.",
        "Only Knights Revenant Champions can be used.",
        "Only Dwarves Champions can be used.",
        "Only Shadowkin Champions can be used.",
        "Only Sylvan Watcher Champions can be used.",
    ]
    league_label = "Only Champions from the Telerian League can be used."
    twenty_labels = faction_labels[:19] + [league_label]
    catalog = [
        {
            "id": i,
            "label": lbl,
            "description": lbl,
            "condition_type": "faction",
        }
        for i, lbl in enumerate(twenty_labels, start=1)
    ]
    view = PostConditionsGridView(
        catalog=catalog, preferences=[], discord_id="x", siege_client=object()
    )
    # 20 toggles + 4 nav = 24.
    assert len(view.children) == 24


def test_grid_view_default_on_toggles_pre_styled_success() -> None:
    """Toggle button is success style for conditions in preferences."""
    from mom_bot.post_conditions.views import PostConditionsGridView, _ToggleButton

    view = PostConditionsGridView(
        catalog=_GRID_CATALOG_FACTION,
        preferences=[1],
        discord_id="x",
        siege_client=object(),
    )
    toggles = [c for c in view.children if isinstance(c, _ToggleButton)]
    by_id = {t._condition_id: t for t in toggles}
    assert by_id[1].style == discord.ButtonStyle.success
    assert by_id[2].style == discord.ButtonStyle.secondary


def test_grid_view_embed_title_has_meta_group_header_and_page_indicator() -> None:
    """Embed title matches 'Editing — {meta_label} (page i/N)'."""
    from mom_bot.post_conditions.views import PostConditionsGridView

    view = PostConditionsGridView(
        catalog=_GRID_CATALOG_TWO_GROUPS,
        preferences=[],
        discord_id="x",
        siege_client=object(),
    )
    # Page 0 → Faction & League.
    embed = view.initial_embed()
    assert embed.title == "Editing — Faction & League (page 1/2)"

    # Advance to page 1 → Role, Affinity, Rarity.
    view._page_index = 1
    view._build_components()
    embed = view.initial_embed()
    assert embed.title == "Editing — Role, Affinity, Rarity (page 2/2)"


def test_grid_view_buttons_use_short_labels_not_canonical() -> None:
    """Buttons render with discord_display short label, not canonical string."""
    from mom_bot.post_conditions.views import PostConditionsGridView, _ToggleButton

    catalog = [
        {
            "id": 1,
            "label": "Only Sylvan Watcher Champions can be used.",
            "description": "Only Sylvan Watcher Champions can be used.",
            "condition_type": "faction",
        }
    ]
    view = PostConditionsGridView(
        catalog=catalog, preferences=[], discord_id="x", siege_client=object()
    )
    toggle = next(c for c in view.children if isinstance(c, _ToggleButton))
    assert toggle.label == "Sylvan Watchers"
    # Must NOT be the canonical string.
    assert toggle.label != "Only Sylvan Watcher Champions can be used."


def test_grid_view_button_count_matches_expected_for_19_11_6() -> None:
    """A 3-page catalog (19/11/6) gives correct button count per page.

    Page 1: 19 toggle buttons + 4 nav = 23 total.
    Page 2: 11 toggle buttons + 4 nav = 15 total.
    Page 3: 6 toggle buttons + 4 nav = 10 total.
    """
    from mom_bot.post_conditions.views import PostConditionsGridView

    # Build the full 36-entry catalog from the canonical table.
    faction_conditions = [
        {"id": i, "label": lbl, "description": lbl, "condition_type": "faction"}
        for i, lbl in enumerate(
            [
                "Only Champions from the Telerian League can be used.",
                "Only Champions from the Gaellen Pact can be used.",
                "Only Champions from The Corrupted can be used.",
                "Only Champions from the Nyresan Union can be used.",
                "Only Banner Lord Champions can be used.",
                "Only High Elves Champions can be used.",
                "Only Sacred Order Champions can be used.",
                "Only Barbarian Champions can be used.",
                "Only Ogryn Tribe Champions can be used.",
                "Only Lizardmen Champions can be used.",
                "Only Skinwalker Champions can be used.",
                "Only Orc Champions can be used.",
                "Only Demonspawn Champions can be used.",
                "Only Undead Horde Champions can be used.",
                "Only Dark Elves Champions can be used.",
                "Only Knights Revenant Champions can be used.",
                "Only Dwarves Champions can be used.",
                "Only Shadowkin Champions can be used.",
                "Only Sylvan Watcher Champions can be used.",
            ],
            start=1,
        )
    ]
    role_conditions = [
        {"id": i, "label": lbl, "description": lbl, "condition_type": "role"}
        for i, lbl in enumerate(
            [
                "Only HP Champions can be used.",
                "Only DEF Champions can be used.",
                "Only Support Champions can be used.",
                "Only ATK Champions can be used.",
                "Only Void Champions can be used.",
                "Only Force Champions can be used.",
                "Only Magic Champions can be used.",
                "Only Spirit Champions can be used.",
                "Only Legendary Champions can be used.",
                "Only Epic Champions can be used.",
                "Only Rare Champions can be used.",
            ],
            start=20,
        )
    ]
    effect_conditions = [
        {"id": i, "label": lbl, "description": lbl, "condition_type": "effect"}
        for i, lbl in enumerate(
            [
                "All Champions are immune to Turn Meter reduction effects.",
                "All Champions are immune to Turn Meter fill effects.",
                "All Champions are immune to cooldown increasing effects.",
                "All Champions are immune to cooldown decreasing effects.",
                "All Champions are immune to [Sheep] debuffs.",
                "Champions cannot be revived.",
            ],
            start=31,
        )
    ]
    full_catalog = faction_conditions + role_conditions + effect_conditions

    view = PostConditionsGridView(
        catalog=full_catalog,
        preferences=[],
        discord_id="x",
        siege_client=object(),
    )
    # Page 0: 19 faction toggles + 4 nav.
    assert len(view.children) == 23

    view._page_index = 1
    view._build_components()
    assert len(view.children) == 15  # 11 role + 4 nav

    view._page_index = 2
    view._build_components()
    assert len(view.children) == 10  # 6 effects + 4 nav


def test_grid_view_prev_disabled_on_first_page_next_disabled_on_last() -> None:
    """Prev is disabled on page 0; Next is disabled on the last page."""
    from mom_bot.post_conditions.views import NavButton, PostConditionsGridView

    view = PostConditionsGridView(
        catalog=_GRID_CATALOG_TWO_GROUPS,
        preferences=[],
        discord_id="x",
        siege_client=object(),
    )
    navs = {n._direction: n for n in view.children if isinstance(n, NavButton)}
    assert navs["prev"].disabled is True
    assert navs["next"].disabled is False

    # Advance to last page.
    view._page_index = 1
    view._build_components()
    navs = {n._direction: n for n in view.children if isinstance(n, NavButton)}
    assert navs["prev"].disabled is False
    assert navs["next"].disabled is True


@pytest.mark.asyncio
async def test_grid_view_toggle_flips_selection_and_style() -> None:
    """Toggling a button flips its selection state and restyled to match."""
    from mom_bot.post_conditions.views import PostConditionsGridView, _ToggleButton

    view = PostConditionsGridView(
        catalog=_GRID_CATALOG_FACTION,
        preferences=[],  # starts all OFF
        discord_id="x",
        siege_client=object(),
    )

    # id 1 starts OFF (secondary).
    toggle = next(c for c in view.children if isinstance(c, _ToggleButton) and c._condition_id == 1)
    assert view._selections[1] is False
    assert toggle.style == discord.ButtonStyle.secondary

    # Simulate the callback.
    interaction = _make_interaction()
    toggle._view = view  # wire parent reference as discord.py would
    await toggle.callback(interaction)

    # Selection flipped to ON; embed + view sent back.
    assert view._selections[1] is True
    interaction.response.edit_message.assert_awaited_once()

    # The newly-rebuilt button for id 1 should now be success.
    new_toggle = next(
        c for c in view.children if isinstance(c, _ToggleButton) and c._condition_id == 1
    )
    assert new_toggle.style == discord.ButtonStyle.success


@pytest.mark.asyncio
async def test_grid_view_nav_changes_page_and_preserves_selections() -> None:
    """NavButton changes _page_index; selections from prior page survive."""
    from mom_bot.post_conditions.views import NavButton, PostConditionsGridView

    view = PostConditionsGridView(
        catalog=_GRID_CATALOG_TWO_GROUPS,
        preferences=[1],  # id 1 ON on page 0
        discord_id="x",
        siege_client=object(),
    )
    assert view._page_index == 0
    assert view._selections[1] is True

    # Find and fire the Next button.
    next_btn = next(n for n in view.children if isinstance(n, NavButton) and n._direction == "next")
    interaction = _make_interaction()
    next_btn._view = view
    await next_btn.callback(interaction)

    assert view._page_index == 1
    # Selection for id 1 (on page 0) must still be True.
    assert view._selections[1] is True
    interaction.response.edit_message.assert_awaited_once()


def test_grid_view_b1_regression_subpaginated_meta_renders_heading_once() -> None:
    """When a meta-group sub-paginates, _summary_pages merges it.

    build_summary_embed must render the meta-group heading exactly once,
    not once per GridPage sub-page.  Forces sub-pagination by overriding
    _pages after construction with a page_size=10 split.

    The catalog uses 12 faction entries from the canonical SHORT_LABELS
    table so short_label does not raise KeyError during _build_components.
    """
    from mom_bot.post_conditions.grid_layout import split_by_meta_group
    from mom_bot.post_conditions.views import (
        PostConditionsGridView,
        _flat_to_meta_keyed,
        build_summary_embed,
    )

    # 12 faction labels from the canonical SHORT_LABELS table.
    faction_labels = [
        "Only Banner Lord Champions can be used.",
        "Only High Elves Champions can be used.",
        "Only Sacred Order Champions can be used.",
        "Only Barbarian Champions can be used.",
        "Only Ogryn Tribe Champions can be used.",
        "Only Lizardmen Champions can be used.",
        "Only Skinwalker Champions can be used.",
        "Only Orc Champions can be used.",
        "Only Demonspawn Champions can be used.",
        "Only Undead Horde Champions can be used.",
        "Only Dark Elves Champions can be used.",
        "Only Knights Revenant Champions can be used.",
    ]
    catalog = [
        {
            "id": i,
            "label": lbl,
            "description": lbl,
            "condition_type": "faction",
        }
        for i, lbl in enumerate(faction_labels, start=1)
    ]

    # Build pages with small page_size to force sub-pagination.
    pages_subpaged = split_by_meta_group(catalog, page_size=10)
    assert len(pages_subpaged) == 2, "expected 2 sub-pages for 12 conditions at page_size=10"

    # Build view normally (default page_size=20 → one page), then
    # override _pages to simulate the sub-paginated layout.
    view = PostConditionsGridView(
        catalog=catalog,
        preferences=[1, 11],  # one from each sub-page
        discord_id="x",
        siege_client=object(),
    )
    view._pages = pages_subpaged  # type: ignore[misc]

    summary = view._summary_pages()
    assert len(summary) == 1, (
        f"expected 1 merged tuple for sub-paginated Faction & League; "
        f"got {[s[0] for s in summary]}"
    )
    base_label, conditions = summary[0]
    assert base_label == "Faction & League"
    assert len(conditions) == 12

    # build_summary_embed must render the heading exactly once.
    view._selections[1] = True
    view._selections[11] = True
    embed = build_summary_embed(
        pages=summary,
        selections=_flat_to_meta_keyed(view._selections, view._pages),
    )
    all_text = (embed.description or "") + (embed.title or "")
    for f in embed.fields:
        all_text += f"\n{f.name}\n{f.value}"
    assert (
        all_text.count("Faction & League") == 1
    ), "B1 regression: 'Faction & League' heading appeared more than once"
