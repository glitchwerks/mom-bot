"""Grid-layout helpers for post-condition preference selection.

Provides :class:`GridPage` and :func:`split_by_meta_group`, which
converts a flat list of PostConditionResponse dicts into pages, one
outer chunk per ``META_GROUPS`` entry, with sub-pagination when a
group exceeds ``page_size``.

Per-meta-group pagination (§ 3.1 of the issue #145 plan): pages from
different meta-groups are never merged, even when there is unused
capacity on the prior page. The ``page_size`` parameter is the
*sub-page cap* within a meta-group, not a chunking boundary across
groups.

Page-size 20 matches the legacy-View 5-rows × 5-buttons cap minus one
reserved nav row of 4 buttons (see issue #145 plan §§ 2.7, 3.1).

This module is intentionally discord-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mom_bot.post_conditions.grouping import group_by_meta

_PAGE_SIZE: int = 20


@dataclass(frozen=True)
class GridPage:
    """A single page of conditions for the button-grid view.

    Each page belongs to exactly one meta-group. When a meta-group
    sub-paginates (more conditions than ``page_size``), consecutive
    ``GridPage`` objects share the same ``meta_label`` but carry
    distinct ``(i/N)`` suffixes in ``label``. Single-page groups have
    ``label == meta_label``.

    Attributes:
        label: Human-readable page title, e.g. ``"Faction & League"``
            or ``"Effects & Other (2/2)"`` when a meta-group
            sub-paginates. Used as the embed / page heading.
        meta_label: The unqualified ``META_GROUPS`` label with no
            ``"(i/N)"`` suffix (e.g. ``"Faction & League"`` for all
            sub-pages of that group). Useful for grouping sub-pages
            back into a single summary section.
        conditions: Ordered list of PostConditionResponse dicts for
            this page. Each dict contains at minimum ``id`` (int),
            ``condition_type`` (str), and ``description`` (str).
    """

    label: str
    meta_label: str
    conditions: list[dict[str, Any]]


def split_by_meta_group(
    conditions: list[dict[str, Any]],
    page_size: int = _PAGE_SIZE,
) -> list[GridPage]:
    """Group conditions by META_GROUP, then sub-paginate within each group.

    Iterates ``META_GROUPS`` in canonical order. For each meta-group
    with one or more matching conditions, emits
    ``ceil(len(group) / page_size)`` consecutive :class:`GridPage`
    objects. Pages from different meta-groups are never merged.

    Each GridPage's ``conditions`` list is sorted by
    ``(condition_type, id)`` ascending for deterministic ordering.

    Args:
        conditions: Flat list of PostConditionResponse dicts. Each
            dict must contain at minimum ``"condition_type"`` (str)
            and ``"id"`` (int) keys. Unknown condition types are
            silently ignored (delegated to
            :func:`~.grouping.group_by_meta`).
        page_size: Sub-pagination cap within a meta-group. Defaults
            to 20, the button capacity of a single legacy
            ``discord.ui.View`` page (5 rows × 5 buttons − 1 nav
            row, per issue #145 plan § 2.7).

    Returns:
        Ordered list of :class:`GridPage` objects. Empty if the input
        is empty or no condition matches any known meta-group. Page
        order follows the canonical ``META_GROUPS`` label order;
        within a meta-group, sub-pages appear in chunk order
        (1, 2, …, N). Single-page groups have ``label == meta_label``;
        multi-page groups carry an ``(i/N)`` suffix on ``label``.
    """
    pages: list[GridPage] = []

    for meta_label, conds in group_by_meta(conditions):
        sorted_conds = sorted(
            conds,
            key=lambda c: (str(c["condition_type"]), int(c["id"])),
        )
        chunks: list[list[dict[str, Any]]] = [
            sorted_conds[start : start + page_size]
            for start in range(0, len(sorted_conds), page_size)
        ]
        n = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            label = meta_label if n == 1 else f"{meta_label} ({i}/{n})"
            pages.append(GridPage(label=label, meta_label=meta_label, conditions=chunk))

    return pages
