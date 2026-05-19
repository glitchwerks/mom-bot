---
title: "/post-conditions-set Button + Modal + CheckboxGroup UX (#138)"
issue: 138
touches:
  - src/mom_bot/post_conditions/commands.py
  - src/mom_bot/post_conditions/views.py
  - src/mom_bot/post_conditions/grouping.py
  - src/mom_bot/post_conditions/modal_layout.py
  - pyproject.toml
  - tests/post_conditions/test_views.py
  - tests/post_conditions/test_commands.py
  - tests/post_conditions/test_modal_layout.py
  - tests/post_conditions/test_modal.py
skills_relevant:
  - python
  - refactoring-discipline
---

# /post-conditions-set: Button + Modal + CheckboxGroup UX (#138)

## 1. Problem Statement

`/post-conditions-set` currently uses a paginated `discord.ui.Select`-based view
(`PostConditionsView` in `src/mom_bot/post_conditions/views.py:L205-L470`).
#136 / #137 (merged at commit `392677a`) added a live-updating summary embed to
work around Discord's truncated chip rendering on closed Select widgets, but the
overall UX still feels cramped — Selects don't render long labels well and
require page navigation to see/edit everything.

discord.py 2.7 ships `discord.ui.CheckboxGroup` /
`discord.ui.Checkbox` /
`discord.CheckboxGroupOption` (component type 22, modal-only). The verified
class lives at `.venv/Lib/site-packages/discord/ui/checkbox.py:L61-L281`.
Replacing the Select+pagination View with a Button that opens a
CheckboxGroup-bearing Modal gives full-width labels, native default-state
pre-checking, and removes page navigation from the hot path.

## 2. Sources (verified)

All claims about discord.py 2.7 API surface below are verified against the
installed library at `.venv/Lib/site-packages/discord/` (no version pin file
read because discord.py is installed editable via the project venv; the
checkbox module exists which only ships in 2.7+ per the in-source
`.. versionadded:: 2.7` directive at `checkbox.py:L64`).

| Claim | Source |
| --- | --- |
| `CheckboxGroup` and `Checkbox` are exported from `discord.ui` | `.venv/Lib/site-packages/discord/ui/__init__.py:L30` re-exports `.checkbox`; `checkbox.py:L52-L56` has `__all__ = ('CheckboxGroup', 'Checkbox')` |
| `CheckboxGroup` is added in discord.py 2.7 | `checkbox.py:L64` `.. versionadded:: 2.7` |
| `CheckboxGroup` allows up to 10 options | `checkbox.py:L73-L74` docstring and `checkbox.py:L238-L239` `if len(self._underlying.options) >= 10: raise ValueError` |
| `CheckboxGroup.__init__` accepts `options=[CheckboxGroupOption(...)]`, `min_values`, `max_values`, `required`, `custom_id` | `checkbox.py:L93-L116` |
| Per-option `default=True` pre-checks the box | `checkbox.py:L206-L207` docstring; `CheckboxGroupOption` constructed at `checkbox.py:L215-L220` |
| Submitted values come back as `CheckboxGroup.values: list[str]` of the option `value`s | `checkbox.py:L129-L132` (property) and `checkbox.py:L262-L265` `_handle_submit` writes `data['values']` into `self._values` |
| Modals cap total children at 5 | `.venv/Lib/site-packages/discord/ui/modal.py:L270-L273` `if len(self._children) >= 5: raise ValueError('maximum number of children exceeded (5)')` |
| `CheckboxGroup.width == 5` (occupies entire modal "row") | `checkbox.py:L252-L254` |
| Modal `on_submit` is the canonical post-submit hook | `discord/ui/modal.py` `Modal` class shape (BaseView subclass; `_dispatch_submit` schedules `_scheduled_task` → `on_submit`) |
| Current view at `views.py:L205-L470`, summary embed at `views.py:L67-L160`, commit path at `views.py:L337-L364` | Repo files (read directly) |
| META_GROUPS shape `[("Faction & League", ["faction","league"]), ("Role, Affinity, Rarity", ["role","affinity","rarity"]), ("Effects & Other", ["effect","other"])]` | `src/mom_bot/post_conditions/grouping.py:L23-L27` |
| Existing client write path: `SiegeWebClient.set_my_preferences(discord_id, ids)` | `src/mom_bot/post_conditions/client.py:L482-L512` |

## 3. Decision Log

### D1. One CheckboxGroup per Modal (forced by library constraints)

