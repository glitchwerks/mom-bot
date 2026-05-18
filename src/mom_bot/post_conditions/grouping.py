"""Meta-category grouping helpers for post-condition preferences.

Defines the canonical ``META_GROUPS`` ordering that maps human-readable
meta-category labels to their constituent ``condition_type`` values, and
provides :func:`group_by_meta` for sorting an arbitrary list of
PostConditionResponse dicts into that order.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# META_GROUPS — canonical label → condition_type mapping
# ---------------------------------------------------------------------------

#: Ordered list of ``(label, [condition_type, ...])`` pairs.
#:
#: The order determines the page order in the ``/post-conditions-set`` view
#: and the display order in ``/post-conditions`` and ``/post-conditions-get``.
#: Covers all seven closed ``condition_type`` values enforced by the
#: siege-web CHECK constraint.
META_GROUPS: list[tuple[str, list[str]]] = [
    ("Faction & League", ["faction", "league"]),
    ("Role, Affinity, Rarity", ["role", "affinity", "rarity"]),
    ("Effects & Other", ["effect", "other"]),
]

# Build a fast lookup: condition_type → meta label.
_TYPE_TO_META: dict[str, str] = {ct: label for label, types in META_GROUPS for ct in types}


def group_by_meta(
    conditions: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group a flat list of PostConditionResponse dicts by meta-category.

    Conditions are partitioned into meta-groups in ``META_GROUPS`` order.
    Meta-groups with no matching conditions are omitted from the output.

    Args:
        conditions: A list of PostConditionResponse dicts, each containing
            at minimum a ``"condition_type"`` key.

    Returns:
        An ordered list of ``(meta_label, [condition, ...])`` tuples.
        Only non-empty meta-groups are included.  The label order matches
        :data:`META_GROUPS`.
    """
    buckets: dict[str, list[dict[str, Any]]] = {label: [] for label, _ in META_GROUPS}

    for cond in conditions:
        ct: str = str(cond.get("condition_type", ""))
        meta_label = _TYPE_TO_META.get(ct)
        if meta_label is not None:
            buckets[meta_label].append(cond)

    return [(label, buckets[label]) for label, _ in META_GROUPS if buckets[label]]
