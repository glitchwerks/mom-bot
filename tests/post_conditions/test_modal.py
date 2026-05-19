"""Tests for EditPreferencesModal in mom_bot.post_conditions.views.

Covers: CheckboxGroup wiring, title truncation, on_submit success path,
on_submit failure/rollback path, embed re-render on submit, and
accumulation of selections across sequential modal submits.

These tests use ``_simulate_modal_submit`` to inject ``CheckboxGroup._values``
directly, which mirrors the internal ``_handle_submit`` path at
``.venv/Lib/site-packages/discord/ui/checkbox.py:L262-L265``.  Discord sets
``_values`` at interaction time; simulating that injection is the only way to
exercise ``on_submit`` without a live Gateway connection.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord

from mom_bot.post_conditions.client import SiegeWebError
from mom_bot.post_conditions.modal_layout import ModalPage
from mom_bot.post_conditions.views import EditPreferencesModal

# ---------------------------------------------------------------------------
# Module-level constant: Discord modal title character limit.
# Source: .venv/Lib/site-packages/discord/ui/modal.py (docstring, L88).
# ---------------------------------------------------------------------------
_MODAL_TITLE_LIMIT = 45

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_COND_A1: dict[str, Any] = {
    "id": 1,
    "condition_type": "faction",
    "description": "Only Barbarian Champions.",
    "stronghold_level": 1,
}
_COND_A2: dict[str, Any] = {
    "id": 2,
    "condition_type": "league",
    "description": "Only Telerian League Champions.",
    "stronghold_level": 1,
}
_COND_B1: dict[str, Any] = {
    "id": 3,
    "condition_type": "role",
    "description": "Only HP Champions.",
    "stronghold_level": 1,
}
_COND_B2: dict[str, Any] = {
    "id": 4,
    "condition_type": "affinity",
    "description": "Only Void Champions.",
    "stronghold_level": 2,
}

# page_A covers ids 1, 2 (Faction & League sub-page)
_PAGE_A = ModalPage(
    label="Faction & League",
    conditions=[_COND_A1, _COND_A2],
)

# page_B covers ids 3, 4 (Role, Affinity, Rarity sub-page)
_PAGE_B = ModalPage(
    label="Role, Affinity, Rarity",
    conditions=[_COND_B1, _COND_B2],
)

# Full pages list used by build_summary_embed / _selections_to_meta_keyed.
# Shape: list[tuple[str, list[dict[str, Any]]]] matching group_by_meta output.
_PAGES: list[tuple[str, list[dict[str, Any]]]] = [
    ("Faction & League", [_COND_A1, _COND_A2]),
    ("Role, Affinity, Rarity", [_COND_B1, _COND_B2]),
]


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_fake_parent(
    selections: dict[int, bool] | None = None,
) -> MagicMock:
    """Return a fake parent_view with a ``selections`` dict attribute.

    Only ``selections`` is needed by ``EditPreferencesModal``; the view
    itself is not imported here to avoid a forward-reference error
    (``EditPreferencesView`` is implemented in Phase 3).

    Args:
        selections: Initial flat {condition_id: bool} mapping.
            Defaults to all False for the sample conditions.

    Returns:
        A :class:`unittest.mock.MagicMock` with a ``.selections`` attribute.
    """
    parent = MagicMock()
    parent.selections = selections if selections is not None else {}
    return parent


def _make_interaction() -> MagicMock:
    """Return a minimal fake discord.Interaction for modal tests.

    Returns:
        A :class:`unittest.mock.MagicMock` whose response.edit_message and
        response.send_message are :class:`unittest.mock.AsyncMock` instances.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_siege_client(*, fail: bool = False) -> MagicMock:
    """Return a fake SiegeWebClient.

    Args:
        fail: When ``True``, ``set_my_preferences`` raises
            :class:`~mom_bot.post_conditions.client.SiegeWebError`.

    Returns:
        A :class:`unittest.mock.MagicMock` with ``set_my_preferences``
        wired as an :class:`unittest.mock.AsyncMock`.
    """
    client = MagicMock()
    if fail:
        client.set_my_preferences = AsyncMock(
            side_effect=SiegeWebError("simulated failure")
        )
    else:
        client.set_my_preferences = AsyncMock(return_value=[])
    return client


