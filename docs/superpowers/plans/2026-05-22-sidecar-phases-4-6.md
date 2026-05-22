---
title: mom-bot Epic #128 Phases 4–6 — outbound write endpoints
touches:
  - src/mom_bot/sidecar/app.py
  - src/mom_bot/sidecar/
  - tests/sidecar/
skills_relevant:
  - python
---

# mom-bot Epic #128 — Phases 4–6 Execution Plan

**Parent epic:** `glitchwerks/mom-bot#128` — mom-bot replaces siege-web's bundled bot+sidecar
**Parent scoping plan:** `docs/superpowers/plans/2026-05-20-mom-bot-epic-128-phase-1-scoping.md` (Rev 4, APPROVED 2026-05-21)
**Phase 3 baseline (just merged):** `474fe38` — `/api/members` + `/api/members/{discord_user_id}` (PR #185)
**Authoritative contract:** `../siege-web/bot/INTERFACE.md` + `../siege-web/backend/tests/integration/sidecar/` (tests win on disagreement)
**Created:** 2026-05-22

## 1. Purpose

Implement the three remaining outbound-write sidecar endpoints that complete Epic #128's HTTP surface, closing issues `#178`, `#179`, `#180` and unblocking the coord contract `glitchwerks/rsl-mom-apps#3` (acceptance criterion #2: `contracts/sidecar-api.yaml`).

## 2. Phase ordering — sequential, not parallel

The three phases are sequential because **Phase 4 (`/api/notify`) is the first write-path endpoint** — it establishes the `discord.NotFound` / `discord.Forbidden` / `discord.HTTPException` translation surface that Phases 5 and 6 reuse. Implementing the three in parallel would produce three slightly-divergent exception-translation paths that would then need a reconciliation pass.

Order:

1. **Phase 4** (`#178`) — `POST /api/notify` — establishes Discord-exception translation pattern.
2. **Phase 5** (`#179`) — `POST /api/post-message` — reuses Phase 4's exception pattern; adds channel-name resolution.
3. **Phase 6** (`#180`) — `POST /api/post-image` — reuses Phase 5's channel-name resolution; adds multipart streaming + Discord CDN URL response shape.

Each phase: its own branch (`issue-<N>`), worktree (`.worktrees/issue-<N>`), and PR with `Closes #<N>` in the body. Phase 5 cuts off updated `origin/main` after Phase 4 merges; Phase 6 cuts off updated `origin/main` after Phase 5 merges.

## 3. Cross-phase invariants

These apply to every endpoint and every test file in this plan:

- **Bearer auth.** Use the existing `_require_bearer` dependency from `src/mom_bot/sidecar/auth.py` (introduced in Phase 2, PR #184). Do not roll new auth.
- **Missing-header response is `403`, not `401`.** This is asserted by `siege-web/backend/tests/integration/sidecar/test_auth.py:29-134` across all four endpoints. The `HTTPBearer(auto_error=True)` scaffold from Phase 2 already produces 403; do not regress.
- **`@everyone` is excluded** from any `roles` / `role_names` list returned by the sidecar — established in Phase 3.
- **Discord-exception translation** — `INTERFACE.md` Quick-Start § "Translate Discord errors to HTTP status codes" + § "Error semantics" is the authoritative mapping. As of plan-write time it reads:
  - `discord.Forbidden` → **403**
  - Discord **4xx** (other than Forbidden) → **502**
  - Discord **5xx** or **timeout** → **503**
  - `discord.NotFound` semantics are endpoint-specific (read paths use 200 + business discriminator per Phase 3 precedent; write paths use 404 when the target is required to exist) — confirm against INTERFACE.md's endpoint-specific table and siege-web's tests.
  - **Always re-verify against INTERFACE.md + `siege-web/backend/tests/integration/sidecar/` at implementation time** — tests win if they disagree with prose.
- **Multi-guild scope.** Endpoints query only the guild bound to `build_app(guild=...)` — provisional option (a) decision documented in the `app.py` module docstring (Phase 3).
- **Request validation.** `RequestValidationError` handler splits 422 (path errors) vs 400 (body/query errors) by inspecting `err["loc"][0]` — established Phase 3, do not regress.
- **Conformance gates.** Where siege-web's integration test under `siege-web/backend/tests/integration/sidecar/` exists for the endpoint, port it as a unit test against the FastAPI `TestClient` and ensure parity on status codes, error shapes, and response keys.

## 4. Verification gate (per phase, pre-PR)

Every phase must pass the **full CI suite** locally before opening the PR:

```bash
cd I:/games/raid/mom-bot/.worktrees/issue-<N>
./.venv/Scripts/python.exe -m ruff check src/ tests/
./.venv/Scripts/python.exe -m black --check src/ tests/
./.venv/Scripts/python.exe -m mypy src/
./.venv/Scripts/python.exe -m pytest        # full suite, no -k filter
```

Per the lesson recorded in the Phase 2 session-summary (`code-writer` skipped `black`, shipped failing format) — **all four tools, in this order**. Scoped or single-tool verification is insufficient.

State the expected pre-implementation test count in the implementer brief (capture via `pytest --collect-only -q | tail -1` on the merge-base before changes).

## 5. PR discipline

- PR body MUST contain a plain-text `Closes #<N>` line (not just in the commit message; squash-merge synthesizes the merge commit from the PR title + body, not from source commits).
- PR body MUST end with the Claude attribution line per global `CLAUDE.md § GitHub Comments`.
- Pre-merge: 7/7 CI green + both `quality-gate` + `quality-gate-shadow` clean before merge (Phase 3 pattern).
- After merge: delete branch + clean worktree via the local `clean-gone` skill.

---

## Task 1 — Phase 4: `POST /api/notify`

**Issue:** `glitchwerks/mom-bot#178`
**Branch:** `issue-178` (worktree already created at `.worktrees/issue-178`)
**Authoritative spec sources** (read order):

1. Issue body of `#178` (scope + conformance gates section)
2. `../siege-web/bot/INTERFACE.md` — `/api/notify` row in the endpoint table + error-semantics table
3. `../siege-web/backend/tests/integration/sidecar/test_notify*.py` if it exists (siege-web's conformance suite)
4. `../siege-web/backend/app/services/bot_client.py:43-99` — caller side; the four sidecar methods (`notify`, `post_message`, `post_image`, `get_members`) swallow `httpx.HTTPError` and return `False`/`None`/`[]`

**Behavior:**

- Method/path: `POST /api/notify`
- Auth: Bearer (existing `_require_bearer` dependency)
- Request body: `{"username": str, "message": str}` (confirm shape against siege-web's caller and INTERFACE.md — if INTERFACE.md says `discord_id` instead of `username`, follow INTERFACE.md).
- Resolution: look up the guild member by username in the guild bound at startup; DM them with `message`.
- Request body confirmed from INTERFACE.md § "POST /api/notify": `{"username": "SomeMember", "message": "Your siege assignment is ready."}`.
- Success: 200 with the JSON shape INTERFACE.md prescribes (verify at impl time).
- `discord.NotFound` (no such member by username) → 404 with structured error body (write-path semantics).
- `discord.Forbidden` (DMs closed / blocked / no shared guild) → **403** per § Error semantics.
- Discord **4xx** (non-Forbidden) → **502**.
- Discord **5xx** or **timeout** → **503**.

**Files touched:**

- `src/mom_bot/sidecar/app.py` — add endpoint + Pydantic request/response models.
- Possibly `src/mom_bot/sidecar/discord_errors.py` (new) — extract the exception-translation helper so Phases 5/6 import it. Implementer decides; if extracted, the helper must be importable.
- `tests/sidecar/test_notify.py` (new) — full conformance coverage: auth (403 on missing), 200 happy path, 404 NotFound, 502 Forbidden, 502 5xx, request validation (422/400 split).

**Definition of done:** all four verification commands pass; PR open with `Closes #178`; siege-web's `test_notify*.py` (if present) ported and green.

## Task 2 — Phase 5: `POST /api/post-message`

**Issue:** `glitchwerks/mom-bot#179`
**Branch:** `issue-179`
**Setup:** after Phase 4 PR merges and `origin/main` is fast-forwarded locally:

```bash
git -C I:/games/raid/mom-bot pull --ff-only origin main
git -C I:/games/raid/mom-bot worktree add I:/games/raid/mom-bot/.worktrees/issue-179 -b issue-179 origin/main
```

**Authoritative spec sources:**

1. Issue body of `#179`
2. `../siege-web/bot/INTERFACE.md` — `/api/post-message` row
3. `../siege-web/backend/tests/integration/sidecar/test_post_message*.py` if present
4. `../siege-web/backend/app/services/bot_client.py:43-99`

**Behavior:**

- Method/path: `POST /api/post-message`
- Auth: Bearer
- Request body shape per INTERFACE.md — likely `{"channel": str, "message": str}` but verify against the spec + tests.
- **Channel-name resolution.** Resolve the channel by exact name in the bound guild. If multiple channels share the name, follow INTERFACE.md's tie-break rule. If none, treat as `discord.NotFound`.
- Reuse Phase 4's `discord_errors` helper (or whatever Phase 4 produced) for exception translation.
- Success/error shapes per INTERFACE.md.

**Files touched:**

- `src/mom_bot/sidecar/app.py` — add endpoint + Pydantic models.
- Possibly `src/mom_bot/sidecar/channel_resolver.py` (new) — extract channel-name resolution if it becomes more than ~10 lines (Phase 6 will reuse).
- `tests/sidecar/test_post_message.py` (new) — full conformance coverage parallel to Task 1.

**Definition of done:** all four verification commands pass; PR open with `Closes #179`; channel-name resolution covered by tests (single match, multi match, no match).

## Task 3 — Phase 6: `POST /api/post-image`

**Issue:** `glitchwerks/mom-bot#180`
**Branch:** `issue-180`
**Setup:** after Phase 5 PR merges and `origin/main` is fast-forwarded:

```bash
git -C I:/games/raid/mom-bot pull --ff-only origin main
git -C I:/games/raid/mom-bot worktree add I:/games/raid/mom-bot/.worktrees/issue-180 -b issue-180 origin/main
```

**Authoritative spec sources:**

1. Issue body of `#180`
2. `../siege-web/bot/INTERFACE.md` — `/api/post-image` row
3. `../siege-web/backend/tests/integration/sidecar/test_post_image*.py` if present
4. `../siege-web/backend/app/services/bot_client.py:43-99` for the `post_image` caller

**Behavior:**

- Method/path: `POST /api/post-image`
- Auth: Bearer
- Multipart upload via FastAPI's `UploadFile`. **Stream the upload** to Discord; do not buffer the whole file in memory.
- Body fields per INTERFACE.md — likely `channel` (form), optional `message` (form), and `file` (binary).
- Reuse Phase 5's channel resolver.
- Response: Discord CDN URL of the uploaded attachment (shape per INTERFACE.md).
- Discord-exception translation reused from Phase 4 helper.

**Files touched:**

- `src/mom_bot/sidecar/app.py` — add endpoint.
- `tests/sidecar/test_post_image.py` (new) — full conformance coverage including streaming (the test can patch `UploadFile` or use FastAPI's test multipart support — implementer chooses).

**Definition of done:** all four verification commands pass; PR open with `Closes #180`; multipart streaming verified (no full-buffer read in the endpoint); CDN URL returned in the documented response shape.

---

## 6. Out of scope

- `contracts/sidecar-api.yaml` (the OpenAPI spec) — that closes `glitchwerks/rsl-mom-apps#3` AC #2 on the **coord repo**, not in mom-bot. After Phase 6 lands, the live API is the source for generating the spec — a separate task on `rsl-mom-apps`.
- Revisiting the multi-guild option (a) decision — provisional, but not part of this execution plan.
- Bot Container App infra changes (no Bicep edits) — those live in their own issues.