A `CheckboxGroup` has `width = 5` (`checkbox.py:L252-L254`) and a Modal's
`add_item` rejects a 6th child outright (`modal.py:L270-L273`). The 5-width
budget per row applies to layout, but the hard cap on total children is also 5.
A `CheckboxGroup` consuming `width=5` therefore fills a row by itself.

Empirically that means **one `CheckboxGroup` per Modal**, regardless of whether
later discord.py versions raise the width budget. We commit to "1 group per
modal" because it (a) matches the verified API, (b) keeps each modal's content
focused on one meta-page, and (c) sidesteps any ambiguity about whether the
modal-row-budget is 5 or something larger in a future API rev.

This contradicts the issue body's optimistic "one CheckboxGroup per meta-page,
single modal" framing. The plan therefore implements **Button-per-meta-page**.

### D2. Sub-pagination when a meta-page exceeds 10 conditions

CheckboxGroup caps options at 10 (`checkbox.py:L238-L239`). Several meta-pages
in this app can exceed that (e.g. "Effects & Other" combines `effect` and
`other`, which together can have dozens of catalog entries).

When a meta-page exceeds 10 conditions, split it into deterministic sub-pages
of ≤10 each (sort by `condition_type` then by catalog `id` to keep ordering
stable across catalog refreshes). Render one button per sub-page labelled
e.g. "Edit Effects & Other (1/2)".

### D3. Retire `PostConditionsView` entirely; keep `build_summary_embed`

`PostConditionsView` (`views.py:L205-L470`) is structurally tied to Select +
pagination — page navigation, per-page selection state, Prev/Next/Commit/Cancel
buttons. None of that survives the redesign. **Delete the class.**

`build_summary_embed` (`views.py:L67-L160`) is retained because it is the
read-only summary renderer used by:

1. The ephemeral message that hosts the new `EditPreferencesView` (it shows
   the user the current state alongside the "Edit ..." buttons).
2. The post-submit refresh inside each modal's `on_submit` (the ephemeral is
   re-edited with a freshly-rendered embed).

Its L385-L545 unit tests already lock its contract, which makes it cheap to
keep and risky to rewrite. Note: the prior version of this decision claimed
the function is consumed by `/post-conditions-get` — that was wrong;
`/post-conditions-get` has its own rendering path. The honest justification is
the two consumers above.

The new entry-point View is a tiny `EditPreferencesView(discord.ui.View)` with:

- One `EditMetaButton` per meta-page sub-page (1 button if ≤10 options, N
  buttons if split). Each button opens a Modal containing one CheckboxGroup
  with that sub-page's options.
- A `Dismiss` button (no-op; just removes the ephemeral).

The view holds `selections: dict[int, bool]` keyed by **catalog condition id**
(flat, not meta-label-keyed), seeded from the user's saved preferences at
command time. Each modal submit updates the matching slice of `selections`,
calls
`SiegeWebClient.set_my_preferences(discord_id, ids=[id for id, on in selections.items() if on])`,
then re-renders the ephemeral summary embed via `build_summary_embed`.

#### D3a. Adapter between flat `selections` and `build_summary_embed`

`build_summary_embed(pages, selections: dict[str, set[int]])` takes the
**old meta-label-keyed shape** (see signature at
`src/mom_bot/post_conditions/views.py:L67-L70`). The new view's
`selections: dict[int, bool]` is flat by condition id. Three resolutions
were considered:

1. Change `build_summary_embed`'s signature to accept the flat shape.
2. Inline a dict-comprehension conversion at every call site.
3. **Extract a named pure converter function.** *(chosen)*

Option 3 wins because:

- The converter is pure and unit-testable independently of any Discord
  interaction surface or event loop.
- `build_summary_embed`'s existing signature is preserved verbatim, so the
  L385-L545 unit tests stay literally as-is (per Phase 5 retention plan).
- It makes the adapter explicit and named — not buried inside `on_submit` or
  duplicated across the two call sites listed above.

**Where it lives:** `src/mom_bot/post_conditions/views.py` (module-private
helper, alongside `build_summary_embed`). Keeping it co-located with its only
direct consumer avoids growing the public surface of `modal_layout.py` with
a function that has nothing to do with modal layout.

**Signature:**

```python
def _selections_to_meta_keyed(
    selections: dict[int, bool],
    pages: list[tuple[str, list[dict[str, Any]]]],
) -> dict[str, set[int]]:
    """Convert flat {id: bool} to {meta_label: {id, ...}} for build_summary_embed."""
```

