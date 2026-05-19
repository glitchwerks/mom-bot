"""Modal-layout helpers for post-condition preference selection.

Provides :class:`ModalPage` (a lightweight typed container) and
:func:`split_meta_for_modals`, which converts a flat list of
PostConditionResponse dicts into Discord-modal-sized pages of at most
10 conditions each, grouped by meta-category.

This module is intentionally discord-free: it performs pure data
transformation and may be imported without a Discord runtime present.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from mom_bot.post_conditions.grouping import group_by_meta

# Maximum number of conditions that fit inside a single Discord modal.
# Enforced by CheckboxGroup.append_option (raises ValueError at 11th option).
# Source: .venv/Lib/site-packages/discord/ui/checkbox.py:L238-L239.
_PAGE_SIZE: int = 10


class ModalPage(NamedTuple):
    """A single page of conditions destined for a Discord modal.

    Attributes:
        label: Human-readable page title, e.g. ``"Faction & League"`` or
            ``"Role, Affinity, Rarity (2/3)"`` when a meta-group spans
            multiple pages.
        conditions: Ordered list of PostConditionResponse dicts for this
            page.  Each dict contains at minimum ``id`` (int),
            ``condition_type`` (str), and ``description`` (str).
    """

    label: str
    conditions: list[dict[str, Any]]


def split_meta_for_modals(
    conditions: list[dict[str, Any]],
) -> list[ModalPage]:
    """Split conditions into Discord-modal-sized pages grouped by meta-category.

    Each meta-category produced by :func:`~.grouping.group_by_meta` is
    sorted deterministically (by ``condition_type`` ascending, then ``id``
    ascending) and chunked into slices of at most :data:`_PAGE_SIZE`
    conditions.

    - A single chunk keeps the bare meta-label (e.g. ``"Faction & League"``).
    - Multiple chunks are labelled ``"<meta> (i/N)"`` for i in 1..N.

    Empty meta-categories are already suppressed by :func:`group_by_meta`
    and therefore produce no :class:`ModalPage` output.

    Args:
        conditions: A flat list of PostConditionResponse dicts.  Each dict
            must contain at minimum ``"condition_type"`` (str) and ``"id"``
            (int) keys.

    Returns:
        An ordered list of :class:`ModalPage` objects.  Page order follows
        the canonical :data:`~.grouping.META_GROUPS` label order; within a
        meta-category, pages appear in chunk order (1, 2, …, N).
    """
    pages: list[ModalPage] = []

    for meta_label, conds in group_by_meta(conditions):
        # Deterministic ordering: (condition_type, id) ascending.
        sorted_conds = sorted(
            conds,
            key=lambda c: (str(c.get("condition_type", "")), int(c.get("id", 0))),
        )

        # Chunk into slices of _PAGE_SIZE.
        chunks: list[list[dict[str, Any]]] = [
            sorted_conds[start : start + _PAGE_SIZE]
            for start in range(0, len(sorted_conds), _PAGE_SIZE)
        ]

        n = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            label = meta_label if n == 1 else f"{meta_label} ({i}/{n})"
            pages.append(ModalPage(label=label, conditions=chunk))

    return pages
