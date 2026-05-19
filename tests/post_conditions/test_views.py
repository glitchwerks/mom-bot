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
    PostConditionsView,
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

# Initial preferences: id 1 (faction) and id 3 (role) selected.
_INITIAL_PREFS: list[dict[str, Any]] = [
    {
        "id": 1,
        "description": "Only Barbarian Champions.",
        "stronghold_level": 1,
        "condition_type": "faction",
    },
    {
        "id": 3,
        "description": "Only HP Champions.",
        "stronghold_level": 1,
        "condition_type": "role",
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


def _make_select_interaction(values: list[str]) -> MagicMock:
    """Return an interaction where the Select was submitted with given values."""
    interaction = _make_interaction()
    interaction.data = {"values": values}
    return interaction


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_view_constructs_without_error() -> None:
    """PostConditionsView can be instantiated with catalog and initial prefs."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=_INITIAL_PREFS,
        discord_id="123",
        siege_client=client,
    )
    assert view is not None


def test_view_starts_on_page_zero() -> None:
    """PostConditionsView starts on page 0 (first meta-group)."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=_INITIAL_PREFS,
        discord_id="123",
        siege_client=client,
    )
    assert view.current_page == 0


def test_view_has_three_pages_for_full_catalog() -> None:
    """PostConditionsView has 3 pages when all three meta-groups are non-empty."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=_INITIAL_PREFS,
        discord_id="123",
        siege_client=client,
    )
    assert view.page_count == 3


def test_view_has_fewer_pages_when_meta_group_empty() -> None:
    """PostConditionsView has 2 pages when one meta-group has no conditions."""
    # Only faction + role — Effects & Other is empty.
    partial_catalog = [c for c in _ALL_CONDITIONS if c["condition_type"] in ("faction", "role")]
    client = MagicMock()
    view = PostConditionsView(
        catalog=partial_catalog,
        initial_prefs=[],
        discord_id="123",
        siege_client=client,
    )
    assert view.page_count == 2


# ---------------------------------------------------------------------------
# Pre-population from initial GET state
# ---------------------------------------------------------------------------


def test_view_prepopulates_selections_from_initial_prefs() -> None:
    """Selections dict is pre-populated from initial_prefs on construction."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=_INITIAL_PREFS,
        discord_id="123",
        siege_client=client,
    )
    # id 1 is faction → Faction & League page
    fl_label = "Faction & League"
    assert 1 in view.selections[fl_label]

    # id 3 is role → Role, Affinity, Rarity page
    rar_label = "Role, Affinity, Rarity"
    assert 3 in view.selections[rar_label]


def test_view_unselected_conditions_not_in_selections() -> None:
    """Conditions not in initial_prefs are not in selections."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=_INITIAL_PREFS,
        discord_id="123",
        siege_client=client,
    )
    # id 2 (league) was not in initial_prefs
    fl_label = "Faction & League"
    assert 2 not in view.selections[fl_label]


# ---------------------------------------------------------------------------
# Page navigation preserves selections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_page_preserves_current_page_selections() -> None:
    """Pressing Next captures current page selections before advancing."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=[],
        discord_id="123",
        siege_client=client,
    )
    # Simulate user picking id=2 on page 0 (Faction & League).
    view.selections["Faction & League"] = {2}

    interaction = _make_interaction()
    await view.go_next(interaction)

    # Page should have advanced.
    assert view.current_page == 1
    # Selections for page 0 are preserved.
    assert 2 in view.selections["Faction & League"]


@pytest.mark.asyncio
async def test_prev_page_preserves_current_page_selections() -> None:
    """Pressing Prev captures current page selections before going back."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=[],
        discord_id="123",
        siege_client=client,
    )
    # Navigate to page 1 first.
    view.current_page = 1
    view.selections["Role, Affinity, Rarity"] = {3}

    interaction = _make_interaction()
    await view.go_prev(interaction)

    assert view.current_page == 0
    assert 3 in view.selections["Role, Affinity, Rarity"]


@pytest.mark.asyncio
async def test_round_trip_navigation_preserves_all_pages() -> None:
    """Next → Prev → Next → Next → Prev preserves all selections."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=[],
        discord_id="123",
        siege_client=client,
    )
    # Set up distinct selections per page.
    view.selections["Faction & League"] = {1}
    view.selections["Role, Affinity, Rarity"] = {3}
    view.selections["Effects & Other"] = {5}

    interaction = _make_interaction()

    # Next → page 1
    await view.go_next(interaction)
    assert view.current_page == 1

    # Prev → page 0
    await view.go_prev(interaction)
    assert view.current_page == 0
    assert 1 in view.selections["Faction & League"]

    # Next → page 1
    await view.go_next(interaction)
    assert view.current_page == 1
    assert 3 in view.selections["Role, Affinity, Rarity"]

    # Next → page 2
    await view.go_next(interaction)
    assert view.current_page == 2
    assert 5 in view.selections["Effects & Other"]

    # Prev → page 1
    await view.go_prev(interaction)
    assert view.current_page == 1
    assert 3 in view.selections["Role, Affinity, Rarity"]


