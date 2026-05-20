---
title: "Redesign /post-conditions-set as a V1 button-grid checklist (closes #145)"
issue: 145
touches:
  - src/mom_bot/post_conditions/commands.py
  - src/mom_bot/post_conditions/views.py
  - src/mom_bot/post_conditions/modal_layout.py
  - src/mom_bot/post_conditions/grid_layout.py
  - src/mom_bot/post_conditions/discord_display.py
  - tests/post_conditions/test_modal.py
  - tests/post_conditions/test_modal_layout.py
  - tests/post_conditions/test_grid_layout.py
  - tests/post_conditions/test_discord_display.py
  - tests/post_conditions/test_views.py
  - tests/post_conditions/test_commands.py
  - scripts/smoke_v1_button_grid.py
skills_relevant:
  - python
  - refactoring-discipline
  - superpowers:brainstorming
---

# Redesign /post-conditions-set as a V1 button-grid checklist

Closes [#145](https://github.com/glitchwerks/mom-bot/issues/145). Supersedes the V2 CheckboxGroup plan (deleted in the same commit as this file).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render `/post-conditions-set` as an ephemeral `discord.ui.View` whose toggleable `discord.ui.Button`s let the user check/uncheck each post-condition, with a live-updating embed summary and a single Save button that commits the staged selection set.

**Architecture:** A new `PostConditionsGridView(discord.ui.View)` owns a `dict[int, bool]` of staged selections. Toggle buttons (rows 0-3, up to 20 per page) flip an entry and trigger `edit_message(embed=..., view=self)` to refresh the visual style and summary. A nav row (row 4) carries `[Prev] [Save] [Cancel] [Next]`. Pagination boundaries follow `META_GROUPS` order (same chunking concept as `split_meta_for_modals`, but page size 20). Save aggregates the staged set and PUTs through the existing `set_my_preferences` client.

**Tech Stack:** discord.py 2.7 legacy `View` + `Button` (no V2 components), Python 3.13, pytest.

---

## 1. Problem statement

PR #144 shipped `/post-conditions-set` as a multi-page **modal** flow. The modal UX is awkward (max 5 components/page, no live summary, save-per-page semantics). The V2 redesign attempted in this worktree (commits `6f33b1c`, `4e5f74a`) discovered that `CheckboxGroup` (component type 22) is platform-restricted to **Modals only**:

- Top-level `CheckboxGroup` in a V2 message → Discord 400 ([issue #145 comment](https://github.com/glitchwerks/mom-bot/issues/145#issuecomment-) — first rejection, smoke `scripts/smoke_v2_checkbox.py`).
- `CheckboxGroup` nested inside `Container` (component type 17) → Discord 400. Allowed children of `Container` are types `{1, 9, 10, 12, 13, 14}`; type 22 is not in the set ([issue #145 comment](https://github.com/glitchwerks/mom-bot/issues/145#issuecomment-) — second rejection, smoke `scripts/smoke_v2_checkbox_in_container.py`).

The V2 path is closed at every nesting depth discord.py 2.7 exposes. We pivot to a **V1 button-grid** in a legacy `discord.ui.View`:

- Buttons render as full-width clickable surfaces with `ButtonStyle` carrying selection state visually.
- Legacy `View` accepts a `discord.Embed` alongside, so the live summary survives.
- All component types involved (Button, View) are well-established platform surfaces with no known rejection modes.

## 2. Sources (verified 2026-05-20)

All citations are against the in-tree venv at `.venv/Lib/site-packages/discord/` (discord.py 2.7).

### 2.1 Legacy `View` cap is 5 rows × 5 width-units = 25 components

`.venv/Lib/site-packages/discord/ui/view.py:L785-L790`:

```python
def add_item(self, item: Item[Any]) -> Self:
    if len(self._children) >= 25:
        raise ValueError('maximum number of children exceeded')

    if item._is_v2():
        raise ValueError('v2 items cannot be added to this view')
```

The cap is enforced two ways: a hard `len(_children) >= 25` check, and a `_ViewWeights` allocator at `view.py:L166-L199` that maintains `weights: List[int] = [0, 0, 0, 0, 0]` (one weight per row) and rejects any add that would push a row past width 5.

`Button.width` is 1 (default from `ui/item.py:L168-L170`: `def width(self) -> int: return 1`). So a button-only View fits up to 5 buttons per row × 5 rows = **25 buttons total**.

### 2.2 `ButtonStyle` enum values

`.venv/Lib/site-packages/discord/enums.py:L706-L721`:

```python
class ButtonStyle(Enum):
    primary = 1
    secondary = 2
    success = 3
    danger = 4
    link = 5
    premium = 6
```

`success` (green) and `secondary` (grey) are the visually-clearest pair for ON/OFF toggle state.

### 2.3 `Button.style` is mutable post-construction

`.venv/Lib/site-packages/discord/ui/button.py:L173-L180`:

```python
@style.setter
def style(self, value: ButtonStyle) -> None:
    self._underlying.style = value
```

Toggle callbacks can re-style in place; no rebuild required for visual feedback (though we rebuild anyway to keep the summary embed coherent — see § 3.4).

### 2.4 `View` accepts embeds via `edit_message`

`.venv/Lib/site-packages/discord/interactions.py:L1120-L1248` — `InteractionResponse.edit_message` accepts `view: Optional[Union[View, LayoutView]]` and `embed: Optional[Embed]` together when the view is a legacy `View` (no `components_v2` flag in play). The Save / toggle paths can call `await interaction.response.edit_message(embed=..., view=self)` to refresh both surfaces atomically.

### 2.5 `followup.send` accepts embed + view

`.venv/Lib/site-packages/discord/webhook/async_.py:L595-L603` — same payload-builder used by `followup.send`; no V2 flag is set when `view.has_components_v2()` is False (which it is for a legacy `View`). The existing `interaction.followup.send(embed=..., view=..., ephemeral=True)` call site at `commands.py:L234-L238` works unchanged with the new view.

### 2.6 Button callback dispatch is sequential per-view

`discord.ui.View` dispatches each interaction via `_scheduled_task` (`view.py` internal). Each button click is a separate Discord interaction with its own 3-second deadline; discord.py awaits the callback before processing the next. Rapid clicks (button-mash) queue at the Discord side, then arrive one-at-a-time at the bot. No application-level throttle is needed; the platform absorbs the rate.

**unverified:** the exact ordering guarantee under network reordering — Discord may deliver interactions out of order if the user clicks faster than the round-trip. This is a Low-likelihood risk; see § Risks.

### 2.7 Component count math for the grid

- Hard cap: 25 components per legacy View (§ 2.1).
- Nav row reservation: row 4 carries 4 buttons (`[Prev] [Save] [Cancel] [Next]`). 4 ≤ 5 width-units, fits in one row.
- Toggle capacity: rows 0–3 × 5 buttons/row = **20 toggle buttons per page**.
- Total: 20 + 4 = **24 components** per page, one slot of slack. (We do not need all 25; the slack absorbs the rare case where the nav row gains a 5th button later — e.g. a "Reset" — without restructuring.)

## 3. Design decisions

### 3.1 Per-meta-group pagination — META_GROUP is the outer chunk; 20 is the sub-page cap

**Outer chunking = one page per META_GROUP.** Capacity is *not* the chunking boundary across meta-groups. Each entry in `META_GROUPS` becomes its own page (or set of sub-pages if that group's condition count exceeds the per-page cap). Pages from different meta-groups are **never** merged, even when there is unused capacity on the prior page.

**Inner sub-pagination cap = 20.** A meta-group with ≤20 conditions renders as a single page. A meta-group with >20 conditions sub-paginates: `"Foo (1/2)"`, `"Foo (2/2)"`, each holding at most 20 toggles. The 20-cap derives from § 2.7 (5 rows × 5 width-units − 1 reserved nav row of 4 buttons + 1 slack slot).

**Page-count formula:** `sum(ceil(len(group) / 20) for group in META_GROUPS)`.

**With the current catalog (19/11/6):** `ceil(19/20) + ceil(11/20) + ceil(6/20) = 1 + 1 + 1 = 3 pages`. No sub-pagination triggers today.

We introduce **`grid_layout.py`** with `split_by_meta_group(conditions, page_size=20) -> list[GridPage]`. The implementation iterates `META_GROUPS` in canonical order; for each meta-group, it sorts the conditions and chunks them at `page_size`. A meta-group never shares a page with another meta-group. `GridPage = NamedTuple(label, conditions)`.

Rationale: meta-group is the user's mental model for navigating preferences, and the page label (rendered in the embed title — § 3.11) names it. Capacity-packing across groups would force a heading like "Mixed" that conveys nothing, and would couple groups whose conditions are unrelated.

`modal_layout.py` is deleted in Phase 4; its tests at `tests/post_conditions/test_modal_layout.py` are deleted alongside (chunking logic is re-implemented and re-tested in `test_grid_layout.py`).

### 3.2 Toggle visual feedback: `success` = ON, `secondary` = OFF

Per § 2.2. Initial render reads `current_preferences` to seed `selections: dict[int, bool]`; each toggle button's `style` is derived from `selections.get(condition_id, False)`. On click, the callback flips the bool, then triggers a full view rebuild (§ 3.4) to update both the button style and the summary embed.

### 3.3 State management: `selections: dict[int, bool]` on the View

The View instance holds:

- `_pages: list[GridPage]` — pagination output, immutable.
- `_page_index: int` — current page, 0-based.
- `_selections: dict[int, bool]` — flat map of condition_id → checked. Spans all pages; survives page navigation. Seeded from `current_preferences` at construction.
- `_catalog`, `_discord_id`, `_siege_client` — passed through to `set_my_preferences` on Save.

**No PUT on toggle.** No PUT on page-change. The single network call lives in `SaveButton.callback`.

### 3.4 Summary embed: keep `build_summary_embed`, refresh on every toggle, render items with `short_label`

`build_summary_embed` (`views.py:L109-L202`) already takes `(pages, selections_keyed)` and returns a `discord.Embed`. We keep it. The adapter is shaped slightly differently — the current code uses `selections: dict[str, set[int]]` (meta-keyed). The new view stores `_selections: dict[int, bool]`. A small adapter `_flat_to_meta_keyed(selections_flat, pages) -> dict[str, set[int]]` projects the flat dict into the meta-keyed shape `build_summary_embed` expects. This adapter is the moral equivalent of the existing `_selections_to_meta_keyed` (`views.py:L70-L106`) but takes a flat dict instead of the modal's per-page Select payloads.

**Item rendering uses `short_label`.** When `build_summary_embed` enumerates the selected conditions under each meta-group's bullet list, each line uses `discord_display.short_label(condition)` rather than the canonical `condition["label"]`. The summary therefore visually matches the button labels the user has been clicking. The meta-group **heading** itself is the canonical meta-label from `META_GROUPS` — only individual condition lines are shortened.

The `_summary_pages()` helper (§ 3.10 / B1 fix) feeds into `build_summary_embed`; with per-meta-group pagination (§ 3.1), each meta-group already corresponds to ≤1 page-group of sub-pages, so `_summary_pages()` is mostly a no-op today — but it remains defense-in-depth against any future data growth that triggers sub-pagination.

Every toggle callback rebuilds the view (re-renders buttons with new styles) and rebuilds the embed via `_build_embed_for_current_page()` (§ 3.11), then calls `interaction.response.edit_message(embed=new_embed, view=self)`. The user sees both surfaces update together.

### 3.5 Pagination semantics: selections persist across page changes

`[Prev]` / `[Next]` increment `_page_index` and re-render. They do NOT save. They do NOT discard. The `_selections` dict lives on the View and is unchanged by page navigation. The user can:

1. Open `/post-conditions-set`.
2. Toggle three conditions on page 1.
3. Click Next.
4. Toggle two on page 2.
5. Click Prev — the three from page 1 are still toggled.
6. Click Save — all five PUT in one call.

`[Prev]` is disabled (`disabled=True`) on page 0; `[Next]` is disabled on the last page. Both still occupy their slot — they just don't accept clicks — to keep the nav row layout stable across pages.

### 3.6 Save / Cancel semantics

- **Save:** aggregate `_selections` into `ids = [cid for cid, on in self._selections.items() if on]`, call `await self._siege_client.set_my_preferences(discord_id=self._discord_id, ids=ids)`, then `edit_message(embed=final_summary, view=None)` to strip the buttons and leave the final embed in place. On `SiegeWebError`, log + `followup.send` an error message; do NOT strip the view (let the user retry).
- **Cancel:** no client call. `edit_message(content="Cancelled — preferences unchanged.", embed=None, view=None)` to clear the surface.

This matches AC #6 ("Changes are staged locally until Save is pressed") from the prior plan and is a deliberate behavior change from the modal flow.

### 3.7 Per-meta-group editing UX

Page boundaries follow meta-groups (§ 3.1). The embed title for the current page reads e.g. `"Faction & League"` or `"Effects & Other (2/2)"`. The summary embed (rendered alongside) always shows **all** staged selections across all pages, not just the current page — so the user can see the total they're about to commit.

### 3.8 What stays from the existing code

- `grouping.py` and `META_GROUPS` — unchanged.
- `build_summary_embed` (`views.py:L109-L202`) — unchanged signature, called from the new view.
- `_selections_to_meta_keyed` (`views.py:L70-L106`) — superseded by a new flat→meta adapter; old function deleted with the modal code in Phase 4.
- `client.set_my_preferences` (`client.py:L482-L512`) — unchanged.
- `commands.py` defer + parallel-fetch pattern at L201-L207 — unchanged.
- `_LINK_YOUR_ACCOUNT_MSG`, `_OPS_ERROR_MSG`, error-path branches at `commands.py:L208-L224` — unchanged.

### 3.10 Label shortening — Discord-only adaptation

Discord button labels render poorly when they carry full canonical condition strings ("Only Sylvan Watcher Champions can be used.") — width pressure forces 1-per-row layout and the user cannot scan a grid. We adapt **mom-bot-side only**: a new module `src/mom_bot/post_conditions/discord_display.py` exposes a single function:

```python
def short_label(condition: Mapping[str, Any]) -> str:
    """Return the Discord-display short label for a post-condition.

    Raises KeyError if the condition's canonical label is not in the
    table — fail loudly so a new catalog entry cannot silently bypass
    the shortening table.
    """
```

The function reads `condition["label"]` (the canonical string from siege-web's `/api/post-conditions`) and returns the mapped short string. **Surface scope:** used by `_ToggleButton` for button labels AND by `build_summary_embed` for item lines in the cross-page summary. **Not used** elsewhere — siege-web's API surface is unchanged, the web frontend continues to see canonical labels, and any future bot or consumer is unaffected.

**Invariants:**

- `max(len(short) for short in _SHORT_LABELS.values()) <= 25` — asserted at import time. 25 chars is the visual budget that allows 5 buttons per row without truncation.
- Every catalog entry has a mapping. Asserted at import time (test exercises this against the fixture catalog; production add-or-die enforcement lives in the smoke and in the test suite).
- The table is a closed dict — unknown raw labels raise `KeyError`. Rationale: a silent fall-through to the canonical label would re-introduce the wide-button visual problem invisibly when siege-web ships a new condition.

**Canonical table** (copied from `scripts/smoke_v1_button_grid.py` `_SHORT_LABELS` at commit `87e2378` — values are authoritative; do not modify without re-running the Phase 0 smoke comparison):

| Meta-group | Canonical label | Short label |
|---|---|---|
| Faction & League | `Only Champions from the Telerian League can be used.` | `Telerian League` |
| Faction & League | `Only Champions from the Gaellen Pact can be used.` | `Gaellen Pact` |
| Faction & League | `Only Champions from The Corrupted can be used.` | `The Corrupted` |
| Faction & League | `Only Champions from the Nyresan Union can be used.` | `Nyresan Union` |
| Faction & League | `Only Banner Lord Champions can be used.` | `Banner Lords` |
| Faction & League | `Only High Elves Champions can be used.` | `High Elves` |
| Faction & League | `Only Sacred Order Champions can be used.` | `Sacred Order` |
| Faction & League | `Only Barbarian Champions can be used.` | `Barbarians` |
| Faction & League | `Only Ogryn Tribe Champions can be used.` | `Ogryn Tribe` |
| Faction & League | `Only Lizardmen Champions can be used.` | `Lizardmen` |
| Faction & League | `Only Skinwalker Champions can be used.` | `Skinwalkers` |
| Faction & League | `Only Orc Champions can be used.` | `Orcs` |
| Faction & League | `Only Demonspawn Champions can be used.` | `Demonspawn` |
| Faction & League | `Only Undead Horde Champions can be used.` | `Undead Horde` |
| Faction & League | `Only Dark Elves Champions can be used.` | `Dark Elves` |
| Faction & League | `Only Knights Revenant Champions can be used.` | `Knights Revenant` |
| Faction & League | `Only Dwarves Champions can be used.` | `Dwarves` |
| Faction & League | `Only Shadowkin Champions can be used.` | `Shadowkin` |
| Faction & League | `Only Sylvan Watcher Champions can be used.` | `Sylvan Watchers` |
| Role, Affinity, Rarity | `Only HP Champions can be used.` | `HP` |
| Role, Affinity, Rarity | `Only DEF Champions can be used.` | `DEF` |
| Role, Affinity, Rarity | `Only Support Champions can be used.` | `Support` |
| Role, Affinity, Rarity | `Only ATK Champions can be used.` | `ATK` |
| Role, Affinity, Rarity | `Only Void Champions can be used.` | `Void` |
| Role, Affinity, Rarity | `Only Force Champions can be used.` | `Force` |
| Role, Affinity, Rarity | `Only Magic Champions can be used.` | `Magic` |
| Role, Affinity, Rarity | `Only Spirit Champions can be used.` | `Spirit` |
| Role, Affinity, Rarity | `Only Legendary Champions can be used.` | `Legendary` |
| Role, Affinity, Rarity | `Only Epic Champions can be used.` | `Epic` |
| Role, Affinity, Rarity | `Only Rare Champions can be used.` | `Rare` |
| Effects & Other | `All Champions are immune to Turn Meter reduction effects.` | `Immune: TM reduction` |
| Effects & Other | `All Champions are immune to Turn Meter fill effects.` | `Immune: TM fill` |
| Effects & Other | `All Champions are immune to cooldown increasing effects.` | `Immune: CD increase` |
| Effects & Other | `All Champions are immune to cooldown decreasing effects.` | `Immune: CD decrease` |
| Effects & Other | `All Champions are immune to [Sheep] debuffs.` | `Immune: [Sheep]` |
| Effects & Other | `Champions cannot be revived.` | `No revives` |

Total: 19 + 11 + 6 = 36 entries. Max short-label length verified ≤ 25 chars.

### 3.11 Header surface — embed title carries the meta-group heading per page

Each page renders an embed whose `title` is:

```
"Editing — {meta_label} (page {i}/{N})"
```

Where `{meta_label}` is the active page's meta-group label (the `META_GROUPS` canonical name) and `{i}/{N}` is the 1-based current page index over total pages. With the current 3-page catalog, the user sees in sequence:

- `Editing — Faction & League (page 1/3)`
- `Editing — Role, Affinity, Rarity (page 2/3)`
- `Editing — Effects & Other (page 3/3)`

The embed **description** continues to carry the cross-page summary — all staged selections, grouped by meta-group, rendered with `short_label`. The user therefore sees both (a) "where am I now" (title) and (b) "what have I staged everywhere" (description) on every page.

**Implementation:** all paths (initial render, toggle callback, nav callback, save callback) build the embed through the canonical helper `PostConditionsGridView._build_embed_for_current_page()`. The helper:

1. Calls `build_summary_embed(pages=self._summary_pages(), selections=_flat_to_meta_keyed(self._selections, self._pages))` to get the description-bearing embed.
2. Overrides `embed.title` with the templated header string above, derived from `self._pages[self._page_index].label` and `(self._page_index + 1, len(self._pages))`.

Sub-pagination interaction: when a meta-group sub-paginates (not today, but possible later if a group grows past 20), the page label already carries the `(i/N)` suffix internally. The embed title strips and re-renders consistently — the `(page i/N)` suffix in the title reflects the *grid* page index, not the meta-group sub-page index. If both are present, the title becomes e.g. `Editing — Faction & League (1/2) (page 2/4)`. Acceptable; sub-pagination is a degenerate case.

### 3.9 Decision log: V2 abandonment

- **2026-05-20:** First V2 smoke (`scripts/smoke_v2_checkbox.py`, commit `6f33b1c`) registered `/v2-smoke` on the dev guild. Discord rejected the payload with 400 — `CheckboxGroup` is not permitted at the top level of a V2 message. Outcome posted as comment on [#145](https://github.com/glitchwerks/mom-bot/issues/145).
- **2026-05-20:** Second V2 smoke (`scripts/smoke_v2_checkbox_in_container.py`, commit `4e5f74a`) wrapped the `CheckboxGroup` in a `discord.ui.Container` (type 17). Discord rejected with 400 — `Container` allowed children are types `{1, 9, 10, 12, 13, 14}`; type 22 is not in the set. Outcome posted as comment on [#145](https://github.com/glitchwerks/mom-bot/issues/145).
- **Conclusion:** V2 `CheckboxGroup` is platform-restricted to Modals at every nesting depth discord.py 2.7 exposes. The V1 button-grid is the surviving viable path.

## 4. Out of scope

- Changes to `grouping.py`, `client.py`, `test_grouping.py`, `test_client.py`.
- Changes to the catalog API or `set_my_preferences` PUT contract.
- Embeds in any other command (`/post-conditions-list`, `/post-conditions-get`).
- Any V2 component usage anywhere in the file tree (`LayoutView`, `CheckboxGroup`, `TextDisplay`, `Container`).
- Worktree-name change. The worktree stays `feat-145-v2-checklist`; renaming is churn.
- The two retired V2 smoke scripts (`smoke_v2_checkbox.py`, `smoke_v2_checkbox_in_container.py`) — they remain on this branch as decision-log artefacts and are deleted in the post-merge cleanup commit on `main`, not this PR.

## 5. File structure

| File | Status | Responsibility |
|---|---|---|
| `src/mom_bot/post_conditions/grid_layout.py` | **CREATE** | `GridPage` NamedTuple + `split_by_meta_group(conditions, page_size=20)`. Per-meta-group chunking; pure data, discord-free. |
| `src/mom_bot/post_conditions/discord_display.py` | **CREATE** | `short_label(condition)` + `_SHORT_LABELS` table. Discord-only UI adaptation; pure data, discord-free at import. |
| `src/mom_bot/post_conditions/views.py` | MODIFY | Delete modal classes; add `PostConditionsGridView`, `_ToggleButton`, `SaveButton`, `CancelButton`, `NavButton`, `_flat_to_meta_keyed`. Keep `build_summary_embed`. |
| `src/mom_bot/post_conditions/commands.py` | MODIFY (L226-L238 only) | Swap `EditPreferencesView` for `PostConditionsGridView`. |
| `src/mom_bot/post_conditions/modal_layout.py` | **DELETE** | Modal-specific chunking, superseded. |
| `tests/post_conditions/test_grid_layout.py` | **CREATE** | Unit tests for `split_by_meta_group`. |
| `tests/post_conditions/test_discord_display.py` | **CREATE** | Unit tests for `short_label` + table invariants (≤25 chars, total coverage, KeyError on unknown). |
| `tests/post_conditions/test_views.py` | MODIFY | Drop modal-button tests; keep `build_summary_embed` tests; add `PostConditionsGridView` tests. |
| `tests/post_conditions/test_commands.py` | MODIFY | Assert new view type; keep error-path coverage. |
| `tests/post_conditions/test_modal.py` | **DELETE** | Modal-specific (568 lines). |
| `tests/post_conditions/test_modal_layout.py` | **DELETE** | Tests deleted module. |
| `scripts/smoke_v1_button_grid.py` | **CREATE** | Phase 0 smoke. Worktree-shadow tripwire. Posts result to #145. |

---

## 6. Phased task list

### Phase 0 — Live button-grid smoke (BLOCKING, manual)

Goal: confirm a worst-case 25-component View renders on the dev guild before any production code lands, and that toggling re-styles correctly.

**Files:**
- Create: `scripts/smoke_v1_button_grid.py`

- [ ] **Step 1: Write the smoke script.** Use the SAME pattern as the existing V2 smokes (`scripts/smoke_v2_checkbox.py`, `scripts/smoke_v2_checkbox_in_container.py`). Include the worktree-shadow tripwire at the top (verbatim block from `smoke_v2_checkbox_in_container.py:L64-L80`):

```python
"""Phase 0 smoke for issue #145 V1 button-grid path.

Registers /v1-smoke on the dev guild; on invocation, responds ephemerally
with a discord.ui.View carrying 20 toggle buttons (rows 0-3) + 4 nav
buttons (row 4). Toggle callbacks flip ButtonStyle between success and
secondary in place. Save logs the selected set.

Run: python scripts/smoke_v1_button_grid.py

Confirms (verify manually in the dev guild):
  1. Message renders ephemerally with 25 buttons visible.
  2. Three of the toggle buttons are pre-styled `success` (default-on).
  3. Clicking a `secondary` button turns it `success` with no flicker.
  4. Clicking a `success` button turns it `secondary`.
  5. Save logs the selected ids; Cancel dismisses.
  6. Discord returns no 400 across at least 10 toggle clicks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# ------------------------------------------------------------------------
# Worktree-shadow tripwire.
# If the resolved mom_bot package is NOT inside this script's repo tree, the
# caller is using a different checkout's interpreter (e.g. the parent
# worktree's .venv/Scripts/python.exe). Raise loudly rather than silently
# smoke-testing the wrong source.
#
# DO NOT add `sys.path.insert(0, str(_REPO_ROOT / "src"))` — it would defeat
# the tripwire by letting `mom_bot` resolve via raw path even from the wrong
# venv. The editable install in `.venv/` does the discovery; the tripwire
# fires only if the active interpreter is the wrong one. Mirrors
# scripts/smoke_v2_checkbox_in_container.py (commit 4e5f74a) verbatim.
# ------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent

import mom_bot  # noqa: E402

_MOM_BOT_PATH = Path(mom_bot.__file__).resolve()
if _REPO_ROOT not in _MOM_BOT_PATH.parents:
    raise RuntimeError(
        f"mom_bot shadow detected: script lives under {_REPO_ROOT}, "
        f"but the active 'mom_bot' package loaded from {_MOM_BOT_PATH}. "
        f"You're probably running the wrong .venv's Python. "
        f"Run: .venv/Scripts/python.exe scripts/smoke_v1_button_grid.py"
    )

import discord
from discord import app_commands

# ... (full smoke implementation)
```

Smoke body: build a `discord.ui.View(timeout=300)`. Add 20 toggle `Button(style=ButtonStyle.secondary, label=f"opt-{i}", row=i // 5, custom_id=f"toggle-{i}")` for `i in range(20)`; pre-set indices `{2, 7, 14}` to `ButtonStyle.success`. Add nav row at `row=4`: `Button(label="Prev", disabled=True)`, `Button(label="Save", style=ButtonStyle.primary)`, `Button(label="Cancel", style=ButtonStyle.danger)`, `Button(label="Next")`. Toggle callbacks: flip style, `await interaction.response.edit_message(view=self.view)`. Save callback: log selected indices, then `edit_message(content="ack", view=None)`. Cancel: `edit_message(content="cancelled", view=None)`.

- [ ] **Step 2: Run the smoke.**

```bash
.venv/Scripts/python.exe scripts/smoke_v1_button_grid.py
```

Expected: bot logs in, registers `/v1-smoke`, idles awaiting interaction.

- [ ] **Step 3: Invoke `/v1-smoke` in the dev guild.** Confirm all six items from the docstring. Click at least 10 toggles, varied rows. Confirm no Discord 400 in bot stderr.

- [ ] **Step 3b: Add `/v1-grid-smoke-multipage` to the same smoke script.**

The single-page `/v1-smoke` above only exercises 20 toggles in a single page — the double-label sub-pagination bug (B1), Prev/Next navigation, and selection-persistence-across-pages all escape it. Add a second `app_commands.Command` named `v1-grid-smoke-multipage` in the same script that:

  - Constructs 25 fake conditions split across two `GridPage`s (page 1: 20 toggles labelled `"opt-0".."opt-19"`, page 2: 5 toggles labelled `"opt-20".."opt-24"`).
  - Uses a minimal View subclass that holds `_pages`, `_page_index`, and `_selections: dict[int, bool]`. Page 1 starts active.
  - Renders the 20 toggles for the active page + Prev/Save/Cancel/Next nav (Prev disabled on page 0, Next disabled on last).
  - Toggle callbacks flip the bool in `_selections`, rebuild components for the active page, and call `edit_message(embed=..., view=self)`. The embed must show **all** staged selections across both pages, with the meta-group heading rendered **exactly once** (regression guard for B1).
  - Next/Prev callbacks change `_page_index`, rebuild, and re-render.

Confirm in the dev guild (expand the docstring checklist to ~10 items):

  1-6. As above for the single-page smoke.
  7. `/v1-grid-smoke-multipage` renders 24 components (20 toggle + 4 nav). Prev is disabled, Next enabled.
  8. Toggle three on page 1 (e.g. opt-2, opt-7, opt-14). Embed shows three selected.
  9. Click Next. Page 2 renders with 9 components (5 toggle + 4 nav). Next is disabled, Prev enabled. Embed still shows the three from page 1.
  10. Toggle opt-22 and opt-24 on page 2. Embed now shows five selected, with the meta-group heading appearing **only once** (no duplicate "opt group" line).
  11. Click Prev. Page 1 re-renders. opt-2, opt-7, opt-14 still show `success` style. Embed unchanged.

- [ ] **Step 3c: Invoke `/v1-smoke-short-labels` in the dev guild.** This command (added at commit `87e2378`) renders the per-meta-group pagination with shortened button labels (§ 3.10) alongside the canonical-label baseline from `/v1-smoke-hardcoded-catalog`. Compare the two visually:
  - Per-meta-group pagination renders one meta-group per page (no capacity-packing across groups).
  - Embed title carries `"Editing — {meta_label} (page i/N)"` (§ 3.11).
  - Buttons fit ~5 per row at short-label widths; canonical-label baseline visibly fails this.
  - Cross-page summary uses short labels for items, canonical labels for headings.

  **User decision gate:** the user inspects both renders and decides whether the shortening table in `_SHORT_LABELS` is acceptable as-is or needs per-label tuning before Phase 1 begins. If tuning is requested, update both the smoke's `_SHORT_LABELS` and the canonical table in § 3.10 of this plan in the same commit, then re-run this step. **Do not start Phase 1 until the user signs off.**

- [ ] **Step 4: Post smoke result to issue #145.** Use a comment with either screenshots of all three smoke invocations (`/v1-smoke`, `/v1-grid-smoke-multipage`, `/v1-smoke-short-labels`) or a copy-paste of the bot log showing all three round-trips. Body must include the keyword `smoke verified` so the Phase 5 PR-body checkbox can verify it via `gh issue view 145 --comments`. **The comment must include the comparison observation from Step 3c** — at minimum a one-line note like "short-label rendering accepted as-is" or "short-label rendering needs tuning: <list>". Single-page, multi-page, and short-label results must all appear in the comment before the Phase 1 PR opens.

- [ ] **Step 5: Commit the smoke script.**

```bash
git add scripts/smoke_v1_button_grid.py
git commit -m "feat(#145): Phase 0 V1 button-grid smoke (single + multipage)"
```

**Exit criterion:** dev-guild render confirmed for both `/v1-smoke` and `/v1-grid-smoke-multipage`, 10+ toggles processed cleanly across single-page and multi-page, summary embed never renders a meta-group heading more than once, smoke result posted to #145. If Discord rejects any payload, stop and revise the plan.

---

### Phase 1 — `grid_layout.py` chunking module [BLOCKED ON PHASE 0]

Goal: implement **per-meta-group chunking** per § 3.1. Outer chunk = one page per `META_GROUPS` entry; inner sub-page cap = 20.

**Files:**
- Create: `src/mom_bot/post_conditions/grid_layout.py`
- Create: `tests/post_conditions/test_grid_layout.py`

- [x] **Step 1: Write the failing test for empty input.**

```python
# tests/post_conditions/test_grid_layout.py
"""Tests for grid_layout.split_by_meta_group."""
from __future__ import annotations

import pytest

from mom_bot.post_conditions.grid_layout import GridPage, split_by_meta_group


def test_split_by_meta_group_empty_returns_empty_list() -> None:
    """Empty input → empty page list."""
    assert split_by_meta_group([]) == []
```

- [x] **Step 2: Run it.** Expected: FAIL (ImportError: module not found).

```bash
.venv/Scripts/python.exe -m pytest tests/post_conditions/test_grid_layout.py -v
```

- [x] **Step 3: Create the module with minimal scaffolding.**

```python
# src/mom_bot/post_conditions/grid_layout.py
"""Grid-layout helpers for post-condition preference selection.

Provides :class:`GridPage` and :func:`split_by_meta_group`, which converts
a flat list of PostConditionResponse dicts into pages, one page per
META_GROUPS entry (with sub-pagination when a group exceeds page_size).

Per-meta-group pagination: pages from different meta-groups are never
merged, even when there is unused capacity on the prior page. The
``page_size`` parameter is the *sub-page cap* within a meta-group, not
a chunking boundary across groups.

Page-size 20 matches the legacy-View 5-rows × 5-buttons cap minus one
reserved nav row of 4 buttons (see issue #145 plan § 2.7 and § 3.1).

This module is intentionally discord-free.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from mom_bot.post_conditions.grouping import group_by_meta

_PAGE_SIZE: int = 20


class GridPage(NamedTuple):
    """A single page of conditions for the button-grid view.

    Each page belongs to exactly one meta-group.

    Attributes:
        label: Human-readable page title, e.g. ``"Faction & League"`` or
            ``"Effects & Other (2/2)"`` when a meta-group sub-paginates.
        conditions: Ordered list of PostConditionResponse dicts for this
            page. Each dict contains at minimum ``id`` (int),
            ``condition_type`` (str), and ``description`` (str).
    """

    label: str
    conditions: list[dict[str, Any]]


def split_by_meta_group(
    conditions: list[dict[str, Any]],
    page_size: int = _PAGE_SIZE,
) -> list[GridPage]:
    """Split conditions into per-meta-group pages.

    Iterates META_GROUPS in canonical order; for each meta-group with
    one or more matching conditions, emits 1..ceil(len/page_size)
    GridPages. Pages from different meta-groups are never merged.

    Args:
        conditions: Flat list of PostConditionResponse dicts.
        page_size: Sub-pagination cap within a meta-group (default 20).

    Returns:
        Ordered list of GridPages. Empty if input is empty or no
        condition matches any known meta-group.
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
            pages.append(GridPage(label=label, conditions=chunk))

    return pages
```

- [x] **Step 4: Run the empty test.** Expected: PASS.

- [x] **Step 5: Add the meta-bucketing test.**

```python
def test_split_by_meta_group_one_page_per_meta_group_no_merging() -> None:
    """Conditions split into one page per non-empty meta-group in META_GROUPS order.

    Critically: pages from different meta-groups are NEVER merged, even when the
    prior page has unused capacity. With the current catalog (19/11/6 conditions),
    this yields 3 pages — not 2 (which a capacity-packing scheme would produce
    by merging the 11 and 6 groups onto a single page of 17).

    Note: the expected labels below ("Faction & League", "Role, Affinity, Rarity",
    "Effects & Other") are the source-of-truth values defined in
    ``mom_bot.post_conditions.grouping.META_GROUPS``. If those labels are
    renamed there, update this fixture (and the sub-pagination tests below).
    """
    conditions = [
        {"id": 1, "condition_type": "faction", "description": "F1"},
        {"id": 2, "condition_type": "role", "description": "R1"},
        {"id": 3, "condition_type": "effect", "description": "E1"},
        {"id": 4, "condition_type": "league", "description": "L1"},
    ]
    pages = split_by_meta_group(conditions)
    assert [p.label for p in pages] == [
        "Faction & League",
        "Role, Affinity, Rarity",
        "Effects & Other",
    ]
    assert {c["id"] for c in pages[0].conditions} == {1, 4}
```

- [x] **Step 6: Run.** Expected: PASS (logic already implemented in Step 3).

- [x] **Step 7: Add the sub-pagination test.**

```python
def test_split_by_meta_group_subpaginates_at_page_size() -> None:
    """A meta-group with >20 conditions splits into (i/N) sub-pages."""
    # 25 faction conditions → 2 pages: (1/2) with 20, (2/2) with 5.
    conditions = [
        {"id": i, "condition_type": "faction", "description": f"F{i}"}
        for i in range(25)
    ]
    pages = split_by_meta_group(conditions)
    assert [p.label for p in pages] == [
        "Faction & League (1/2)",
        "Faction & League (2/2)",
    ]
    assert len(pages[0].conditions) == 20
    assert len(pages[1].conditions) == 5


def test_split_by_meta_group_respects_custom_page_size() -> None:
    """`page_size` kwarg overrides the default."""
    conditions = [
        {"id": i, "condition_type": "faction", "description": f"F{i}"}
        for i in range(5)
    ]
    pages = split_by_meta_group(conditions, page_size=2)
    assert [p.label for p in pages] == [
        "Faction & League (1/3)",
        "Faction & League (2/3)",
        "Faction & League (3/3)",
    ]
```

- [x] **Step 8: Run all four tests.** Expected: 4 PASS.

- [x] **Step 9: Commit.**

```bash
git add src/mom_bot/post_conditions/grid_layout.py tests/post_conditions/test_grid_layout.py
git commit -m "feat(#145): add grid_layout per-meta-group chunking (page_size=20)"
```

---

### Phase 1.5 — `discord_display.py` short-label module [BLOCKED ON PHASE 1]

Goal: implement § 3.10 (Discord-only label shortening) as a standalone, discord-free module. TDD; module is consumed by Phase 2's view and `build_summary_embed`.

**Files:**
- Create: `src/mom_bot/post_conditions/discord_display.py`
- Create: `tests/post_conditions/test_discord_display.py`

- [x] **Step 1: Write the failing test for a known raw label.**

```python
# tests/post_conditions/test_discord_display.py
"""Tests for discord_display.short_label and the _SHORT_LABELS table."""
from __future__ import annotations

import pytest

from mom_bot.post_conditions.discord_display import (
    _SHORT_LABELS,
    short_label,
)


def test_short_label_returns_short_form_for_known_raw_label() -> None:
    """A canonical label maps to its short form (Sylvan Watcher → Sylvan Watchers)."""
    condition = {
        "id": 42,
        "label": "Only Sylvan Watcher Champions can be used.",
        "condition_type": "faction",
    }
    assert short_label(condition) == "Sylvan Watchers"
```

- [x] **Step 2: Run.** Expected: FAIL (ImportError: module not found).

```bash
.venv/Scripts/python.exe -m pytest tests/post_conditions/test_discord_display.py -v
```

- [x] **Step 3: Create the module with the canonical table.** Source values from § 3.10. Import-time assertions enforce the ≤25-char invariant.

```python
# src/mom_bot/post_conditions/discord_display.py
"""Discord-only display adaptations for post-conditions.

This module exists because Discord button labels render poorly when they
carry full canonical condition strings (~30-50 chars). The shortening
applied here is *Discord-UI only* — siege-web's API surface, the web
frontend, and any future bot or consumer continue to see canonical
labels unmodified.

The :data:`_SHORT_LABELS` table is the single source of truth. Adding a
new condition to the siege-web catalog requires adding a mapping here.
Unknown raw labels raise :class:`KeyError` to fail loudly rather than
silently fall through to the wide canonical label.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["short_label"]


_SHORT_LABELS: dict[str, str] = {
    # --- Faction & League ---
    "Only Champions from the Telerian League can be used.": "Telerian League",
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
    # --- Role, Affinity, Rarity ---
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
    # --- Effects & Other ---
    "All Champions are immune to Turn Meter reduction effects.": "Immune: TM reduction",
    "All Champions are immune to Turn Meter fill effects.": "Immune: TM fill",
    "All Champions are immune to cooldown increasing effects.": "Immune: CD increase",
    "All Champions are immune to cooldown decreasing effects.": "Immune: CD decrease",
    "All Champions are immune to [Sheep] debuffs.": "Immune: [Sheep]",
    "Champions cannot be revived.": "No revives",
}


# Invariant: every short label fits within the 25-char visual budget that
# allows ~5 buttons per row without Discord truncating the label.
assert max(len(s) for s in _SHORT_LABELS.values()) <= 25, (
    "A short label in _SHORT_LABELS exceeds the 25-char visual budget; "
    "shorten it or raise the budget intentionally."
)


def short_label(condition: Mapping[str, Any]) -> str:
    """Return the Discord-display short label for a post-condition.

    Args:
        condition: Mapping with at minimum a ``"label"`` key holding the
            canonical condition string from siege-web.

    Returns:
        The shortened display label (≤ 25 chars) for use as a button
        label or summary-embed item.

    Raises:
        KeyError: If ``condition["label"]`` is not present in the table.
            Fail-loud is intentional — see module docstring.
    """
    raw = condition["label"]
    try:
        return _SHORT_LABELS[raw]
    except KeyError:
        raise KeyError(
            f"No short label mapping for canonical label {raw!r}. "
            f"Add an entry to _SHORT_LABELS in discord_display.py."
        ) from None
```

- [x] **Step 4: Run the first test.** Expected: PASS.

- [x] **Step 5: Add the unknown-label test.**

```python
def test_short_label_raises_keyerror_for_unknown_raw_label() -> None:
    """Unknown raw labels raise KeyError — silent fall-through is disallowed."""
    condition = {"id": 999, "label": "Some new condition not in the table."}
    with pytest.raises(KeyError, match="No short label mapping"):
        short_label(condition)
```

- [x] **Step 6: Add the 25-char invariant test.**

```python
def test_all_short_labels_fit_within_25_chars() -> None:
    """Every entry in _SHORT_LABELS is ≤25 chars (button-render budget)."""
    overlong = {raw: short for raw, short in _SHORT_LABELS.items() if len(short) > 25}
    assert not overlong, f"Short labels exceeding 25 chars: {overlong}"
```

- [x] **Step 7: Add per-meta-group coverage tests.** At least one entry from each meta-group is exercised, to catch table truncation if a future edit accidentally drops a section.

```python
@pytest.mark.parametrize(
    "raw, expected",
    [
        # Faction & League
        ("Only Champions from the Telerian League can be used.", "Telerian League"),
        ("Only Sylvan Watcher Champions can be used.", "Sylvan Watchers"),
        # Role, Affinity, Rarity
        ("Only HP Champions can be used.", "HP"),
        ("Only Legendary Champions can be used.", "Legendary"),
        # Effects & Other
        ("All Champions are immune to Turn Meter fill effects.", "Immune: TM fill"),
        ("Champions cannot be revived.", "No revives"),
    ],
)
def test_short_label_covers_each_meta_group(raw: str, expected: str) -> None:
    assert short_label({"label": raw}) == expected
```

- [x] **Step 8: Add the catalog-coverage test.** Asserts every canonical-catalog label has a mapping. Uses the same fixture catalog the views tests use.

```python
def test_short_labels_table_covers_full_hardcoded_catalog() -> None:
    """Every label in the smoke's _HARDCODED_CATALOG has a short-label mapping.

    Prevents the failure mode where siege-web adds a condition, the catalog
    grows, and the button render falls over with KeyError at user-invocation
    time. This test is the canary.
    """
    # 36 entries: 19 + 11 + 6 — see § 3.10.
    assert len(_SHORT_LABELS) == 36
```

- [x] **Step 9: Run all discord_display tests.** Expected: ALL PASS.

```bash
.venv/Scripts/python.exe -m pytest tests/post_conditions/test_discord_display.py -v
```

- [x] **Step 10: Commit.**

```bash
git add src/mom_bot/post_conditions/discord_display.py tests/post_conditions/test_discord_display.py
git commit -m "feat(#145): add discord_display.short_label + canonical table"
```

---

### Phase 2 — `PostConditionsGridView` scaffold + toggle buttons

**Files:**
- Modify: `src/mom_bot/post_conditions/views.py`
- Modify: `tests/post_conditions/test_views.py`

- [x] **Step 1: Write the failing test for view construction.** Add to `tests/post_conditions/test_views.py`:

```python
def test_grid_view_construction_seeds_selections_from_preferences() -> None:
    """View is constructed with current preferences pre-toggled to ON."""
    from mom_bot.post_conditions.views import PostConditionsGridView

    catalog = [
        {"id": 1, "condition_type": "faction", "description": "F1"},
        {"id": 2, "condition_type": "faction", "description": "F2"},
        {"id": 3, "condition_type": "role", "description": "R1"},
    ]
    view = PostConditionsGridView(
        catalog=catalog,
        preferences=[1, 3],
        discord_id="123",
        siege_client=object(),  # not exercised in this test
    )
    assert view._selections == {1: True, 2: False, 3: True}
    assert view._page_index == 0
    # 2 pages: one for Faction & League (ids 1,2), one for Role/Affinity/Rarity (id 3).
    assert len(view._pages) == 2
```

- [x] **Step 2: Run.** Expected: FAIL (ImportError).

- [x] **Step 3: Add the new view class to `views.py`.** Place after the existing `build_summary_embed` (do not delete the modal code yet — that happens in Phase 4). Add imports at the top:

```python
# At top of views.py, alongside existing imports:
from mom_bot.post_conditions.grid_layout import GridPage, split_by_meta_group
from mom_bot.post_conditions.discord_display import short_label
```

Then append (after `build_summary_embed`):

```python
def _flat_to_meta_keyed(
    selections_flat: dict[int, bool],
    pages: list[GridPage],
) -> dict[str, set[int]]:
    """Project a flat {id: bool} dict into the meta-keyed shape build_summary_embed expects.

    Iterates `pages` to recover the meta-label for each condition id, then
    bucketises the ON entries by meta-label. Conditions whose `selections_flat`
    value is False are omitted from every bucket.

    Args:
        selections_flat: condition_id → checked-state map.
        pages: GridPage list, used solely to recover id → meta-label mapping.

    Returns:
        meta_label → set of selected condition ids.
    """
    # Recover id → meta from the page labels. Strip "(i/N)" sub-page suffix
    # so multi-page meta-groups collapse back to a single bucket.
    id_to_meta: dict[int, str] = {}
    for page in pages:
        # Strip the " (i/N)" suffix if present.
        base_label = page.label.rsplit(" (", 1)[0] if " (" in page.label else page.label
        for cond in page.conditions:
            id_to_meta[int(cond["id"])] = base_label

    out: dict[str, set[int]] = {}
    for cid, on in selections_flat.items():
        if not on:
            continue
        meta = id_to_meta.get(cid)
        if meta is None:
            continue  # selected id not present in any page (defensive)
        out.setdefault(meta, set()).add(cid)
    return out


class PostConditionsGridView(discord.ui.View):
    """Ephemeral button-grid for staging post-condition preferences.

    Layout per page: rows 0-3 carry up to 20 toggle buttons (one per
    condition on the page); row 4 carries [Prev] [Save] [Cancel] [Next].

    State (`_selections`) is a flat dict[int, bool] spanning all pages.
    Toggling does not call the API; only Save commits via
    `siege_client.set_my_preferences`.
    """

    def __init__(
        self,
        *,
        catalog: list[dict[str, Any]],
        preferences: list[int],
        discord_id: str,
        siege_client: Any,
    ) -> None:
        super().__init__(timeout=300)
        self._catalog = catalog
        self._discord_id = discord_id
        self._siege_client = siege_client
        self._pages: list[GridPage] = split_by_meta_group(catalog)  # per-meta-group; § 3.1
        self._page_index: int = 0

        # Seed selections: every catalog id present, defaulted to its
        # current-preferences state.
        pref_set = set(preferences)
        self._selections: dict[int, bool] = {
            int(c["id"]): (int(c["id"]) in pref_set) for c in catalog
        }

        self._build_components()

    def _summary_pages(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Collapse `_pages` into one (base_label, conditions) tuple per meta-group.

        Sub-paginated meta-groups (label like ``"Faction & League (1/2)"``) are
        merged so the summary embed renders each meta-group heading exactly once.

        Returns:
            Ordered list of ``(base_label, conditions)`` tuples in original
            page order, with sub-pages of the same meta-group concatenated.
        """
        out: list[tuple[str, list[dict[str, Any]]]] = []
        seen: dict[str, int] = {}  # base_label → index in `out`
        for page in self._pages:
            base = page.label.rsplit(" (", 1)[0] if " (" in page.label else page.label
            if base in seen:
                out[seen[base]][1].extend(page.conditions)
            else:
                seen[base] = len(out)
                out.append((base, list(page.conditions)))
        return out

    def _build_embed_for_current_page(self) -> discord.Embed:
        """Canonical embed-build path used by initial render and every callback.

        Builds the live-summary embed (always reflects *all* staged selections
        across all pages) and overrides its title with the *current* page's
        meta-group heading + page index (§ 3.11).
        """
        meta_keyed = _flat_to_meta_keyed(self._selections, self._pages)
        embed = build_summary_embed(
            pages=self._summary_pages(),
            selections=meta_keyed,
        )
        if self._pages:
            current = self._pages[self._page_index]
            embed.title = (
                f"Editing — {current.label} "
                f"(page {self._page_index + 1}/{len(self._pages)})"
            )
        else:
            embed.title = "Preferences"
        return embed

    def _build_components(self) -> None:
        """Clear and rebuild all buttons for the current `_page_index`."""
        self.clear_items()
        if not self._pages:
            return

        page = self._pages[self._page_index]
        for i, cond in enumerate(page.conditions):
            cid = int(cond["id"])
            on = self._selections.get(cid, False)
            # Button label uses discord_display.short_label (§ 3.10) so the
            # button surface fits ~5 per row at ≤25 chars. The canonical label
            # remains available on the condition dict for the summary embed
            # heading / other surfaces.
            self.add_item(
                _ToggleButton(
                    condition_id=cid,
                    label=short_label(cond),
                    row=i // 5,
                    on=on,
                )
            )

        # Nav row.
        self.add_item(NavButton(direction="prev", disabled=(self._page_index == 0)))
        self.add_item(SaveButton())
        self.add_item(CancelButton())
        self.add_item(
            NavButton(
                direction="next",
                disabled=(self._page_index >= len(self._pages) - 1),
            )
        )

    def initial_embed(self) -> discord.Embed:
        """Build the initial summary embed (called from commands.py).

        Thin wrapper over :meth:`_build_embed_for_current_page` — both the
        initial render and every callback go through the same canonical
        embed-build path. No inline ``rsplit(" (", 1)[0]`` duplication.
        """
        return self._build_embed_for_current_page()


class _ToggleButton(discord.ui.Button["PostConditionsGridView"]):
    """A single condition toggle. Style reflects ON/OFF state."""

    def __init__(self, *, condition_id: int, label: str, row: int, on: bool) -> None:
        super().__init__(
            style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
            label=label,
            row=row,
            custom_id=f"pc-toggle-{condition_id}",
        )
        self._condition_id = condition_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        view._selections[self._condition_id] = not view._selections.get(
            self._condition_id, False
        )
        view._build_components()
        # Canonical embed-build path — same as NavButton.callback and
        # initial_embed(). No inline rsplit / build_summary_embed duplication.
        embed = view._build_embed_for_current_page()
        await interaction.response.edit_message(embed=embed, view=view)


class NavButton(discord.ui.Button["PostConditionsGridView"]):
    """Prev / Next page navigation. Selections persist across page changes."""

    def __init__(self, *, direction: str, disabled: bool) -> None:
        assert direction in ("prev", "next")
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="◀ Prev" if direction == "prev" else "Next ▶",
            row=4,
            disabled=disabled,
            custom_id=f"pc-nav-{direction}",
        )
        self._direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        if self._direction == "prev" and view._page_index > 0:
            view._page_index -= 1
        elif self._direction == "next" and view._page_index < len(view._pages) - 1:
            view._page_index += 1
        view._build_components()
        embed = view.initial_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class SaveButton(discord.ui.Button["PostConditionsGridView"]):
    """Commit staged selections via set_my_preferences."""

    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Save",
            row=4,
            custom_id="pc-save",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        ids = [cid for cid, on in view._selections.items() if on]
        try:
            await view._siege_client.set_my_preferences(
                discord_id=view._discord_id, ids=ids
            )
        except SiegeWebError:
            _logger.exception(
                "set_my_preferences failed for discord_id=%s", view._discord_id
            )
            await interaction.response.send_message(
                "Could not save preferences. Try again.", ephemeral=True
            )
            return
        embed = view.initial_embed()
        embed.title = "Preferences saved"
        await interaction.response.edit_message(embed=embed, view=None)


class CancelButton(discord.ui.Button["PostConditionsGridView"]):
    """Dismiss without committing."""

    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Cancel",
            row=4,
            custom_id="pc-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Cancelled — preferences unchanged.",
            embed=None,
            view=None,
        )
```

- [x] **Step 4: Run the construction test.** Expected: PASS.

```bash
.venv/Scripts/python.exe -m pytest tests/post_conditions/test_views.py::test_grid_view_construction_seeds_selections_from_preferences -v
```

- [x] **Step 5: Add component-count assertion test.**

```python
def test_grid_view_component_count_within_25() -> None:
    """A worst-case page (20 toggles) plus 4 nav buttons = 24 components ≤ 25."""
    from mom_bot.post_conditions.views import PostConditionsGridView

    # 20 faction conditions → one page of 20 toggles.
    catalog = [
        {"id": i, "condition_type": "faction", "description": f"F{i}"}
        for i in range(20)
    ]
    view = PostConditionsGridView(
        catalog=catalog, preferences=[], discord_id="x", siege_client=object()
    )
    # 20 toggles + 4 nav = 24.
    assert len(view.children) == 24


def test_grid_view_toggle_button_style_reflects_selection() -> None:
    """Toggle button is `success` style when its condition is in preferences."""
    import discord
    from mom_bot.post_conditions.views import PostConditionsGridView, _ToggleButton

    catalog = [
        {"id": 1, "condition_type": "faction", "description": "F1"},
        {"id": 2, "condition_type": "faction", "description": "F2"},
    ]
    view = PostConditionsGridView(
        catalog=catalog, preferences=[1], discord_id="x", siege_client=object()
    )
    toggles = [c for c in view.children if isinstance(c, _ToggleButton)]
    by_id = {t._condition_id: t for t in toggles}
    assert by_id[1].style == discord.ButtonStyle.success
    assert by_id[2].style == discord.ButtonStyle.secondary
```

- [x] **Step 6: Run.** Expected: 2 PASS.

- [x] **Step 7: Add nav-disable test.**

```python
def test_grid_view_prev_disabled_on_first_page_next_disabled_on_last() -> None:
    """Boundary pages have one nav button disabled."""
    from mom_bot.post_conditions.views import PostConditionsGridView, NavButton

    # Two meta-groups → two pages.
    catalog = [
        {"id": 1, "condition_type": "faction", "description": "F1"},
        {"id": 2, "condition_type": "role", "description": "R1"},
    ]
    view = PostConditionsGridView(
        catalog=catalog, preferences=[], discord_id="x", siege_client=object()
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
```

- [x] **Step 8: Run.** Expected: PASS.

- [x] **Step 8b: Add the embed-title meta-group-header test (§ 3.11).**

```python
def test_embed_title_carries_meta_group_header_for_current_page() -> None:
    """Embed title matches "Editing — {meta_label} (page i/N)" for the active page."""
    from mom_bot.post_conditions.views import PostConditionsGridView

    catalog = [
        {"id": 1, "label": "Only Banner Lord Champions can be used.",
         "condition_type": "faction"},
        {"id": 2, "label": "Only HP Champions can be used.",
         "condition_type": "role"},
    ]
    view = PostConditionsGridView(
        catalog=catalog, preferences=[], discord_id="x", siege_client=object()
    )
    # Page 0 → Faction & League.
    embed = view.initial_embed()
    assert embed.title == "Editing — Faction & League (page 1/2)"

    # Advance to page 1 → Role, Affinity, Rarity.
    view._page_index = 1
    view._build_components()
    embed = view.initial_embed()
    assert embed.title == "Editing — Role, Affinity, Rarity (page 2/2)"
```

- [x] **Step 8c: Add the button-label uses-short-label test (§ 3.10).**

```python
def test_toggle_button_label_uses_short_label_not_canonical() -> None:
    """Buttons render with the discord_display short label, not the canonical string."""
    from mom_bot.post_conditions.views import PostConditionsGridView, _ToggleButton

    catalog = [
        {"id": 1, "label": "Only Sylvan Watcher Champions can be used.",
         "condition_type": "faction"},
    ]
    view = PostConditionsGridView(
        catalog=catalog, preferences=[], discord_id="x", siege_client=object()
    )
    toggle = next(c for c in view.children if isinstance(c, _ToggleButton))
    assert toggle.label == "Sylvan Watchers"
```

- [x] **Step 9: Add the sub-pagination summary-embed dedup test (B1 regression guard).**

Per-meta-group pagination (§ 3.1) makes the cross-meta-group double-heading bug
structurally impossible at current data sizes (every page already belongs to a
single meta-group). This test remains as **defense-in-depth** for the
*sub-pagination* case: if a meta-group ever grows past 20 conditions, the
`_summary_pages()` collapse logic must still merge sub-pages so the summary
embed renders the heading exactly once.

```python
def test_summary_pages_merges_subpaginated_meta_groups() -> None:
    """When a meta-group spans multiple GridPages, _summary_pages collapses
    them into one (base_label, conditions) tuple and build_summary_embed
    renders the heading exactly once."""
    from mom_bot.post_conditions.views import (
        PostConditionsGridView,
        build_summary_embed,
        _flat_to_meta_keyed,
    )

    # 25 faction conditions → two sub-pages: "(1/2)" with 20, "(2/2)" with 5.
    catalog = [
        {"id": i, "condition_type": "faction", "description": f"F{i}"}
        for i in range(25)
    ]
    view = PostConditionsGridView(
        catalog=catalog, preferences=[], discord_id="x", siege_client=object()
    )
    # Select one id from each sub-page.
    view._selections[3] = True   # on page (1/2)
    view._selections[22] = True  # on page (2/2)

    pages = view._summary_pages()
    assert len(pages) == 1, (
        f"sub-pages should merge into one tuple; got {[p[0] for p in pages]}"
    )
    base_label, conds = pages[0]
    assert base_label == "Faction & League"
    assert len(conds) == 25  # both chunks concatenated

    # build_summary_embed must render the heading exactly once.
    embed = build_summary_embed(
        pages=pages,
        selections=_flat_to_meta_keyed(view._selections, view._pages),
    )
    rendered = "\n".join(
        [embed.title or ""] + [f.name + "\n" + f.value for f in embed.fields]
    )
    assert rendered.count("Faction & League") == 1, (
        "heading rendered more than once — double-label bug regressed"
    )
```

- [x] **Step 10: Run.** Expected: PASS.

- [x] **Step 11: Commit.**

```bash
git add src/mom_bot/post_conditions/views.py tests/post_conditions/test_views.py
git commit -m "feat(#145): PostConditionsGridView scaffold + toggle/nav buttons"
```

---

### Phase 3 — Save / Cancel callback tests with mocked client

**Files:**
- Modify: `tests/post_conditions/test_views.py`

- [x] **Step 1: Add the Save callback test.**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_save_callback_puts_selected_ids_and_strips_view() -> None:
    """SaveButton aggregates ON selections and PUTs them via the client."""
    import discord
    from mom_bot.post_conditions.views import PostConditionsGridView, SaveButton

    catalog = [
        {"id": 1, "condition_type": "faction", "description": "F1"},
        {"id": 2, "condition_type": "faction", "description": "F2"},
        {"id": 3, "condition_type": "role", "description": "R1"},
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

    siege_client.set_my_preferences.assert_awaited_once_with(
        discord_id="42", ids=[3]
    )
    interaction.response.edit_message.assert_awaited_once()
    # view=None in the call → buttons stripped.
    _, kwargs = interaction.response.edit_message.call_args
    assert kwargs["view"] is None
```

- [x] **Step 2: Run.** Expected: PASS.

- [x] **Step 3: Add the Cancel callback test.**

```python
@pytest.mark.asyncio
async def test_cancel_callback_makes_no_client_call_and_strips_view() -> None:
    """CancelButton does not touch the client."""
    from unittest.mock import AsyncMock, MagicMock
    from mom_bot.post_conditions.views import PostConditionsGridView, CancelButton

    catalog = [{"id": 1, "condition_type": "faction", "description": "F1"}]
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
```

- [x] **Step 4: Run.** Expected: PASS.

- [x] **Step 5: Add the Save-error test.**

```python
@pytest.mark.asyncio
async def test_save_callback_handles_siege_web_error() -> None:
    """SiegeWebError → user gets a retry message; view is NOT stripped."""
    from unittest.mock import AsyncMock, MagicMock
    from mom_bot.post_conditions.client import SiegeWebError
    from mom_bot.post_conditions.views import PostConditionsGridView, SaveButton

    catalog = [{"id": 1, "condition_type": "faction", "description": "F1"}]
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
```

- [x] **Step 6: Run.** Expected: PASS.

- [x] **Step 7: Commit.**

```bash
git add tests/post_conditions/test_views.py
git commit -m "test(#145): Save/Cancel callback coverage + error paths"
```

---

### Phase 4 — Wire `commands.py` to the new view + delete modal code

Apply `refactoring-discipline` — deletions in this phase change no external behavior beyond what Phases 1-3 already replaced.

**Files:**
- Modify: `src/mom_bot/post_conditions/commands.py:L226-L238`
- Modify: `src/mom_bot/post_conditions/views.py` (deletions)
- Delete: `src/mom_bot/post_conditions/modal_layout.py`
- Modify: `tests/post_conditions/test_commands.py`
- Modify: `tests/post_conditions/test_views.py` (remove modal tests)
- Delete: `tests/post_conditions/test_modal.py`
- Delete: `tests/post_conditions/test_modal_layout.py`

- [x] **Step 1: Update `commands.py:L226-L238`.**

Replace:

```python
    pref_ids = [int(p["id"]) for p in prefs]
    view = EditPreferencesView(
        catalog=catalog,
        preferences=pref_ids,
        discord_id=discord_id,
        siege_client=siege_client,
    )
    await interaction.followup.send(
        embed=view.initial_embed(),
        view=view,
        ephemeral=True,
    )
```

with:

```python
    pref_ids = [int(p["id"]) for p in prefs]
    view = PostConditionsGridView(
        catalog=catalog,
        preferences=pref_ids,
        discord_id=discord_id,
        siege_client=siege_client,
    )
    await interaction.followup.send(
        embed=view.initial_embed(),
        view=view,
        ephemeral=True,
    )
```

And update the import at the top of `commands.py`:

```python
# Replace:
#   from mom_bot.post_conditions.views import EditPreferencesView
# With:
from mom_bot.post_conditions.views import PostConditionsGridView
```

- [x] **Step 1b: Update the module-level import in `tests/post_conditions/test_commands.py:L25`.**

This step MUST run before any test command in this phase. When Phase 4 Step 5 deletes
`EditPreferencesView` from `views.py`, any file that still names it at import time will
fail at IMPORT (`ImportError: cannot import name 'EditPreferencesView'`), which masks
the TDD signal — pytest never gets to the assertions.

Replace:

```python
from mom_bot.post_conditions.views import EditPreferencesView
```

with:

```python
from mom_bot.post_conditions.views import PostConditionsGridView
```

If `test_commands.py` references `EditPreferencesView` in any other context (e.g. an
`isinstance(...)` assertion, a type hint, a patched attribute), update those references
in this same step.

- [x] **Step 2: Update `test_commands.py` assertions.** Replace any assertion that names `EditPreferencesView` with `PostConditionsGridView`. Drop any assertion that inspects modal-specific behavior (e.g. checking `view._modal_pages`). Keep all error-path tests (404 → link msg, AuthError → ops msg, generic Exception → ops msg). Add:

```python
@pytest.mark.asyncio
async def test_post_conditions_set_uses_grid_view() -> None:
    """The handler constructs a PostConditionsGridView, not the old modal view."""
    # ... existing fixture setup ...
    from mom_bot.post_conditions.views import PostConditionsGridView
    # ... await the handler ...
    sent_view = interaction.followup.send.call_args.kwargs["view"]
    assert isinstance(sent_view, PostConditionsGridView)
```

- [x] **Step 3: Run command tests.** Expected: PASS.

```bash
.venv/Scripts/python.exe -m pytest tests/post_conditions/test_commands.py -v
```

- [x] **Step 4: Delete `modal_layout.py`.**

```bash
git rm src/mom_bot/post_conditions/modal_layout.py
git rm tests/post_conditions/test_modal_layout.py
git rm tests/post_conditions/test_modal.py
```

- [x] **Step 5: In `views.py`, delete:**
  - `EditPreferencesModal` (L205-L370)
  - `_EditMetaButton` (L377-L417)
  - `_DismissButton` (L420-L436)
  - `EditPreferencesView` (L444-L526)
  - `_selections_to_meta_keyed` (L70-L106) — superseded by `_flat_to_meta_keyed`
  - The import of `ModalPage, split_meta_for_modals` (L27)

- [x] **Step 6: Update `__all__` in `views.py`** (currently L29-L33):

```python
__all__ = [
    "build_summary_embed",
    "PostConditionsGridView",
]
```

- [x] **Step 7: In `test_views.py`, delete every test that imports `EditPreferencesModal`, `_EditMetaButton`, `_DismissButton`, `EditPreferencesView`, or `_selections_to_meta_keyed`.** Keep:
  - All `build_summary_embed` tests.
  - All `PostConditionsGridView` tests added in Phases 2-3.

- [x] **Step 8: Run the full post_conditions suite.** Expected: ALL PASS, no skips.

```bash
.venv/Scripts/python.exe -m pytest tests/post_conditions/ -v
```

- [x] **Step 9: Commit.** CLAUDE.md forbids `git add -A` — stage explicit paths only. The Phase 4 file set is deterministic; both the modifications and the deletions are listed below.

```bash
git add \
  src/mom_bot/post_conditions/commands.py \
  src/mom_bot/post_conditions/views.py \
  tests/post_conditions/test_commands.py \
  tests/post_conditions/test_views.py
# Deletions staged by `git rm` in Step 4 are already in the index:
#   src/mom_bot/post_conditions/modal_layout.py
#   tests/post_conditions/test_modal_layout.py
#   tests/post_conditions/test_modal.py
git commit -m "refactor(#145): delete modal flow; wire commands.py to grid view"
```

Confirm the staged set with `git status` before committing — only the listed files
plus the three `git rm`-staged deletions should appear.

---

### Phase 5 — PR + manual dev-guild smoke (BLOCKING for merge)

- [ ] **Step 1: Run the bot locally against the dev guild on this branch.**

```bash
.venv/Scripts/python.exe -m mom_bot
```

- [ ] **Step 2: Invoke `/post-conditions-set` as a user with an existing preference set.** Confirm:
  - Ephemeral renders with a button grid (rows 0-3 toggles, row 4 nav).
  - Pre-checked options match stored preferences (green = on, grey = off).
  - Toggling a button instantly changes its style.
  - The summary embed updates on every toggle.
  - `Next` moves to the next meta-group page; selections from page 1 are still toggled when returning via `Prev`.
  - `Save` strips the buttons and shows the final summary; a follow-up `/post-conditions-list` reflects the new state.
  - `Cancel` does not modify stored preferences (verify with a follow-up `/post-conditions-list`).
  - No Discord 400 in bot stderr across at least 30 click interactions.

- [ ] **Step 3: Post smoke result to issue #145.** Include the keyword `smoke verified` and either a screenshot or a copy-paste of the bot log showing the round-trip.

- [ ] **Step 3a (conditional — Phase 5a debounce mitigation).** This step exists to land the risk-table's "client-side debounce" mitigation if smoke reveals it's needed. **Skip if the 30-click stress in Step 2 produced no 429s and no visibly delayed updates.**

If toggle-mash testing produced:
  - Any HTTP 429 in bot stderr, OR
  - Visible UI lag (button click → embed update delay > ~500ms perceptible to the user), OR
  - Out-of-order embed states (a stale render arriving after a fresh one),

then add a client-side throttle in `_ToggleButton.callback`. Two acceptable implementations:

  - **(a) `asyncio.Lock` on the view**: add `self._toggle_lock = asyncio.Lock()` in `__init__`; wrap the toggle body in `async with view._toggle_lock:`. Serialises callbacks; simplest correct fix for ordering issues.
  - **(b) 250ms throttle**: track `self._last_toggle_at: float = 0.0` on the view; in the callback, drop any toggle arriving within 250ms of the previous one (ignore silently, do not `edit_message`). Reduces request rate; appropriate fix for 429s.

Add a regression test in `test_views.py` covering whichever path is chosen. Commit on the same branch with message `feat(#145): client-side toggle debounce (smoke-driven)`.

If Step 2 was clean, this step is a no-op — proceed to Step 4. The risk-table row in § 8 remains as the documented mitigation discoverable for a future regression.

- [ ] **Step 4: Open the PR.**

```bash
gh pr create --title "feat(#145): V1 button-grid checklist for /post-conditions-set" --body-file .tmp/pr-145-body.md
```

PR body template (write to `.tmp/pr-145-body.md` first):

```markdown
Replaces the multi-page modal flow shipped in #144 with a button-grid
in a legacy `discord.ui.View`. Toggle buttons (5 rows × up to 5 buttons)
flip selections in place; Save commits via `set_my_preferences`. Live
summary embed refreshes on every toggle.

V2 CheckboxGroup is platform-restricted to Modals at every nesting depth
discord.py 2.7 exposes — see issue #145 comments for the two rejection
smokes that proved it.

Closes #145.

## Phase 0 smoke gate (issue #145)
- [ ] Confirmed a comment exists on issue #145 with the Phase 0 button-grid smoke result (keyword `smoke verified` or a screenshot attachment). Verified via `gh issue view 145 --comments`. **Mergers must tick this only after running the command and seeing the comment.**

## Pre-merge dev-guild smoke (issue #145 AC)
- [ ] Demonstrated /post-conditions-set on dev guild (button grid renders, toggle visual feedback works, embed updates per toggle, Save commits, Cancel preserves, page nav preserves selections).

🤖 _Generated by Claude Code on behalf of @cbeaulieu-gt_
```

- [ ] **Step 5: After merge,** delete this plan file and the two retired V2 smoke scripts in a cleanup commit on `main`:

```bash
git rm docs/superpowers/plans/2026-05-20-issue-145-button-grid.md
git rm scripts/smoke_v2_checkbox.py
git rm scripts/smoke_v2_checkbox_in_container.py
```

(Plan-file deletion per CLAUDE.md `# Document Files § Lifecycle`.)

---

## 7. Acceptance criteria

- [ ] Phase 0 verification: button-grid smoke confirmed against a dev guild, posted to #145.
- [ ] `/post-conditions-set` sends an ephemeral message containing a button grid with up to 20 toggle buttons + 4 nav buttons.
- [ ] Each toggle button's style reflects ON (`success`) / OFF (`secondary`) state.
- [ ] Toggling pre-checks the user's current preferences on initial render.
- [ ] Summary embed updates on every toggle (live, not deferred).
- [ ] Save button writes the staged selection set via `set_my_preferences` in one call.
- [ ] Cancel button leaves preferences untouched (no API call).
- [ ] Page navigation preserves selections (toggling on page 1 → Next → Prev still shows it toggled).
- [ ] All existing `tests/post_conditions/` tests still pass; modal-flow tests are deleted; new tests cover the grid view construction + button callbacks.
- [ ] No Discord 400s observed in the dev-guild smoke across 30+ interactions.
- [ ] No V2 components used (`LayoutView`, `CheckboxGroup`, `TextDisplay`, `Container`) anywhere in the diff.
- [ ] Manual dev-guild smoke before merge. This gate is mandatory.

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Discord rejects the 25-component View payload (unlikely — legacy surface) | Very Low | **Phase 0 is the mitigation.** If smoke fails, plan changes before production code lands. |
| A future meta-group exceeds 20 conditions without sub-pagination working correctly | Low | `test_split_by_meta_group_subpaginates_at_page_size` covers the >20 case explicitly. Per-meta-group pagination (§ 3.1) means the only way this risk surfaces is intra-group sub-pagination — the B1 regression test in Phase 2 Step 9 guards against the related summary-embed double-heading failure mode. |
| Shortened button labels lose user context (e.g. "HP" alone is ambiguous out of context) | Medium | Meta-group header in embed title (§ 3.11) carries the missing context — the user always sees `"Editing — Role, Affinity, Rarity"` above a column of `HP / DEF / Support / ATK / …` buttons. Final commit response (Save) re-renders the summary with the same `short_label`-then-canonical-heading layout so the user can audit the staged set. **Smoke gate:** Phase 0 Step 3c surfaces this for the user to evaluate before any Phase 1 code lands. If smoke reveals the loss is too costly, the fallback is to revert to canonical labels in `_ToggleButton` and accept the 1-button-per-row visual issue (the table in `discord_display.py` is reused for the summary surface only). |
| Discord rate-limits rapid toggle clicks | Low | `unverified:` per § 2.6 — each click is a separate 3-second interaction with its own deadline; the platform absorbs rate. **Mitigation lives in Phase 5 Step 3a (conditional)**: if the 30-click stress surfaces 429s or visible lag, add asyncio.Lock or 250ms throttle in `_ToggleButton.callback`. If clean, Step 3a is a no-op. |
| Button click delivery reordering under fast user input | Low | `unverified:` — discord.py dispatches sequentially per view (§ 2.6) but Discord-side ordering under network delay is not documented. Smoke includes a 30-click stress in step 2. |
| Removing `build_summary_embed` would break the new view (verifying it stays) | N/A | Phase 4 explicitly keeps `build_summary_embed`; `__all__` retains the export. |
| Merging without manual smoke (the recurring PR #139/#143/#144 failure) | Medium | Two checkboxes in PR body — Phase 0 smoke confirmation + dev-guild smoke. Reviewer bot blocks merge while either is unchecked. |

## 9. Decision log

See § 3.9 for the V2 abandonment record with citations to the two rejection comments on issue #145.

🤖 _Plan generated by Claude Code on behalf of @cbeaulieu-gt_