Implementation walks `pages` (the existing `group_by_meta(...)` output), and
for each `(label, conditions)` collects `{cond["id"] for cond in conditions
if selections.get(int(cond["id"]), False)}` into the result under `label`.
Returns `{}` for the empty case; `build_summary_embed` already handles
"no selected ids" gracefully.

### D4. No live updates inside the modal (forced by Discord)

Modals are blocking — there is no interaction surface inside a modal that
fires before submit. The summary embed updates only between modals, on
ephemeral refresh after a successful submit. Per-modal cancellation (user
closes without submitting) leaves state untouched — this is intrinsic to the
Modal lifecycle (no submit means `on_submit` does not fire and `set_my_preferences`
is never called), so no explicit cancel handling is needed for "cancel a single
edit". The view-level Dismiss button only removes the ephemeral.

### D5. Atomic-per-modal writes, not "save all on close"

Each modal submit performs an immediate PUT through `set_my_preferences`. This
is unlike the old "Commit" button which batched all pages. Rationale:

- Modals don't have a Cancel-with-state semantic; the only signals are "submit"
  or "user closed dialog". Treating each submit as authoritative matches Discord
  UX expectations.
- It means partial editing (open meta-page A, submit, walk away) persists what
  was actually edited, instead of being lost.
- Each PUT sends the full preference set (not a diff), so this is no more
  destructive than the current batch save — same endpoint, same shape.

If the PUT fails, surface an ephemeral error and leave local `selections`
unchanged so the user can retry by reopening the same modal (defaults will
still reflect the last-successful state).

## 4. Open Questions

None remaining — the two flagged in the brief are resolved as D1 and D2 above.

## 5. Phased Task List

Plan follows TDD discipline: red tests first, then implementation to green,
then deletion of obsolete tests/code.

### Phase 0 — Pre-implementation: issue comment + converter helper

1. **Post comment on #138** clarifying the AC #2 deviation (per D1). The
   router will post via `mcp__github__add_issue_comment`; the content is:

   > **AC #2 deviation — verified library constraint**
   >
   > `discord.ui.CheckboxGroup` has `width = 5`
   > (`.venv/Lib/site-packages/discord/ui/checkbox.py:L252-L254`) and
   > `discord.ui.Modal.add_item` rejects a 6th child outright
   > (`modal.py:L270-L273`). One `CheckboxGroup` therefore fills a modal by
   > itself — a single modal cannot host multiple meta-pages.
   >
   > **Revised AC #2:** "Each meta-page becomes one or more buttons (one per
   > ≤10-option sub-page). Clicking a button opens a modal with one
   > CheckboxGroup for that sub-page." This keeps the user-visible
   > button → modal → checkboxes flow intact; only the literal "one modal,
   > many CheckboxGroups" framing changes.
   >
   > Full rationale in `docs/superpowers/plans/2026-05-19-issue-138-checkbox-modal.md`
   > § D1.
   >
   > 🤖 _Generated by Claude Code on behalf of @cbeaulieu-gt_

2. **Tests for `_selections_to_meta_keyed`** in
   `tests/post_conditions/test_views.py` — red first:
   - Empty `selections` → `{}`.
   - All-False `selections` → `{}` (no labels in result; falsy entries
     dropped).
   - Mixed selections distribute correctly into meta-labelled buckets per
     `META_GROUPS` ordering.
   - Ids in `selections` that are not present in `pages` are silently
     ignored (defensive — stale state should not crash render).
   - Conditions whose `meta_label` is absent from `pages` (empty bucket) do
     not appear as keys in the result.
3. Implement `_selections_to_meta_keyed` in `views.py` adjacent to
   `build_summary_embed`. Run tests to green.

This phase ships **before** the modal class so Phase 2's `on_submit` tests
can call the converter directly with no `...` placeholder.

### Phase 1 — Modal + sub-pagination helpers (pure functions)

Goal: extract pure helpers so they can be unit-tested without a Discord event
loop.

1. **New module `src/mom_bot/post_conditions/modal_layout.py`** — small,
   discord-free helper module containing:
   - `split_meta_for_modals(conditions: list[dict[str, Any]]) -> list[ModalPage]`
     where `ModalPage = NamedTuple('ModalPage', label=str, conditions=list[dict])`.
   - Logic: call `group_by_meta` (existing), then for each `(meta_label, conds)`,
     chunk into slices of ≤10 sorted by `(condition_type, id)`. Label each slice
     `f"{meta_label}"` if a single chunk, else `f"{meta_label} ({i}/{n})"`.