def _simulate_modal_submit(
    modal: EditPreferencesModal,
    checked_ids: list[int],
) -> None:
    """Inject submitted values into the modal's CheckboxGroup, simulating Discord.

    Discord calls ``CheckboxGroup._handle_submit`` on interaction receipt,
    which writes ``data['values']`` into ``self._values``
    (``.venv/Lib/site-packages/discord/ui/checkbox.py:L262-L265``).
    We replicate that by setting ``_values`` directly, which is the only
    stable injection point short of a live Gateway connection.

    Args:
        modal: The :class:`EditPreferencesModal` instance to simulate a
            submit on.
        checked_ids: The condition IDs the user checked in the modal.
            These become the stringified values in ``CheckboxGroup._values``.
    """
    modal.group._values = [str(cid) for cid in checked_ids]


def _build_modal(
    page: ModalPage = _PAGE_A,
    selections: dict[int, bool] | None = None,
    discord_id: str = "42",
    siege_client: Any | None = None,
    pages: list[tuple[str, list[dict[str, Any]]]] | None = None,
    fail: bool = False,
) -> tuple[EditPreferencesModal, MagicMock]:
    """Construct an EditPreferencesModal and its fake parent view.

    Args:
        page: The :class:`~mom_bot.post_conditions.modal_layout.ModalPage`
            to pass to the modal.
        selections: Initial selections dict for the parent view.
        discord_id: Discord snowflake for the acting user (string).
        siege_client: Optional pre-built siege client mock.
        pages: Full pages list for embed rendering.
            Defaults to :data:`_PAGES`.
        fail: Whether ``set_my_preferences`` should raise.

    Returns:
        A ``(modal, parent_view)`` tuple.
    """
    if selections is None:
        selections = {}
    if siege_client is None:
        siege_client = _make_siege_client(fail=fail)
    if pages is None:
        pages = _PAGES

    parent = _make_fake_parent(selections)
    modal = EditPreferencesModal(
        page=page,
        parent_view=parent,
        siege_client=siege_client,
        discord_id=discord_id,
        pages=pages,
    )
    return modal, parent


# ---------------------------------------------------------------------------
# Constructor / wiring tests
# ---------------------------------------------------------------------------


def test_checkbox_group_option_labels_match_descriptions() -> None:
    """CheckboxGroup option labels equal the condition descriptions."""
    modal, _ = _build_modal()
    labels = [opt.label for opt in modal.group.options]
    expected = [c["description"] for c in _PAGE_A.conditions]
    assert labels == expected


def test_checkbox_group_option_values_are_stringified_ids() -> None:
    """CheckboxGroup option values are stringified condition IDs."""
    modal, _ = _build_modal()
    values = [opt.value for opt in modal.group.options]
    expected = [str(c["id"]) for c in _PAGE_A.conditions]
    assert values == expected


def test_checkbox_group_defaults_true_for_selected_ids() -> None:
    """Options whose IDs are True in selections have default=True."""
    # id=1 selected, id=2 not selected.
    selections = {1: True, 2: False}
    modal, _ = _build_modal(selections=selections)
    opts_by_value = {opt.value: opt for opt in modal.group.options}
    assert opts_by_value["1"].default is True
    assert opts_by_value["2"].default is False


def test_checkbox_group_default_false_when_id_absent_from_selections() -> None:
    """Options whose IDs are absent from selections have default=False."""
    modal, _ = _build_modal(selections={})
    for opt in modal.group.options:
        assert opt.default is False


def test_checkbox_group_min_values_is_zero() -> None:
    """CheckboxGroup min_values is 0 (allow deselecting everything)."""
    modal, _ = _build_modal()
    assert modal.group.min_values == 0


def test_checkbox_group_max_values_equals_option_count() -> None:
    """CheckboxGroup max_values equals the number of options on the page."""
    modal, _ = _build_modal()
    assert modal.group.max_values == len(_PAGE_A.conditions)


def test_modal_title_is_page_label() -> None:
    """Modal title equals the ModalPage label when ≤ 45 characters."""
    modal, _ = _build_modal(page=_PAGE_A)
    assert modal.title == _PAGE_A.label


def test_modal_title_truncated_to_45_chars() -> None:
    """Modal title is truncated to 45 characters when the label is longer."""
    long_label = "A" * 60
    page = ModalPage(label=long_label, conditions=[_COND_A1])
    modal, _ = _build_modal(page=page)
    assert len(modal.title) <= _MODAL_TITLE_LIMIT
    assert modal.title == long_label[:_MODAL_TITLE_LIMIT]


# ---------------------------------------------------------------------------
# on_submit — success path
# ---------------------------------------------------------------------------


