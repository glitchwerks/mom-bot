---
title: "Redesign /post-conditions-set as a Components V2 ephemeral checklist (closes #145)"
issue: 145
touches:
  - src/mom_bot/post_conditions/commands.py
  - src/mom_bot/post_conditions/views.py
  - src/mom_bot/post_conditions/modal_layout.py
  - tests/post_conditions/test_modal.py
  - tests/post_conditions/test_modal_layout.py
  - tests/post_conditions/test_views.py
  - tests/post_conditions/test_commands.py
  - scripts/smoke_v2_checkbox.py
skills_relevant:
  - python
  - refactoring-discipline
  - superpowers:brainstorming
---

# Redesign /post-conditions-set as a Components V2 ephemeral checklist

Closes [#145](https://github.com/glitchwerks/mom-bot/issues/145). Refs [#138](https://github.com/glitchwerks/mom-bot/issues/138), PR [#144](https://github.com/glitchwerks/mom-bot/pull/144) (merged at `343ebaf`).

## 1. Problem statement

PR #144 shipped `/post-conditions-set` as a multi-page **modal** flow (`EditPreferencesModal` + `_EditMetaButton`). Modals are wrong for this use case:

- Modals max out at 5 components per page and require multi-step navigation across meta-groups.
- The dismiss/save flow is awkward — users must finish *all* pages before any save lands.
- The 25-component cap on a regular `View` forces sub-pagination even within a single meta-group.

Discord Components V2 (`LayoutView` + `CheckboxGroup` + `TextDisplay`, max **40** child components per message; see § 2.1) lets us render the full preferences picker in a single ephemeral message with one `CheckboxGroup` per meta-page (sub-paginated at 10 options per group). One Save button writes the union of all selections; one Cancel button discards.

## 2. Sources (verified 2026-05-20)

All citations are against the in-tree venv at `.venv/Lib/site-packages/discord/` (discord.py 2.7) — these are the source of truth for what the running bot will accept.

### 2.1 `LayoutView` is the V2-aware view; total child cap is 40

`.venv/Lib/site-packages/discord/ui/view.py:L819-L847`:

```python
class LayoutView(BaseView):
    """... supports all component types and uses what Discord refers to as "v2 components" """
    ...
    def __init__(self, *, timeout: Optional[float] = 180.0) -> None:
        super().__init__(timeout=timeout)
        if self._total_children > 40:
            raise ValueError('maximum number of children exceeded (40)')
```

By contrast, the legacy `View.add_item` rejects V2 items (`view.py:L789-L790`: `if item._is_v2(): raise ValueError('v2 items cannot be added to this view')`) and caps at 25 (`view.py:L786`).

### 2.2 The `components_v2` flag is set automatically (covers `followup.send` too)

`.venv/Lib/site-packages/discord/webhook/async_.py:L595-L603` is the shared payload-builder used by `InteractionResponse.send_message`, `InteractionResponse.edit_message`, `Webhook.send`, and the `Webhook.send`-backed `InteractionResponse.followup.send`. The flag handling therefore applies uniformly across all three send paths used in this codebase:

```python
if view is not MISSING:
    if view is not None:
        data['components'] = view.to_components()
        if view.has_components_v2():
            if flags is not MISSING:
                flags.components_v2 = True
            else:
                flags = MessageFlags(components_v2=True)
```

**Implication:** the caller does **not** need to pass `flags=MessageFlags(components_v2=True)` explicitly to `interaction.response.send_message(view=layout_view, ephemeral=True)` **nor** to `interaction.followup.send(view=layout_view, ephemeral=True)`. discord.py 2.7 detects `has_components_v2()` (which is `any(c._is_v2() for c in self.children)`, `view.py:L310-L311`) and sets the flag for any send path that routes through this helper. Tests should assert on the view shape, not the flag.

### 2.3 `edit_message` supports in-place V2 view swap

`.venv/Lib/site-packages/discord/interactions.py:L1120-L1248` — `InteractionResponse.edit_message` accepts `view: Optional[Union[View, LayoutView]]` and routes through the same `interaction_message_response_params` helper as `send_message`, so the `components_v2` flag is preserved across edit. Per the docstring (`interactions.py:L1161-L1165`):

> To update the message to add a `LayoutView`, you must explicitly set the `content`, `embed`, `embeds`, and `attachments` parameters to either `None` or an empty array, as appropriate.

**Implication:** "no flicker, no double-message" (AC) is achievable via `interaction.response.edit_message(view=updated_view, content=None, embeds=[], attachments=[])`. The `_response_type` is set to `message_update` (`interactions.py:L1242`), which Discord treats as an in-place edit.

### 2.4 `CheckboxGroup` supports `default=True` and `max_values`

`.venv/Lib/site-packages/discord/ui/checkbox.py:L61-L220`. Each `CheckboxGroupOption` accepts `default: bool = False` (L187, L206-L207). `CheckboxGroup` accepts `max_values: Optional[int]` (L99). Selection results are exposed via `.values` (L131) as a list of selected option values.

### 2.5 `TextDisplay` content cap is 4000 characters

`.venv/Lib/site-packages/discord/ui/text_display.py:L52-L54` docstring: `content: :class:`str` — The content of this text display. Up to 4000 characters.` This is distinct from the legacy `Embed.description` cap of 4096. The new constant `_TEXT_DISPLAY_MAX_CHARS = 4000` replaces `_EMBED_MAX_CHARS` (views.py:L61) in the summary-builder truncation logic.

### 2.6 Component-count budget

- Hard cap: 40 child components on a `LayoutView` (§ 2.1).
- Each meta-page renders as: one `TextDisplay` (label) + one `CheckboxGroup` (≤10 options). The `CheckboxGroup` is one component regardless of option count.
- Plus: one root `TextDisplay` (summary header) + Save button + Cancel button = 3 fixed.
- Worst-case meta-page count tolerable: `(40 - 3) / 2 ≈ 18` meta-pages with one CheckboxGroup each. If a meta-group exceeds 10 options it sub-paginates into multiple groups with `(i/N)` titles per the issue body, consuming additional component slots; current data has well under this ceiling.

## 3. Decision log

### 3.1 Phase 0 verification is mandatory and blocking — runs as a manual script

`scripts/smoke_v2_checkbox.py`: a one-shot CLI that logs the bot in, registers a one-time `/v2-smoke` slash command on a dev guild, responds to it with a `LayoutView` containing one `TextDisplay` + one `CheckboxGroup` (5 sample options, one with `default=True`) + a Save button + a Cancel button. Bot author runs the script, invokes `/v2-smoke` in the dev guild, and confirms the payload renders correctly (full-width labels, pre-checked default, no Discord 400).

**Rationale for script (not pytest, not silent unit construction):**

- Issues #138 / #142 / PRs #139 #143 #144 all shipped without manual smoke and broke in prod. Unit tests that assert "view has 4 children" do not prove Discord accepts the payload.
- A pytest test cannot exercise the real Discord API — it can only assert against discord.py's local serialization. The whole point of Phase 0 is to confirm Discord's server-side reaction.
- The script is throwaway (delete in Phase 7 cleanup or keep as a smoke utility — author's call).

**Phase 1 is blocked on Phase 0.** The plan body marks it `BLOCKED ON PHASE 0` and PR #145 will not be opened until the smoke output (screenshot or copy-paste of the rendered message) is attached.

### 3.2 No explicit `MessageFlags(components_v2=True)` in production code

Per § 2.2, discord.py auto-sets the flag from `view.has_components_v2()`. Adding an explicit flag is redundant and creates two sources of truth. Tests will not assert on the flag — they will assert that the view is a `LayoutView` instance and that `view.has_components_v2()` returns `True`.

### 3.3 Re-render uses `edit_message`, not a second `send_message`

Per § 2.3, the Save callback calls `interaction.response.edit_message(view=updated_view, content=None, embeds=[], attachments=[])`. The Cancel callback also uses `edit_message` to strip the buttons and leave the TextDisplay in its current state — preferences are untouched server-side.

### 3.4 `build_summary_embed` is replaced by a TextDisplay markdown builder

A `LayoutView` with `components_v2=True` rejects `Embed`s (`flags.py:L547-L554`: "Does not allow sending any `content`, `embed`, `embeds`, `stickers`, or `poll`"). We replace the rendered `discord.Embed` with a markdown string emitted into `discord.ui.TextDisplay`. The `_selections_to_meta_keyed` helper (`views.py:L70-L106`) stays — it produces the meta-grouped dict that the markdown builder iterates.

### 3.5 Component ordering matches `META_GROUPS`

`LayoutView` renders children in `add_item` order. The new constructor adds: root `TextDisplay` → for each meta-page in canonical META_GROUPS order, add `TextDisplay(title)` + `CheckboxGroup(options)` → trailing `ActionRow` with Save + Cancel buttons. No ordering surprises.

**`(i/N)` suffix scope.** The `(i/N)` suffix on meta-page titles scopes to sub-groups of a **single meta-page**: a meta-page with 12 conditions sub-paginates into "Foo (1/2)" + "Foo (2/2)". `i` resets per meta-page. Across the entire message, there is no global "page i of N" anymore — all meta-pages and sub-groups display simultaneously inside the same ephemeral.

### 3.6 Save semantics — staged-write, not per-toggle PUT

Toggling checkboxes does **not** issue any `set_my_preferences` call. The view only calls `set_my_preferences` on the Save button callback, writing the union of all `CheckboxGroup.values` in one PUT. Cancel discards staged changes without any server call. This is a deliberate behavior change from the prior modal flow (which PUT per-modal-page).

### 3.7 `_DismissButton` is deleted, not subclassed

`_DismissButton.callback` (views.py:L430-L436) calls `interaction.response.edit_message(view=None)` with no `content`, `embeds`, or `attachments` kwargs. Per § 2.3 the V2 in-place edit path **requires** these be explicitly set to `None`/`[]`, so any V2 cancel path needs a different signature. Writing `CancelButton(discord.ui.Button)` fresh in Phase 1 — rather than inheriting from `_DismissButton` — eliminates the risk of an implementer retaining the modal-era arg pattern. `_DismissButton` is deleted unconditionally in Phase 4.

## 4. Out of scope

- Changes to `grouping.py`, `client.py`, `test_grouping.py`, `test_client.py`.
- Changes to the catalog API or `set_my_preferences` PUT contract — the V2 view reads/writes through the same client surface.
- Reworking the `(i/N)` sub-pagination algorithm for meta-pages > 10 options — keep existing logic, just emit one `CheckboxGroup` per sub-page instead of one `Modal` per sub-page.
- Embeds in any other command (`/post-conditions-list` etc.) — they remain on the v1 Embed path.

## 5. Phased task list

### Phase 0 — Live smoke (BLOCKING, manual)

Goal: prove that a `LayoutView` with a `CheckboxGroup` actually renders on a dev guild before writing any production code.

- [ ] Write `scripts/smoke_v2_checkbox.py`. It logs into the bot, registers a guild-scoped `/v2-smoke` command on the configured dev guild, and on invocation responds with:
  - `LayoutView(timeout=300)` containing
  - `TextDisplay("Smoke: V2 CheckboxGroup")`
  - `CheckboxGroup(placeholder=None, options=[Option(label=f"opt-{i}", value=str(i), default=(i==2)) for i in range(5)], min_values=0, max_values=5)`
  - `Button(label="Save")` and `Button(label="Cancel")` (callbacks: log selected values, then `edit_message(view=None, content="ack")`).
- [ ] Bot author runs `python scripts/smoke_v2_checkbox.py` against the dev guild.
- [ ] Bot author invokes `/v2-smoke` in the dev guild.
- [ ] Confirm: (a) message renders ephemerally, (b) `opt-2` is pre-checked, (c) Save button records the user's selection set, (d) Cancel dismisses cleanly, (e) Discord returns no 400.
- [ ] Attach smoke output (screenshot or copy-paste of the bot logs showing selected values) to issue #145 as a comment **before** opening any phase-1 PR.

**Exit criterion:** confirmed dev-guild render with pre-checked default and accepted Save callback. If Discord rejects the payload or rendering is degraded, stop and revise the plan — do not proceed.

### Phase 1 — V2 view scaffold (no callbacks) [BLOCKED ON PHASE 0]

Goal: construct `PostConditionsV2View(discord.ui.LayoutView)` and prove it serializes correctly.

- [ ] Add `PostConditionsV2View(discord.ui.LayoutView)` in `src/mom_bot/post_conditions/views.py`.
  - Constructor takes `pages: list[tuple[str, list[dict]]]`, `current_preferences: set[int]`, `summary_markdown: str`.
  - Adds root `discord.ui.TextDisplay(summary_markdown)`.
  - For each `(meta_label, conditions)` page, sub-paginates `conditions` at 10 items per chunk. For each chunk i of N, adds `TextDisplay(f"{meta_label} ({i}/{N})")` + `CheckboxGroup(options=[CheckboxGroupOption(label=c["label"], value=str(c["id"]), default=(c["id"] in current_preferences)) for c in chunk], min_values=0, max_values=len(chunk))`.
  - Adds two `Button`s: `SaveButton(discord.ui.Button)` and `CancelButton(discord.ui.Button)` — **both written fresh in this phase as direct `discord.ui.Button` subclasses**. Do **not** inherit `CancelButton` from `_DismissButton`; the signatures are incompatible (see § 3.7). Callbacks stubbed `pass` for Phase 1.
- [ ] Add `tests/post_conditions/test_v2_view.py`:
  - Assert `view.has_components_v2()` is `True` (per § 2.2 this is the durable signal).
  - Assert child count matches `1 + sum(2 * sub_pages_per_meta) + 2` (root TextDisplay + each meta's TextDisplay+CheckboxGroup pairs + 2 buttons).
  - Assert each `CheckboxGroupOption` with id in `current_preferences` has `default=True`.
  - Assert `_total_children <= 40` for a representative full-catalog fixture (use the existing post-conditions fixture).

### Phase 2 — Save / Cancel callbacks

- [ ] Implement `SaveButton.callback(interaction)`:
  - Walk `self.view.children`, collecting `CheckboxGroup.values` from each (these are `List[str]` of selected option values per § 2.4).
  - Union into `set[int]` of condition IDs.
  - Call `set_my_preferences(client, interaction.user.id, ids=union_set)` via the existing client.
  - Build the new `summary_markdown` via the new TextDisplay markdown builder (§ Phase 5).
  - Rebuild the view with the updated preferences set as defaults.
  - `await interaction.response.edit_message(view=new_view, content=None, embeds=[], attachments=[])`.
- [ ] Implement `CancelButton.callback(interaction)`:
  - `await interaction.response.edit_message(view=None, content="Cancelled — preferences unchanged.", embeds=[], attachments=[])` (or keep the TextDisplay-only view; choose the path that satisfies the AC "Cancel leaves preferences untouched" — no client call, just UI strip).
- [ ] Tests in `test_v2_view.py`:
  - Mock `discord.Interaction.response.edit_message` and the client's `set_my_preferences`. Drive a synthesized Save callback with pre-set `CheckboxGroup._underlying.values` to simulate user selection. Assert client called with the right union. Assert `edit_message` called with new view.
  - Drive Cancel callback. Assert client **not** called. Assert `edit_message` called with `view=None` or stripped view.

### Phase 3 — Wire into `/post-conditions-set`

The real send-call shape (verified at `commands.py:L201` + `:L234-L238`) is **`defer(ephemeral=True)` followed by `interaction.followup.send(embed=..., view=..., ephemeral=True)`** — not `interaction.response.send_message`. The defer is required because the catalog/preferences fetches at L204-L207 are network calls that can blow the 3-second initial-response window. Per § 2.2 the same payload-builder handles `followup.send`, so `components_v2` auto-flagging works identically.

- [ ] In `src/mom_bot/post_conditions/commands.py:L226-L238`, replace the `EditPreferencesView(...)` construction with `PostConditionsV2View(catalog=catalog, preferences=pref_ids, ...)`.
- [ ] Preserve the existing `await interaction.response.defer(ephemeral=True)` at L201 — it remains compatible with V2 (defer does not lock the follow-up's response format; the follow-up is a fresh message creation).
- [ ] Rewrite the send call to:
  ```python
  await interaction.followup.send(
      view=PostConditionsV2View(...),
      ephemeral=True,
  )
  ```
  No `embed=` kwarg (rejected by V2 flag; § 3.4 + § 3.7). No explicit `flags=` (auto-set per § 2.2). The TextDisplay component carries the summary that the old `view.initial_embed()` used to render.
- [ ] Update `tests/post_conditions/test_commands.py` accordingly — drop embed assertions, assert the view is a `LayoutView`, assert `followup.send` was called (not `response.send_message`) and was called with `view=<LayoutView>, ephemeral=True` and **no** `embed` kwarg.

### Phase 4 — Delete the modal flow

Apply `refactoring-discipline` — these deletions change no external behavior beyond what Phases 1–3 already replaced.

- [ ] Delete `src/mom_bot/post_conditions/modal_layout.py` (88 lines).
- [ ] In `src/mom_bot/post_conditions/views.py`, delete:
  - `EditPreferencesModal` (`L205-L370`)
  - `_EditMetaButton` (`L377-L417`)
  - `_DismissButton` (`L420-L436`) — **unconditional delete** per § 3.7; do not subclass.
  - `EditPreferencesView` (`L444-L526`)
  - `initial_embed` helper at `L512-L526` (Embed-bearing; rejected by V2 flag).
  - `build_summary_embed` (`L109-L202`) and `_EMBED_MAX_CHARS` (`L61`) — confirmed by grep that `build_summary_embed` has no callers outside `views.py`, so delete outright and introduce `_TEXT_DISPLAY_MAX_CHARS = 4000` + `build_summary_markdown` fresh in Phase 5.
  - Keep `_selections_to_meta_keyed` (`L70-L106`).
- [ ] Update `views.py`'s `__all__` (currently `["build_summary_embed", "EditPreferencesModal", "EditPreferencesView"]` at L29-L33):
  - Remove `build_summary_embed`, `EditPreferencesModal`, `EditPreferencesView`.
  - Add `PostConditionsV2View` and `build_summary_markdown`.
- [ ] Delete `tests/post_conditions/test_modal.py` (568 lines).
- [ ] Delete `tests/post_conditions/test_modal_layout.py` (195 lines).
- [ ] In `tests/post_conditions/test_views.py`, delete the `L429-L487` modal-button-click block. Refactor the remainder against the new V2 view (or move into `test_v2_view.py` and delete `test_views.py` entirely if nothing useful remains).
- [ ] Run the full `tests/post_conditions/` suite. All remaining tests pass, no skipped tests.

### Phase 5 — Replace `build_summary_embed` with TextDisplay markdown

- [ ] Introduce `_TEXT_DISPLAY_MAX_CHARS = 4000` in `views.py` (cite: `.venv/Lib/site-packages/discord/ui/text_display.py:L52-L54`; see § 2.5).
- [ ] Add `build_summary_markdown(selections_by_meta: dict[str, set[int]], pages, catalog: dict[int, dict]) -> str` in `views.py`. Mirror the existing `build_summary_embed` formatting (`L109-L202`) but emit markdown instead of an `Embed`:
  - One `## {meta_label}` per meta key.
  - Bulleted `- {label}` lines per selected condition under each meta.
  - Truncation behavior bounded by `_TEXT_DISPLAY_MAX_CHARS` (4000) instead of `_EMBED_MAX_CHARS` (4096). Preserve the existing `… and N more` overflow-suffix shape from `build_summary_embed:L188`.
- [ ] Migrate the existing `test_views.py` unit tests (each by name + intent — these are the four current tests against `build_summary_embed`):
  - `test_build_summary_embed_empty` → `test_build_summary_markdown_empty`: assert no `##` headers and a sensible empty-state string when no selections are set.
  - `test_build_summary_embed_single_meta` → `test_build_summary_markdown_single_meta`: assert `"## Some Meta"` header present and one `"- label"` line per selected condition.
  - `test_build_summary_embed_multi_meta_ordering` → `test_build_summary_markdown_multi_meta_ordering`: assert meta-page order in output matches `META_GROUPS` canonical order.
  - `test_build_summary_embed_truncates_overflow` → `test_build_summary_markdown_truncates_overflow`: assert `len(output) <= _TEXT_DISPLAY_MAX_CHARS` **and** that the output ends with an `"… and N more"` marker. This is the highest-failure-risk test (suffix off-by-one shape); cover it explicitly.
- [ ] Drop all Embed-field assertions (`embed.fields[...]`, `embed.description`, etc.); replace with markdown-shape assertions.

### Phase 6 — Manual dev-guild smoke (BLOCKING for merge)

Per issue AC #8 ("Manual dev-guild smoke before merge. This gate is mandatory.") and the lesson from PRs #139 / #143 / #144.

- [ ] Bot author runs the bot locally against the dev guild with the feature branch checked out.
- [ ] Invokes `/post-conditions-set` as a user with an existing preference set.
- [ ] Confirms:
  - Ephemeral message renders with one CheckboxGroup per meta-page.
  - Pre-checked options match the user's stored preferences.
  - Toggling a checkbox and pressing Save updates the message in-place (no second message; no flicker).
  - Pressing Cancel does not modify stored preferences (verify with a follow-up `/post-conditions-list`).
  - **`.values` round-trip check.** Toggle some checkboxes, press Save, observe the message re-render. Then toggle *different* checkboxes and Save again. Confirm the second Save sees the post-first-Save state (not stale pre-first-Save state) as defaults — i.e. that selections round-trip through the API and back into the next `PostConditionsV2View` constructor as updated `default=True` values. Unit tests cannot prove this (they mock `edit_message`); only a live round-trip can.
- [ ] PR description includes an **unchecked smoke checkbox** that the merger ticks only after live demo:
  ```markdown
  ## Pre-merge smoke gate (issue #145 AC)
  - [ ] Demonstrated /post-conditions-set on dev guild (CheckboxGroup renders, defaults match, Save edits in-place, Cancel preserves)
  ```
  Reviewers (claude-action-runner) block merge while this is unchecked.

### Phase 7 — PR

- [ ] Open PR against `main` with:
  - Title: `feat(#145): Components V2 ephemeral checklist for /post-conditions-set`
  - Body includes `Closes #145` and `Refs #138` (plain text per CLAUDE.md `# Pull Requests`).
  - **Phase 0 paper-trail checkbox** (this is the BLOCKING-1 enforcement gate that the reviewer bot will check):
    ```markdown
    ## Phase 0 smoke gate (issue #145)
    - [ ] Confirmed a comment exists on issue #145 with the Phase 0 smoke result (keyword `smoke verified` or a screenshot attachment). Verified via `gh issue view 145 --comments`. **Mergers must tick this only after running the command and seeing the comment.**
    ```
    Rationale: PRs #139 / #143 / #144 all bypassed equivalent self-discipline gates. An unchecked box on the PR body lets `claude-action-runner` block the merge — the same gate that caught #143 (and that an admin override bypassed for #139). Self-discipline alone is not enough; the paper-trail must be machine-visible.
  - Smoke-gate checklist from Phase 6.
  - Mandatory Claude attribution footer.
- [ ] After merge, delete `scripts/smoke_v2_checkbox.py` or relocate to a smoke-utilities folder (author's call).
- [ ] After merge, delete this plan file per CLAUDE.md `# Document Files § Lifecycle`.

## 6. Acceptance criteria (verbatim from issue #145)

- [ ] Phase 0 verification: minimal V2-message-with-CheckboxGroup smoke confirmed against a dev guild.
- [ ] `/post-conditions-set` sends an ephemeral Components V2 message containing one CheckboxGroup per meta-page (sub-paginated at 10 options, with (i/N) group titles).
- [ ] Each CheckboxGroup pre-checks the user's current preferences.
- [ ] Save button writes the combined selection set via existing `set_my_preferences` path.
- [ ] Cancel button leaves preferences untouched.
- [ ] Changes are staged locally until Save is pressed — no `set_my_preferences` call is issued on individual checkbox toggles; Cancel discards all staged changes without any server call. (See § 3.6; this is a deliberate behavior change from the prior modal flow.)
- [ ] After Save, the ephemeral re-renders with the updated TextDisplay (no flicker, no double-message).
- [ ] All existing `tests/post_conditions/` tests still pass; modal-flow tests are deleted; new tests cover the V2 message construction + button callbacks.
- [ ] Manual dev-guild smoke before merge. This gate is mandatory — PR cannot land without an actual user-flow demonstration.

## 7. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Discord rejects the V2 payload (400) for a shape discord.py 2.7 serializes but the API does not yet accept | Medium — V2 is new; surface area is wide | **Phase 0 is the mitigation.** If smoke fails, plan changes before any production code lands. |
| `CheckboxGroup.values` semantics differ across the response/edit cycle (e.g., values reset after `edit_message`) | Low-Medium | Phase 2 callbacks test this against a synthesized interaction; if it bites, fall back to storing selections in the view's state dict between callbacks. |
| 40-component cap exceeded with current full catalog | Low — current data fits | Phase 1 test asserts `_total_children <= 40` against the real catalog fixture; if it ever fails, add a page-bucketing layer before shipping. |
| Removing `build_summary_embed` breaks an off-screen caller | Low | `Grep build_summary_embed` before deletion; keep the function if any other command still uses it. |
| Merging without manual smoke (the recurring PR #139/#143/#144 failure) | Medium — historical | Smoke checkbox in PR body is unchecked at open; reviewers block merge until ticked. Mandatory per AC #8. |

## 11. Review Response Log

Findings from project-reviewer's pass on the initial revision; resolved in this revision (2026-05-20).

- **BLOCKING-1 (Phase 0 smoke gate enforcement)** — Added a Phase 0 paper-trail checkbox to the Phase 7 PR-body template that names the verification command (`gh issue view 145 --comments`) and the comment-content signal (`smoke verified` keyword or screenshot). Reviewer bot can now BLOCK on the unchecked box; self-discipline no longer the sole gate.
- **BLOCKING-2 (`commands.py` uses `followup.send`, not `send_message`)** — Rewrote Phase 3 to preserve the existing `defer + followup.send` pattern at `commands.py:L201` + `:L234-L238`. Added § 2.2 confirmation that the same payload-builder (`webhook/async_.py:L595-L603`) auto-flags `components_v2` for `followup.send`. Phase 3 now specifies the exact `followup.send(view=..., ephemeral=True)` shape with no `embed=` and no `flags=`.
- **BLOCKING-3 (`_DismissButton` fate)** — Added § 3.7 making the deletion unconditional with the signature-incompatibility rationale. Phase 1 now mandates `CancelButton(discord.ui.Button)` as a fresh class (not subclass of `_DismissButton`). Phase 4 deletes `_DismissButton` unconditionally — the "if not reused" hedge is removed.
- **BLOCKING-4 (Phase 5 test migration covers truncation)** — Phase 5 now lists all four migrating tests by old name → new name + intent, with `test_build_summary_markdown_truncates_overflow` getting an explicit two-part assertion (length cap **and** `… and N more` suffix). Verified `_TEXT_DISPLAY_MAX_CHARS = 4000` against `discord/ui/text_display.py:L52-L54` and added § 2.5 with the citation.
- **CONCERN-5 (Save's union-write semantics)** — Added § 3.6 documenting the staged-write semantics, and added a new AC bullet to § 6 making the "no per-toggle PUT; Cancel discards without server call" behavior explicit.
- **CONCERN-6 (`(i/N)` suffix semantics)** — Added a clarifying paragraph to § 3.5: `i` resets per meta-page, suffix scopes to sub-groups of a single meta-page, no global "page i of N" across the message.
- **CONCERN-7 (`.values` round-trip after `edit_message`)** — Added the two-Save round-trip check to Phase 6's manual smoke list, with an inline note that unit tests cannot verify this because they mock `edit_message`.
- **NIT-8 (`_EMBED_MAX_CHARS` rename decision)** — Resolved as a clean delete + introduce: Phase 4 unconditionally deletes both `build_summary_embed` and `_EMBED_MAX_CHARS`; Phase 5 introduces `_TEXT_DISPLAY_MAX_CHARS = 4000` and `build_summary_markdown` fresh. Tentative "rename if so" language removed.
- **NIT-9 (`__all__` update)** — Phase 4 deletion checklist now includes the explicit `__all__` rewrite: remove `build_summary_embed`, `EditPreferencesModal`, `EditPreferencesView`; add `PostConditionsV2View`, `build_summary_markdown`.

All nine findings resolved as suggested; no deviations.

🤖 _Plan generated by Claude Code on behalf of @cbeaulieu-gt_
