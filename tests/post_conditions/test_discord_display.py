"""Tests for discord_display.short_label and the _SHORT_LABELS table.

Source-of-truth for the label table is the plan at:
  docs/superpowers/plans/2026-05-20-issue-145-button-grid.md § 3.10

The 36-entry table there is canonical; the smoke script's _SHORT_LABELS
dict at scripts/smoke_v1_button_grid.py (commit 87e2378) is a cross-check.
"""

from __future__ import annotations

import pytest

from mom_bot.post_conditions.discord_display import (
    SHORT_LABELS,
    short_label,
)


def test_short_label_returns_short_form_for_known_raw_label() -> None:
    """A canonical label maps to its short form.

    Exercises the Faction & League group: Sylvan Watcher → Sylvan Watchers.
    """
    condition = {
        "id": 42,
        "description": "Only Sylvan Watcher Champions can be used.",
        "condition_type": "faction",
    }
    assert short_label(condition) == "Sylvan Watchers"


def test_short_label_raises_keyerror_for_unknown_raw_label() -> None:
    """Unknown raw labels raise KeyError — silent fall-through is disallowed."""
    condition = {"id": 999, "description": "Some new condition not in the table."}
    with pytest.raises(KeyError, match="No short label mapping"):
        short_label(condition)


def test_all_short_labels_fit_within_25_chars() -> None:
    """Every entry in SHORT_LABELS is ≤ 25 chars (button-render budget)."""
    overlong = {raw: short for raw, short in SHORT_LABELS.items() if len(short) > 25}
    assert not overlong, f"Short labels exceeding 25 chars: {overlong}"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Faction & League — two entries to catch both ends of the group
        (
            "Only Champions from the Telerian League can be used.",
            "Telerian League",
        ),
        (
            "Only Sylvan Watcher Champions can be used.",
            "Sylvan Watchers",
        ),
        # Role, Affinity, Rarity — two entries
        ("Only HP Champions can be used.", "HP"),
        ("Only Legendary Champions can be used.", "Legendary"),
        # Effects & Other — two entries
        (
            "All Champions are immune to Turn Meter fill effects.",
            "Immune: TM fill",
        ),
        ("Champions cannot be revived.", "No revives"),
    ],
)
def test_short_label_covers_each_meta_group(raw: str, expected: str) -> None:
    """At least one entry from each meta-group resolves correctly.

    Catches table truncation if a future edit accidentally drops a section.
    The (raw, expected) pairs are cross-checked against plan § 3.10.
    """
    assert short_label({"description": raw}) == expected


def test_short_labels_table_covers_full_hardcoded_catalog() -> None:
    """Every label in the hardcoded catalog has a short-label mapping.

    The 36 raw labels below are inlined from the smoke script's
    _HARDCODED_CATALOG (scripts/smoke_v1_button_grid.py, commit 87e2378)
    and cross-checked against plan § 3.10.

    Prevents the failure mode where siege-web adds a condition, the catalog
    grows, and short_label raises KeyError at user-invocation time.
    This test is the canary — it fails before any user interaction is broken.

    Source-of-truth: plan § 3.10 (19 Faction & League + 11 Role/Affinity/
    Rarity + 6 Effects & Other = 36 total).
    """
    # 36 raw labels from _HARDCODED_CATALOG — see plan § 3.10.
    raw_labels = [
        # --- Faction & League (19) ---
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
        # --- Role, Affinity, Rarity (11) ---
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
        # --- Effects & Other (6) ---
        "All Champions are immune to Turn Meter reduction effects.",
        "All Champions are immune to Turn Meter fill effects.",
        "All Champions are immune to cooldown increasing effects.",
        "All Champions are immune to cooldown decreasing effects.",
        "All Champions are immune to [Sheep] debuffs.",
        "Champions cannot be revived.",
    ]

    assert len(raw_labels) == 36, f"Fixture must have 36 entries (19+11+6), got {len(raw_labels)}"
    assert len(SHORT_LABELS) == 36, f"SHORT_LABELS must have 36 entries, got {len(SHORT_LABELS)}"

    missing = [r for r in raw_labels if r not in SHORT_LABELS]
    assert not missing, f"Catalog labels missing from SHORT_LABELS: {missing}"
