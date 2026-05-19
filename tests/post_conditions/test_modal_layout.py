"""Tests for mom_bot.post_conditions.modal_layout.

Covers ModalPage construction, split_meta_for_modals chunking behaviour,
deterministic ordering, and empty-meta-group suppression.
"""

from __future__ import annotations

from mom_bot.post_conditions.modal_layout import ModalPage, split_meta_for_modals

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ROLE = "role"
_FACTION = "faction"


def _make_cond(
    cond_id: int,
    condition_type: str,
    description: str = "Test condition.",
) -> dict[str, object]:
    """Build a minimal PostConditionResponse-shaped dict."""
    return {
        "id": cond_id,
        "condition_type": condition_type,
        "description": description,
        "stronghold_level": 1,
    }


# ---------------------------------------------------------------------------
# ModalPage shape tests
# ---------------------------------------------------------------------------


def test_modal_page_has_label_and_conditions() -> None:
    """ModalPage must expose .label (str) and .conditions (list)."""
    page = ModalPage(label="Faction & League", conditions=[])
    assert page.label == "Faction & League"
    assert page.conditions == []


# ---------------------------------------------------------------------------
# Single chunk (<=10 conditions)
# ---------------------------------------------------------------------------


def test_single_chunk_produces_one_modal_page_per_meta() -> None:
    """Meta-page with <=10 conditions yields exactly one ModalPage, label unchanged."""
    # Two role conditions → 'Role, Affinity, Rarity' meta-group (<=10).
    conditions = [_make_cond(i, _ROLE) for i in range(1, 5)]
    pages = split_meta_for_modals(conditions)

    assert len(pages) == 1
    page = pages[0]
    assert page.label == "Role, Affinity, Rarity"
    assert len(page.conditions) == 4


def test_single_chunk_label_has_no_fraction_suffix() -> None:
    """Single-chunk ModalPage label must not contain '(' or '/' characters."""
    conditions = [_make_cond(i, _FACTION) for i in range(1, 8)]
    pages = split_meta_for_modals(conditions)

    assert len(pages) == 1
    assert "(" not in pages[0].label
    assert "/" not in pages[0].label


def test_exactly_ten_conditions_yields_one_page() -> None:
    """Exactly 10 conditions in a meta-group must produce one page (boundary)."""
    conditions = [_make_cond(i, _ROLE) for i in range(1, 11)]
    pages = split_meta_for_modals(conditions)

    assert len(pages) == 1
    assert len(pages[0].conditions) == 10


# ---------------------------------------------------------------------------
# Split (>10 conditions)
# ---------------------------------------------------------------------------


def test_twenty_five_conditions_split_into_three_pages() -> None:
    """25 conditions in one meta-group → 3 ModalPages sized 10/10/5."""
    conditions = [_make_cond(i, _ROLE) for i in range(1, 26)]
    pages = split_meta_for_modals(conditions)

    assert len(pages) == 3
    assert len(pages[0].conditions) == 10
    assert len(pages[1].conditions) == 10
    assert len(pages[2].conditions) == 5


def test_split_labels_carry_fraction_suffix() -> None:
    """Split pages must be labelled '<meta> (i/N)' for i in 1..N."""
    conditions = [_make_cond(i, _ROLE) for i in range(1, 26)]
    pages = split_meta_for_modals(conditions)

    assert pages[0].label == "Role, Affinity, Rarity (1/3)"
    assert pages[1].label == "Role, Affinity, Rarity (2/3)"
    assert pages[2].label == "Role, Affinity, Rarity (3/3)"


def test_eleven_conditions_split_into_two_pages() -> None:
    """11 conditions → 2 pages sized 10/1 (boundary just above the limit)."""
    conditions = [_make_cond(i, _FACTION) for i in range(1, 12)]
    pages = split_meta_for_modals(conditions)

    assert len(pages) == 2
    assert len(pages[0].conditions) == 10
    assert len(pages[1].conditions) == 1
    assert pages[0].label == "Faction & League (1/2)"
    assert pages[1].label == "Faction & League (2/2)"


# ---------------------------------------------------------------------------
# Empty meta-group → no ModalPage
# ---------------------------------------------------------------------------


def test_empty_meta_group_produces_no_modal_page() -> None:
    """A meta-group with no matching conditions must not appear in output."""
    # Only 'faction' conditions → 'Faction & League' populated;
    # 'Role, Affinity, Rarity' and 'Effects & Other' are empty and must be absent.
    conditions = [_make_cond(i, _FACTION) for i in range(1, 4)]
    pages = split_meta_for_modals(conditions)

    labels = [p.label for p in pages]
    assert all("Role, Affinity, Rarity" not in lbl for lbl in labels)
    assert all("Effects & Other" not in lbl for lbl in labels)


def test_empty_input_produces_no_pages() -> None:
    """An empty condition list must yield an empty list of ModalPages."""
    assert split_meta_for_modals([]) == []


# ---------------------------------------------------------------------------
# Deterministic ordering across catalog refreshes
# ---------------------------------------------------------------------------


def test_sort_is_deterministic_across_input_orderings() -> None:
    """Same conditions in different input order must yield identical ModalPages."""
    # 12 role conditions to force a split (so ordering within chunks matters).
    ordered = [_make_cond(i, _ROLE) for i in range(1, 13)]
    shuffled = list(reversed(ordered))

    pages_a = split_meta_for_modals(ordered)
    pages_b = split_meta_for_modals(shuffled)

    assert pages_a == pages_b


def test_sort_is_by_condition_type_then_id() -> None:
    """Conditions within a page must be sorted by (condition_type, id)."""
    # Mix role (types r) and affinity (type a) — both map to same meta-group.
    # Affinity sorts before role alphabetically; within each type, ascending id.
    conditions = [
        _make_cond(5, "role"),
        _make_cond(2, "affinity"),
        _make_cond(1, "role"),
        _make_cond(10, "affinity"),
    ]
    pages = split_meta_for_modals(conditions)

    assert len(pages) == 1
    ids = [(c["condition_type"], c["id"]) for c in pages[0].conditions]
    assert ids == [("affinity", 2), ("affinity", 10), ("role", 1), ("role", 5)]


# ---------------------------------------------------------------------------
# Multiple meta-groups in same call
# ---------------------------------------------------------------------------


def test_multiple_meta_groups_each_chunk_independently() -> None:
    """Two distinct meta-groups are each chunked independently."""
    # 15 faction conditions → 'Faction & League' splits into 2 pages.
    # 3 role conditions → 'Role, Affinity, Rarity' stays as 1 page.
    faction_conds = [_make_cond(i, _FACTION) for i in range(1, 16)]
    role_conds = [_make_cond(100 + i, _ROLE) for i in range(1, 4)]
    pages = split_meta_for_modals(faction_conds + role_conds)

    fl_pages = [p for p in pages if "Faction" in p.label]
    rar_pages = [p for p in pages if "Role" in p.label]

    assert len(fl_pages) == 2
    assert len(rar_pages) == 1
    assert fl_pages[0].label == "Faction & League (1/2)"
    assert fl_pages[1].label == "Faction & League (2/2)"
    assert rar_pages[0].label == "Role, Affinity, Rarity"
