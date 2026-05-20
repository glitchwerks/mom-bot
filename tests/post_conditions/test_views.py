"""Tests for mom_bot.post_conditions.views.

Covers: page navigation preserving selections, pre-population from initial
GET state, Commit flattening, Cancel discarding, using a fake interaction,
and the live-updating selection-summary embed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from mom_bot.post_conditions.views import build_summary_embed

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
# PostConditionsGridView — Phase 2 tests
# ---------------------------------------------------------------------------

# Minimal catalog with real short-label mappings for tests that exercise
# the button-label path.  Each condition carries both "label" (required
# by short_label) and "description" (required by build_summary_embed).
_GRID_CATALOG_FACTION: list[dict[str, Any]] = [
    {
        "id": 1,
        "description": "Only Banner Lord Champions can be used.",
        "condition_type": "faction",
    },
    {
        "id": 2,
        "description": "Only Sylvan Watcher Champions can be used.",
        "condition_type": "faction",
    },
]

_GRID_CATALOG_ROLE: list[dict[str, Any]] = [
    {
        "id": 3,
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
        {"id": i, "description": lbl, "condition_type": "faction"}
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
        {"id": i, "description": lbl, "condition_type": "role"}
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
        {"id": i, "description": lbl, "condition_type": "effect"}
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


# ---------------------------------------------------------------------------
# PostConditionsGridView — Phase 3: Save/Cancel callback tests (mocked client)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_callback_puts_selected_ids_and_strips_view() -> None:
    """SaveButton aggregates ON selections and PUTs them via the client."""
    from mom_bot.post_conditions.views import PostConditionsGridView, SaveButton

    # "description" is required by short_label() — matches siege-web API.
    catalog = [
        {
            "id": 1,
            "condition_type": "faction",
            "description": "Only Banner Lord Champions can be used.",
        },
        {
            "id": 2,
            "condition_type": "faction",
            "description": "Only High Elves Champions can be used.",
        },
        {
            "id": 3,
            "condition_type": "role",
            "description": "Only HP Champions can be used.",
        },
    ]
    siege_client = MagicMock()
    siege_client.set_my_preferences = AsyncMock(return_value=[])

    view = PostConditionsGridView(
        catalog=catalog, preferences=[1], discord_id="42", siege_client=siege_client
    )
    # User toggles id 3 on, id 1 off (staged).
    view._selections[1] = False
    view._selections[3] = True

    save_btn = next(c for c in view.children if isinstance(c, SaveButton))

    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.edit_message = AsyncMock()
    save_btn._view = view  # discord.py sets this on dispatch; emulate.

    await save_btn.callback(interaction)

    siege_client.set_my_preferences.assert_awaited_once_with(discord_id="42", ids=[3])
    interaction.response.edit_message.assert_awaited_once()
    # view=None in the call → buttons stripped.
    _, kwargs = interaction.response.edit_message.call_args
    assert kwargs["view"] is None


@pytest.mark.asyncio
async def test_cancel_callback_makes_no_client_call_and_strips_view() -> None:
    """CancelButton does not touch the client."""
    from mom_bot.post_conditions.views import CancelButton, PostConditionsGridView

    # "description" is required by short_label() — matches siege-web API.
    catalog = [
        {
            "id": 1,
            "condition_type": "faction",
            "description": "Only Banner Lord Champions can be used.",
        }
    ]
    siege_client = MagicMock()
    siege_client.set_my_preferences = AsyncMock()

    view = PostConditionsGridView(
        catalog=catalog, preferences=[1], discord_id="42", siege_client=siege_client
    )
    cancel_btn = next(c for c in view.children if isinstance(c, CancelButton))

    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.edit_message = AsyncMock()
    cancel_btn._view = view

    await cancel_btn.callback(interaction)

    siege_client.set_my_preferences.assert_not_awaited()
    interaction.response.edit_message.assert_awaited_once()
    _, kwargs = interaction.response.edit_message.call_args
    assert kwargs["view"] is None
    assert kwargs["embed"] is None


@pytest.mark.asyncio
async def test_save_callback_handles_siege_web_error() -> None:
    """SiegeWebError → user gets a retry message; view is NOT stripped."""
    from mom_bot.post_conditions.client import SiegeWebError
    from mom_bot.post_conditions.views import PostConditionsGridView, SaveButton

    # "description" is required by short_label() — matches siege-web API.
    catalog = [
        {
            "id": 1,
            "condition_type": "faction",
            "description": "Only Banner Lord Champions can be used.",
        }
    ]
    siege_client = MagicMock()
    siege_client.set_my_preferences = AsyncMock(side_effect=SiegeWebError("boom"))

    view = PostConditionsGridView(
        catalog=catalog, preferences=[1], discord_id="42", siege_client=siege_client
    )
    save_btn = next(c for c in view.children if isinstance(c, SaveButton))

    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    save_btn._view = view

    await save_btn.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    interaction.response.edit_message.assert_not_awaited()