# ---------------------------------------------------------------------------
# Commit — flatten dict and call PUT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_flattens_selections_and_calls_set_preferences() -> None:
    """Commit sends all selected IDs from all pages in a single PUT call."""
    siege_client = MagicMock()
    siege_client.set_my_preferences = AsyncMock(return_value=[])

    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=[],
        discord_id="999",
        siege_client=siege_client,
    )
    view.selections["Faction & League"] = {1, 2}
    view.selections["Role, Affinity, Rarity"] = {3}
    view.selections["Effects & Other"] = set()

    interaction = _make_interaction()
    await view.commit(interaction)

    siege_client.set_my_preferences.assert_awaited_once()
    call_args = siege_client.set_my_preferences.call_args
    sent_ids: list[int] = call_args[1].get("ids") or call_args[0][1]
    assert set(sent_ids) == {1, 2, 3}


@pytest.mark.asyncio
async def test_commit_with_empty_selections_sends_empty_list() -> None:
    """Commit with nothing selected sends empty list — clears all preferences."""
    siege_client = MagicMock()
    siege_client.set_my_preferences = AsyncMock(return_value=[])

    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=[],
        discord_id="999",
        siege_client=siege_client,
    )

    interaction = _make_interaction()
    await view.commit(interaction)

    call_args = siege_client.set_my_preferences.call_args
    # set_my_preferences is called with keyword args only.
    sent_ids: list[int] = call_args.kwargs.get("ids", call_args[0][1] if call_args[0] else [])
    assert sent_ids == []


@pytest.mark.asyncio
async def test_commit_sends_correct_discord_id() -> None:
    """Commit passes the view's discord_id to set_my_preferences."""
    siege_client = MagicMock()
    siege_client.set_my_preferences = AsyncMock(return_value=[])

    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=[],
        discord_id="777888999",
        siege_client=siege_client,
    )

    interaction = _make_interaction()
    await view.commit(interaction)

    call_args = siege_client.set_my_preferences.call_args
    sent_discord_id: str = call_args[1].get("discord_id") or call_args[0][0]
    assert sent_discord_id == "777888999"


# ---------------------------------------------------------------------------
# Cancel — discard without writing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_does_not_call_set_preferences() -> None:
    """Cancel must not call set_my_preferences."""
    siege_client = MagicMock()
    siege_client.set_my_preferences = AsyncMock()

    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=_INITIAL_PREFS,
        discord_id="123",
        siege_client=siege_client,
    )
    view.selections["Faction & League"] = {1, 2}

    interaction = _make_interaction()
    await view.cancel(interaction)

    siege_client.set_my_preferences.assert_not_awaited()


# ---------------------------------------------------------------------------
# Header content
# ---------------------------------------------------------------------------


def test_header_shows_page_number_and_total() -> None:
    """build_header returns a string with 'Page X of N' and meta label."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=[],
        discord_id="123",
        siege_client=client,
    )
    header = view.build_header()
    assert "Page 1 of 3" in header
    assert "Faction & League" in header


def test_header_shows_total_selected_count() -> None:
    """build_header shows total selected count across all pages."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=_INITIAL_PREFS,  # 2 prefs selected
        discord_id="123",
        siege_client=client,
    )
    header = view.build_header()
    assert "Selected: 2" in header


def test_header_updates_on_page_change() -> None:
    """build_header reflects current page after navigation."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=[],
        discord_id="123",
        siege_client=client,
    )
    view.current_page = 1
    header = view.build_header()
    assert "Page 2 of 3" in header
    assert "Role, Affinity, Rarity" in header


# ---------------------------------------------------------------------------
# build_summary_embed — unit tests
# ---------------------------------------------------------------------------

# A pages structure mirroring what PostConditionsView._pages produces.
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


@pytest.mark.asyncio
async def test_on_select_rerenders_embed() -> None:
    """Toggling the Select re-renders the embed alongside the View."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=[],
        discord_id="123",
        siege_client=client,
    )
    # Grab the Select item that was added during _rebuild_items().
    select_item: discord.ui.Select[Any] | None = None
    for item in view.children:
        if isinstance(item, discord.ui.Select):
            select_item = item
            break
    assert select_item is not None, "No Select found in view items"

    interaction = _make_select_interaction(["1", "2"])
    # The callback must be awaitable and call edit_message with embed= kwarg.
    await select_item.callback(interaction)  # type: ignore[misc]

    interaction.response.edit_message.assert_awaited_once()
    call_kwargs = interaction.response.edit_message.call_args.kwargs
    assert "embed" in call_kwargs, "edit_message was not called with embed= kwarg"
    assert "view" in call_kwargs, "edit_message was not called with view= kwarg"
    assert isinstance(call_kwargs["embed"], discord.Embed)


@pytest.mark.asyncio
async def test_prev_next_preserves_embed_selections() -> None:
    """Page navigation preserves cross-page selections in the embed."""
    client = MagicMock()
    view = PostConditionsView(
        catalog=_ALL_CONDITIONS,
        initial_prefs=[],
        discord_id="123",
        siege_client=client,
    )
    # Pre-select id=1 on page 0 (Faction & League).
    view.selections["Faction & League"] = {1}

    interaction = _make_interaction()

    # Navigate to page 1.
    await view.go_next(interaction)

    # The edit_message call should have included an embed.
    call_kwargs = interaction.response.edit_message.call_args.kwargs
    assert "embed" in call_kwargs, "go_next did not pass embed= to edit_message"
    embed = call_kwargs["embed"]
    assert isinstance(embed, discord.Embed)
    # Even though we navigated away from page 0, the page-0 selection
    # should still appear in the embed.
    assert embed.description is not None
    assert "Only Barbarian Champions." in embed.description


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