async def test_on_submit_success_updates_sub_page_ids() -> None:
    """on_submit updates selections for sub-page IDs (checked = True, unchecked = False)."""
    # Start with id=2 selected; after submit with only id=1 checked,
    # id=1 should be True and id=2 should be False.
    selections = {1: False, 2: True}
    modal, parent = _build_modal(selections=selections)
    _simulate_modal_submit(modal, checked_ids=[1])

    await modal.on_submit(_make_interaction())

    assert parent.selections[1] is True
    assert parent.selections[2] is False


async def test_on_submit_success_does_not_touch_other_page_ids() -> None:
    """on_submit leaves IDs outside the sub-page untouched."""
    # ids 3 and 4 belong to page_B, not page_A.
    selections = {1: False, 2: False, 3: True, 4: True}
    modal, parent = _build_modal(page=_PAGE_A, selections=selections)
    _simulate_modal_submit(modal, checked_ids=[])

    await modal.on_submit(_make_interaction())

    assert parent.selections[3] is True
    assert parent.selections[4] is True


async def test_on_submit_success_calls_set_preferences_once() -> None:
    """on_submit calls set_my_preferences exactly once."""
    siege = _make_siege_client()
    modal, _ = _build_modal(selections={1: True, 2: False}, siege_client=siege)
    _simulate_modal_submit(modal, checked_ids=[1])

    await modal.on_submit(_make_interaction())

    siege.set_my_preferences.assert_awaited_once()


async def test_on_submit_success_passes_merged_id_list() -> None:
    """on_submit sends the full merged preference id list (all True ids)."""
    # page_A ids: 1, 2; page_B ids: 3, 4.
    # After submit: id=1 checked from page_A, id=3 already True from page_B.
    selections = {1: False, 2: False, 3: True, 4: False}
    siege = _make_siege_client()
    modal, _ = _build_modal(
        page=_PAGE_A, selections=selections, siege_client=siege
    )
    _simulate_modal_submit(modal, checked_ids=[1])

    await modal.on_submit(_make_interaction())

    _, kwargs = siege.set_my_preferences.call_args
    sent_ids = set(kwargs["ids"])
    assert sent_ids == {1, 3}


async def test_on_submit_success_calls_edit_message_with_view() -> None:
    """on_submit calls interaction.response.edit_message with view=parent."""
    modal, parent = _build_modal(selections={1: True, 2: False})
    _simulate_modal_submit(modal, checked_ids=[1])
    interaction = _make_interaction()

    await modal.on_submit(interaction)

    interaction.response.edit_message.assert_awaited_once()
    _, kwargs = interaction.response.edit_message.call_args
    assert kwargs.get("view") is parent


async def test_on_submit_success_passes_embed_to_edit_message() -> None:
    """on_submit calls edit_message with an embed (discord.Embed instance)."""
    modal, _ = _build_modal(selections={1: True, 2: False})
    _simulate_modal_submit(modal, checked_ids=[1])
    interaction = _make_interaction()

    await modal.on_submit(interaction)

    _, kwargs = interaction.response.edit_message.call_args
    assert isinstance(kwargs.get("embed"), discord.Embed)


# ---------------------------------------------------------------------------
# on_submit — failure / rollback path
# ---------------------------------------------------------------------------


async def test_on_submit_failure_sends_ephemeral_error() -> None:
    """on_submit sends an ephemeral error message when set_my_preferences raises."""
    modal, _ = _build_modal(selections={1: False}, fail=True)
    _simulate_modal_submit(modal, checked_ids=[1])
    interaction = _make_interaction()

    await modal.on_submit(interaction)

    interaction.response.send_message.assert_awaited_once()
    _, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


async def test_on_submit_failure_rolls_back_selections() -> None:
    """on_submit rolls back parent_view.selections to pre-submit state on failure."""
    original = {1: False, 2: True}
    modal, parent = _build_modal(selections=dict(original), fail=True)
    _simulate_modal_submit(modal, checked_ids=[1])

    await modal.on_submit(_make_interaction())

    assert parent.selections == original


async def test_on_submit_failure_does_not_call_edit_message() -> None:
    """on_submit does not call edit_message on set_my_preferences failure."""
    modal, _ = _build_modal(selections={1: False}, fail=True)
    _simulate_modal_submit(modal, checked_ids=[1])
    interaction = _make_interaction()

    await modal.on_submit(interaction)

    interaction.response.edit_message.assert_not_awaited()