2. **Tests at `tests/post_conditions/test_modal_layout.py`** — red first:
   - Single chunk when meta-page has ≤10 conditions: 1 ModalPage, label
     unchanged.
   - Split when >10: N ceil(len/10) pages, labels carry `(1/N)`, `(2/N)`, etc.
   - Empty meta-page produces no ModalPage.
   - Sort is deterministic across catalog refreshes (build two inputs with
     same condition ids in different order, expect equal output).
3. Implement until green.

### Phase 2 — `EditPreferencesModal` (single CheckboxGroup, one ModalPage)

Goal: the modal class that handles one sub-page.

Tests for the modal live in a **new file**
`tests/post_conditions/test_modal.py` (separate from `test_views.py` to keep
the modal lifecycle setup — fake `Interaction`, fake `CheckboxGroup._values`
injection — out of the embed-test module).

1. **Tests at `tests/post_conditions/test_modal.py`** — red:
   - Build a modal from a `ModalPage` + the parent view's `selections`;
     assert the emitted `CheckboxGroup.options` has correct labels, `value`
     is the stringified condition id, and `default=True` exactly for ids
     where `parent_view.selections[id] is True`.
   - `min_values=0` (allow deselecting everything) and
     `max_values=len(options)`.
   - Modal title carries the sub-page label, truncated to Discord's 45-char
     modal title limit (`modal.py:L88`).
   - `on_submit` success path: stub `siege_client.set_my_preferences`,
     simulate submit by setting `CheckboxGroup._values` directly (mirrors
     `_handle_submit` at `checkbox.py:L262-L265`), call `on_submit(interaction)`,
     assert:
     - `parent_view.selections` is updated for ids in this sub-page (ids
       outside this sub-page untouched).
     - `set_my_preferences` called once with `discord_id` and the merged
       preference id list (every id where `selections[id] is True`).
     - `interaction.response.edit_message` called with the freshly-rendered
       embed (via `build_summary_embed(pages, _selections_to_meta_keyed(
       parent_view.selections, pages))`) and `view=parent_view`.
   - `on_submit` failure path: `set_my_preferences` raises → ephemeral
     error message via `interaction.response.send_message(..., ephemeral=
     True)`, `parent_view.selections` rolled back to pre-submit state, no
     exception propagates.
   - **Ported from L550-L606 of old `test_views.py`** (see Phase 5):
     - `test_modal_submit_rerenders_summary_embed` — the L550-L576 intent
       ("summary embed is re-rendered on every preference change") expressed
       against the new architecture: submit a modal, assert
       `edit_message(embed=..., view=...)` is called with an embed whose
       description reflects the post-submit `selections`.
     - `test_sequential_modal_submits_accumulate_selections` — the
       L579-L606 intent ("navigation across pages preserves selections")
       expressed against the new architecture: submit modal A, then submit
       modal B, assert the final `set_my_preferences` call contains the
       union of ids checked across both submits.

2. Implement `EditPreferencesModal(discord.ui.Modal)` in `views.py`:
   - `__init__(self, *, page: ModalPage, parent_view: EditPreferencesView,
     siege_client: SiegeWebClient, discord_id: int, pages:
     list[tuple[str, list[dict[str, Any]]]])` — `pages` is threaded so
     `on_submit` can pass it to the converter and `build_summary_embed`.
   - Adds one `CheckboxGroup` populated from `page.conditions`, with
     `default=True` for ids where `parent_view.selections[id] is True`.
   - `async def on_submit(self, interaction)` — exact shape (no `...`):

     ```python
     # 1. Snapshot for rollback on PUT failure.
     prior = dict(self.parent_view.selections)

     # 2. Update flat selections for this sub-page only.
     submitted_ids = {int(v) for v in self.group.values}
     sub_page_ids = {int(c["id"]) for c in self.page.conditions}
     for cid in sub_page_ids:
         self.parent_view.selections[cid] = cid in submitted_ids

     # 3. Push the full preference set to siege-web.
     try:
         await self.siege_client.set_my_preferences(
             self.discord_id,
             ids=[cid for cid, on in self.parent_view.selections.items() if on],
         )
     except Exception:  # narrow in implementation to the client's exception type
         self.parent_view.selections = prior
         await interaction.response.send_message(
             "Could not save preferences — please try again.",
             ephemeral=True,
         )
         return

     # 4. Refresh the ephemeral message with the converted-shape embed.
     meta_keyed = _selections_to_meta_keyed(self.parent_view.selections, self.pages)
     embed = build_summary_embed(self.pages, meta_keyed)
     await interaction.response.edit_message(embed=embed, view=self.parent_view)
     ```

