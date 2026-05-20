"""Discord-only display adaptations for post-conditions.

This module exists because Discord button labels render poorly when they
carry full canonical condition strings (~30-55 chars). The shortening
applied here is *Discord-UI only* — siege-web's API surface, the web
frontend, and any future bot or consumer continue to see canonical labels
unmodified.

The :data:`SHORT_LABELS` table is the single source of truth for this
mapping.  Adding a new condition to the siege-web catalog requires adding
a corresponding entry here before it can appear on a Discord button.

Unknown raw labels raise :exc:`KeyError` to fail loudly rather than
silently fall through to the wide canonical label — a silent fall-through
would re-introduce the wide-button visual problem invisibly when siege-web
ships a new condition.

The module-level :data:`_LENGTH_INVARIANT_CHECK` assertion (evaluated at
import time) guards the 25-char visual budget.  If a future edit adds an
overlong entry the assertion fires at collection time so the CI test run
fails immediately.

References:
    plan § 3.10 — canonical table and invariant specification.
    scripts/smoke_v1_button_grid.py _SHORT_LABELS (commit 87e2378) —
        cross-check data source.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

__all__ = ["SHORT_LABELS", "short_label"]

# ---------------------------------------------------------------------------
# Canonical short-label table
#
# Maps each siege-web canonical condition label → a compact Discord button
# label (≤ 25 chars).  Source-of-truth: plan § 3.10.
# Cross-checked against scripts/smoke_v1_button_grid.py _SHORT_LABELS at
# commit 87e2378.
#
# 36 entries: 19 Faction & League + 11 Role/Affinity/Rarity + 6 Effects & Other.
# ---------------------------------------------------------------------------

_RAW_SHORT_LABELS: dict[str, str] = {
    # --- Faction & League (19) ---
    "Only Champions from the Telerian League can be used.": ("Telerian League"),
    "Only Champions from the Gaellen Pact can be used.": "Gaellen Pact",
    "Only Champions from The Corrupted can be used.": "The Corrupted",
    "Only Champions from the Nyresan Union can be used.": "Nyresan Union",
    "Only Banner Lord Champions can be used.": "Banner Lords",
    "Only High Elves Champions can be used.": "High Elves",
    "Only Sacred Order Champions can be used.": "Sacred Order",
    "Only Barbarian Champions can be used.": "Barbarians",
    "Only Ogryn Tribe Champions can be used.": "Ogryn Tribe",
    "Only Lizardmen Champions can be used.": "Lizardmen",
    "Only Skinwalker Champions can be used.": "Skinwalkers",
    "Only Orc Champions can be used.": "Orcs",
    "Only Demonspawn Champions can be used.": "Demonspawn",
    "Only Undead Horde Champions can be used.": "Undead Horde",
    "Only Dark Elves Champions can be used.": "Dark Elves",
    "Only Knights Revenant Champions can be used.": "Knights Revenant",
    "Only Dwarves Champions can be used.": "Dwarves",
    "Only Shadowkin Champions can be used.": "Shadowkin",
    "Only Sylvan Watcher Champions can be used.": "Sylvan Watchers",
    # --- Role, Affinity, Rarity (11) ---
    "Only HP Champions can be used.": "HP",
    "Only DEF Champions can be used.": "DEF",
    "Only Support Champions can be used.": "Support",
    "Only ATK Champions can be used.": "ATK",
    "Only Void Champions can be used.": "Void",
    "Only Force Champions can be used.": "Force",
    "Only Magic Champions can be used.": "Magic",
    "Only Spirit Champions can be used.": "Spirit",
    "Only Legendary Champions can be used.": "Legendary",
    "Only Epic Champions can be used.": "Epic",
    "Only Rare Champions can be used.": "Rare",
    # --- Effects & Other (6) ---
    "All Champions are immune to Turn Meter reduction effects.": ("Immune: TM reduction"),
    "All Champions are immune to Turn Meter fill effects.": ("Immune: TM fill"),
    "All Champions are immune to cooldown increasing effects.": ("Immune: CD increase"),
    "All Champions are immune to cooldown decreasing effects.": ("Immune: CD decrease"),
    "All Champions are immune to [Sheep] debuffs.": "Immune: [Sheep]",
    "Champions cannot be revived.": "No revives",
}

#: Immutable public mapping of canonical condition label → short label.
#:
#: Consumers (the toggle-button view, build_summary_embed) import this
#: directly.  Declared as :class:`~types.MappingProxyType` to prevent
#: accidental mutation at runtime.
SHORT_LABELS: Mapping[str, str] = MappingProxyType(_RAW_SHORT_LABELS)

# ---------------------------------------------------------------------------
# Import-time invariant: every short label must fit the 25-char visual
# budget that allows ~5 buttons per row without Discord truncating the
# label text.
#
# This assertion fires at *collection* time (the first import), so a CI
# test run that adds an overlong entry fails immediately with a clear
# message rather than silently rendering truncated buttons in production.
# ---------------------------------------------------------------------------
assert max(len(v) for v in SHORT_LABELS.values()) <= 25, (
    "A short label in SHORT_LABELS exceeds the 25-char visual budget "
    "for Discord button labels. Shorten the offending entry or raise "
    "the budget intentionally after verifying 5-per-row rendering."
)


def short_label(condition: Mapping[str, Any]) -> str:
    """Return the Discord-button display label for a post-condition.

    The raw canonical label (``condition["label"]``) from siege-web is up
    to ~60 characters and renders poorly in a 5-button-per-row layout.
    This function maps each canonical label to a short form (≤ 25 chars)
    used only by mom-bot's Discord surface; other consumers (web,
    siege-web, future bots) continue to see the canonical label.

    Raises:
        KeyError: If the condition's canonical label has no mapping.
            This is loud-by-design — new catalog entries must be added to
            :data:`SHORT_LABELS` before they can appear on a Discord button.

    Args:
        condition: A post-condition dict with at minimum a ``"label"`` key
            holding the canonical condition string from siege-web.

    Returns:
        The short display string (≤ 25 chars) suitable for
        ``discord.ui.Button(label=...)``.
    """
    raw: str = condition["label"]
    try:
        return SHORT_LABELS[raw]
    except KeyError:
        raise KeyError(
            f"No short label mapping for canonical label {raw!r}. "
            f"Add an entry to SHORT_LABELS in discord_display.py "
            f"(see plan § 3.10 for the canonical table)."
        ) from None