async def test_on_submit_failure_does_not_propagate_exception() -> None:
    """on_submit catches SiegeWebError and does not re-raise it."""
    modal, _ = _build_modal(selections={1: False}, fail=True)
    _simulate_modal_submit(modal, checked_ids=[1])
    interaction = _make_interaction()

    # Should complete without raising.
    await modal.on_submit(interaction)


# ---------------------------------------------------------------------------
# Ported from test_views.py L550-L576:
# test_modal_submit_rerenders_summary_embed
# ---------------------------------------------------------------------------


async def test_modal_submit_rerenders_summary_embed() -> None:
    """Submitting a modal passes an embed reflecting the post-submit selections.

    This is the modal-architecture port of the old ``test_on_select_rerenders_embed``
    test (test_views.py:L550-L576): the ephemeral is refreshed with an embed
    whose description reflects the *new* state, not the pre-submit state.
    """
    # Start: id=1 not selected. After submit with id=1 checked the embed
    # should contain "Only Barbarian Champions." in its description.
    selections = {1: False, 2: False}
    modal, _ = _build_modal(page=_PAGE_A, selections=selections)
    _simulate_modal_submit(modal, checked_ids=[1])
    interaction = _make_interaction()

    await modal.on_submit(interaction)

    _, kwargs = interaction.response.edit_message.call_args
    embed: discord.Embed = kwargs["embed"]
    assert embed.description is not None
    assert "Only Barbarian Champions." in embed.description


# ---------------------------------------------------------------------------
# Ported from test_views.py L579-L606:
# test_sequential_modal_submits_accumulate_selections
# ---------------------------------------------------------------------------


async def test_sequential_modal_submits_accumulate_selections() -> None:
    """Selections from sequential modal submits accumulate in the parent view.

    This is the modal-architecture port of the old
    ``test_prev_next_preserves_embed_selections`` test
    (test_views.py:L579-L606): submitting modal A then modal B with different
    sub-page IDs results in the union of both checked-sets being sent to
    siege-web on the second call.
    """
    # Both pages share the same parent_view.
    shared_selections: dict[int, bool] = {1: False, 2: False, 3: False, 4: False}
    parent = _make_fake_parent(shared_selections)
    siege = _make_siege_client()

    # Modal A covers page_A (ids 1, 2). User checks id=1.
    modal_a = EditPreferencesModal(
        page=_PAGE_A,
        parent_view=parent,
        siege_client=siege,
        discord_id="42",
        pages=_PAGES,
    )
    _simulate_modal_submit(modal_a, checked_ids=[1])
    await modal_a.on_submit(_make_interaction())

    # After first submit: parent.selections[1] == True, others unchanged.
    assert parent.selections[1] is True

    # Modal B covers page_B (ids 3, 4). User checks id=3.
    modal_b = EditPreferencesModal(
        page=_PAGE_B,
        parent_view=parent,
        siege_client=siege,
        discord_id="42",
        pages=_PAGES,
    )
    _simulate_modal_submit(modal_b, checked_ids=[3])
    await modal_b.on_submit(_make_interaction())

    # Final set_my_preferences call must include ids from both submits.
    final_call = siege.set_my_preferences.call_args_list[-1]
    _, kwargs = final_call
    sent_ids = set(kwargs["ids"])
    assert sent_ids == {1, 3}


# ---------------------------------------------------------------------------
# Regression: discord_id type guard
# ---------------------------------------------------------------------------


async def test_on_submit_passes_string_discord_id_to_client() -> None:
    """set_my_preferences receives a str discord_id, not an int.

    Regression guard for the bug where EditPreferencesModal accepted
    ``discord_id: int`` but ``SiegeWebClient.set_my_preferences`` expects
    ``discord_id: str``.  AsyncMock accepts any type, so only an explicit
    isinstance check catches the mismatch.
    """
    expected_id = "99"
    siege = _make_siege_client()
    modal, _ = _build_modal(
        selections={1: True, 2: False},
        discord_id=expected_id,
        siege_client=siege,
    )
    _simulate_modal_submit(modal, checked_ids=[1])

    await modal.on_submit(_make_interaction())

    siege.set_my_preferences.assert_awaited_once()
    call_args = siege.set_my_preferences.call_args
    actual_discord_id = call_args.args[0]
    assert isinstance(actual_discord_id, str), (
        f"set_my_preferences expected str discord_id, got {type(actual_discord_id)}"
    )
    assert actual_discord_id == expected_id