### Phase 3 — `EditPreferencesView` (entry-point view)

1. **Tests at `tests/post_conditions/test_views.py`** — red:
   - Constructed from `(catalog, preferences, siege_client, discord_id)`;
     `view.selections` is `{id: id in preferences}` for every catalog id.
   - One `EditMetaButton` per `ModalPage` produced by `split_meta_for_modals`.
   - One `DismissButton`.
   - Clicking an `EditMetaButton` calls `interaction.response.send_modal(...)`
     with a correctly-parameterized `EditPreferencesModal`.
   - Clicking Dismiss edits the original message to remove the view (or sends
     a terse "Dismissed" ephemeral) — confirm one of these once we agree
     on the smoothest UX in a smoke test; default to `edit_message(view=None)`.
2. Implement `EditPreferencesView(discord.ui.View)`:
   - Dynamic button creation in `__init__` (loop over modal pages).
   - Button callbacks dispatch to `EditPreferencesModal`.

### Phase 4 — Wire into `/post-conditions-set` command

1. **Tests at `tests/post_conditions/test_commands.py`** — update existing
   `/post-conditions-set` tests (currently `L273-L419`):
   - Drop expectations about `PostConditionsView` being constructed.
   - Assert the ephemeral response embeds the current-state summary and
     attaches an `EditPreferencesView`.
   - Pre-existing "no catalog → friendly error" / "no preferences yet" paths
     unchanged.
2. Modify `commands.py:L178-L238` (`post_conditions_set`):
   - Fetch catalog + current preferences (unchanged).
   - Build `EditPreferencesView` and `build_summary_embed`.
   - `await interaction.followup.send(embed=..., view=..., ephemeral=True)`.

### Phase 5 — Retire `PostConditionsView` + dead tests

Apply `refactoring-discipline`: behavior-preservation does **not** apply here
because the public behavior of `/post-conditions-set` has intentionally changed
(per the issue acceptance criteria). What we preserve is the contract with
`siege-web` (`set_my_preferences`) and the summary embed shape — both already
covered by retained tests.

1. Delete `PostConditionsView` (`views.py:L205-L470`).
2. Delete tests that exercise its internals in `tests/post_conditions/test_views.py`:
   - Construction tests `L106-L153` — replaced by `EditPreferencesView` tests.
   - Pre-pop tests `L161-L191` — replaced (defaults verified in Modal tests).
   - Navigation tests `L198-L281` — gone; no pagination concept anymore.
   - Commit/cancel tests `L288-L378` — replaced by Modal submit + Dismiss tests.
3. **Retention split for `L385-L606`** (the previous version of this plan
   said "keep L385-L606" — that was wrong; L550-L606 are not pure embed
   tests and will break on `PostConditionsView` deletion):
   - **Keep literally as-is: L385-L545** — pure `build_summary_embed` unit
     tests. They construct neither `PostConditionsView` nor any Select; they
     pass `pages` and meta-keyed `selections` dicts directly. Unchanged
     behavior, unchanged signature, unchanged tests.
   - **Port, do not keep: L550-L606** — `test_on_select_rerenders_embed`
     (L550-L576) and `test_prev_next_preserves_embed_selections` (L579-L606)
     construct `PostConditionsView` and exercise its Select callback +
     `go_next` / `go_prev`. These references die with the class. Their
     behavioral *intent* — "summary embed is re-rendered on every preference
     change" and "selections accumulate across page changes" — remains valid
     in the new design (every modal submit re-renders; sequential submits
     accumulate). The ports live in `tests/post_conditions/test_modal.py`
     under the two test names listed in Phase 2 above
     (`test_modal_submit_rerenders_summary_embed`,
     `test_sequential_modal_submits_accumulate_selections`).
4. **Delete `_make_select_interaction`** — after the L550-L606 port, no
   remaining test exercises a Select callback, so the Select-specific
   interaction builder has no consumer. Keep `_make_interaction` only if
   the new tests reuse it; otherwise delete it too.

### Phase 6 — Manual smoke check

Per AC: "Manual smoke check: full-width readable labels in modal."

