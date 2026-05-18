"""Tests for mom_bot.post_conditions.grouping.

Covers META_GROUPS ordering, empty meta-group skipping, and the
group_by_meta helper.
"""

from __future__ import annotations

import pytest

from mom_bot.post_conditions.grouping import META_GROUPS, group_by_meta

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_CONDITIONS: list[dict[str, object]] = [
    {
        "id": 5,
        "description": "Only HP Champions can be used.",
        "stronghold_level": 1,
        "condition_type": "role",
    },
    {
        "id": 12,
        "description": "Only Barbarian Champions can be used.",
        "stronghold_level": 1,
        "condition_type": "faction",
    },
    {
        "id": 17,
        "description": "All Champions immune to Turn Meter reduction.",
        "stronghold_level": 1,
        "condition_type": "effect",
    },
    {
        "id": 19,
        "description": "Only Void Champions can be used.",
        "stronghold_level": 2,
        "condition_type": "affinity",
    },
    {
        "id": 22,
        "description": "Only Telerian League Champions can be used.",
        "stronghold_level": 1,
        "condition_type": "league",
    },
]


# ---------------------------------------------------------------------------
# META_GROUPS structure tests
# ---------------------------------------------------------------------------


def test_meta_groups_has_three_entries() -> None:
    """META_GROUPS must define exactly three meta-categories."""
    assert len(META_GROUPS) == 3


def test_meta_groups_labels() -> None:
    """META_GROUPS labels must match the spec exactly."""
    labels = [label for label, _ in META_GROUPS]
    assert labels == ["Faction & League", "Role, Affinity, Rarity", "Effects & Other"]


def test_meta_groups_faction_league_contains_correct_types() -> None:
    """'Faction & League' meta must map to faction and league condition types."""
    _, types = META_GROUPS[0]
    assert types == ["faction", "league"]


def test_meta_groups_role_affinity_rarity_contains_correct_types() -> None:
    """'Role, Affinity, Rarity' must map to role, affinity, rarity."""
    _, types = META_GROUPS[1]
    assert types == ["role", "affinity", "rarity"]


def test_meta_groups_effects_other_contains_correct_types() -> None:
    """'Effects & Other' must map to effect and other."""
    _, types = META_GROUPS[2]
    assert types == ["effect", "other"]


def test_meta_groups_covers_all_seven_condition_types() -> None:
    """Every condition_type from the closed enum must appear in META_GROUPS."""
    all_types: list[str] = []
    for _, types in META_GROUPS:
        all_types.extend(types)
    expected = {"role", "affinity", "faction", "league", "rarity", "effect", "other"}
    assert set(all_types) == expected


# ---------------------------------------------------------------------------
# group_by_meta tests
# ---------------------------------------------------------------------------


def test_group_by_meta_preserves_meta_groups_order() -> None:
    """group_by_meta must return groups in META_GROUPS label order."""
    result = group_by_meta(_SAMPLE_CONDITIONS)
    returned_labels = [label for label, _ in result]
    # Sample has role, faction, effect, affinity, league — all three metas populated.
    assert returned_labels == ["Faction & League", "Role, Affinity, Rarity", "Effects & Other"]


def test_group_by_meta_skips_empty_meta_groups() -> None:
    """group_by_meta omits meta-groups with no matching conditions."""
    # Only role conditions — 'Faction & League' and 'Effects & Other' are empty.
    role_only = [
        {
            "id": 1,
            "description": "Only ATK Champions.",
            "stronghold_level": 1,
            "condition_type": "role",
        }
    ]
    result = group_by_meta(role_only)
    labels = [label for label, _ in result]
    assert labels == ["Role, Affinity, Rarity"]


def test_group_by_meta_conditions_placed_in_correct_meta() -> None:
    """Conditions appear under the meta-group matching their condition_type."""
    result = group_by_meta(_SAMPLE_CONDITIONS)
    result_map = {label: conditions for label, conditions in result}

    # faction (id=12) and league (id=22) → Faction & League
    fl_ids = {c["id"] for c in result_map["Faction & League"]}
    assert fl_ids == {12, 22}

    # role (id=5) and affinity (id=19) → Role, Affinity, Rarity
    rar_ids = {c["id"] for c in result_map["Role, Affinity, Rarity"]}
    assert rar_ids == {5, 19}

    # effect (id=17) → Effects & Other
    eo_ids = {c["id"] for c in result_map["Effects & Other"]}
    assert eo_ids == {17}


def test_group_by_meta_returns_empty_list_for_no_conditions() -> None:
    """group_by_meta on an empty list returns an empty list."""
    assert group_by_meta([]) == []


def test_group_by_meta_returns_list_of_tuples() -> None:
    """group_by_meta return type is list[tuple[str, list[dict]]]."""
    result = group_by_meta(_SAMPLE_CONDITIONS)
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, tuple)
        assert len(item) == 2
        label, conditions = item
        assert isinstance(label, str)
        assert isinstance(conditions, list)


def test_group_by_meta_preserves_all_condition_fields() -> None:
    """Each condition dict retains all original fields (id, description, etc.)."""
    result = group_by_meta(_SAMPLE_CONDITIONS[:1])
    # First item: faction (id=12). After skipping role-only and effect-only sample,
    # use a faction-only sample.
    faction_only = [
        {
            "id": 12,
            "description": "Only Barbarian Champions can be used.",
            "stronghold_level": 1,
            "condition_type": "faction",
        }
    ]
    result = group_by_meta(faction_only)
    _, conditions = result[0]
    c = conditions[0]
    assert c["id"] == 12
    assert c["description"] == "Only Barbarian Champions can be used."
    assert c["stronghold_level"] == 1
    assert c["condition_type"] == "faction"


@pytest.mark.parametrize(
    "condition_type,expected_meta",
    [
        ("faction", "Faction & League"),
        ("league", "Faction & League"),
        ("role", "Role, Affinity, Rarity"),
        ("affinity", "Role, Affinity, Rarity"),
        ("rarity", "Role, Affinity, Rarity"),
        ("effect", "Effects & Other"),
        ("other", "Effects & Other"),
    ],
)
def test_each_condition_type_maps_to_correct_meta(condition_type: str, expected_meta: str) -> None:
    """Each condition_type maps to exactly the expected meta-group label."""
    cond = [
        {
            "id": 1,
            "description": "Test.",
            "stronghold_level": 1,
            "condition_type": condition_type,
        }
    ]
    result = group_by_meta(cond)
    assert len(result) == 1
    label, _ = result[0]
    assert label == expected_meta
