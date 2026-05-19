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