1. Run the bot against a dev guild.
2. `/post-conditions-set` → confirm ephemeral renders summary embed + buttons.
3. Click each "Edit ..." button → confirm:
   - Modal title is the meta-page label.
   - Checkboxes render full-width with readable labels (not truncated).
   - Currently-saved preferences are pre-checked.
4. Toggle a few, submit → ephemeral summary embed refreshes with the new state.
5. Cancel a modal (X) → ephemeral state unchanged.
6. Dismiss → ephemeral cleared.
7. Repeat with a synthetic catalog that forces a meta-page over 10 entries
   to exercise sub-pagination buttons.

### Phase 7 — PR

Conventional commit + PR body must include `Closes #138` as plain text (not
inside the scope parens). Branch is `feat-138-checkbox-modal`.

## 6. Acceptance Criteria (copied verbatim from #138)

- [ ] `/post-conditions-set` opens ephemeral message with summary embed +
      Edit Preferences button
- [ ] Button opens modal with one CheckboxGroup per meta-page (or per
      ≤10-option sub-page)
- [ ] Checkboxes pre-checked for current preferences
- [ ] On submit, bot writes via existing `set_my_preferences` path
- [ ] After submit, ephemeral refreshes with updated summary embed
- [ ] Cancel/dismiss leaves preferences untouched
- [ ] No regressions in `tests/post_conditions/`; new tests cover modal-build,
      submit, sub-pagination
- [ ] Manual smoke check: full-width readable labels in modal

Note on bullet #1 vs reality: due to D1 (one CheckboxGroup per modal forced by
library constraints), "Edit Preferences" is rendered as one button **per
meta-page sub-page** rather than a single global button. This is the only
honest mapping from the issue's intent to the verified library surface.
Flagged for reviewer awareness — substantively meets the user-visible goal
(button-driven modal flow) but the literal "an Edit Preferences button" phrasing
is plural in the implementation.

## 7. Out of Scope (copied from #138)

- Backend `short_name` field on `PostCondition`.
- Reverting #137 — summary embed is reused.
- Migrating `/post-conditions` or `/post-conditions-get`.

Additionally:

- Any change to `SiegeWebClient.set_my_preferences` semantics or endpoint.
- Any change to `META_GROUPS` ordering or `group_by_meta`.

## 8. Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| discord.py 2.7 not actually installed in deploy environment (Container App pin lags local venv) | Medium | High — runtime ImportError | Bump `pyproject.toml` lower-bound to `discord.py>=2.7` in Phase 4 commit; verify the GHCR build's resolved version in the PR check logs. *Note: the container runs `uv sync --frozen` from `uv.lock`, which already resolves to `discord-py 2.7.1` (verified at `uv.lock:L613-L622`); the `pyproject.toml` bump is constraint-tightening only, not a new runtime dependency.* |
| Atomic-per-modal writes generate more siege-web calls than the old batch commit | Low | Low — siege-web write is idempotent PUT, not append | Acceptable; if abuse becomes a concern, add client-side dedupe later. Not in scope. |
| User edits the same sub-page twice in quick succession with stale defaults | Low | Low | Each modal is constructed fresh from `parent_view.selections`, which is updated on every successful submit; subsequent opens see fresh defaults. |
| Discord's modal title 45-char limit (`modal.py:L88`) truncates `"Role, Affinity, Rarity (1/2)"` etc. | Low | Cosmetic | "Role, Affinity, Rarity (1/2)" = 28 chars; longest current label well under cap. If a future meta-label grows, truncate to 45 explicitly. |
| Tests use stubbed CheckboxGroup state; real Discord behavior might differ | Low | Medium | Phase 6 manual smoke check is the empirical gate; tests verify our wiring, smoke check verifies Discord's. |
| Plan ships before discord.py version-bump merges and CI fails | Low | Low | Phase 4 is the first phase that imports `discord.ui.CheckboxGroup`; the pyproject bump lands in that same commit. |

## 9. Definition of Done

- All ACs in §6 checked, including manual smoke check.
- `pytest tests/post_conditions/` green.
- `pyproject.toml` lower-bound bumped to `discord.py>=2.7` (and
  `uv pip install -e ".[dev]"` re-run locally).
- PR open with `Closes #138` in the body, all CI checks green.
- `PostConditionsView` and its tests deleted; no orphan imports.
- This plan file deleted in the PR that closes #138 (per the lifecycle rule in
  global CLAUDE.md: plan files are execution artifacts, not durable docs).
