# mom-bot Epic 2.6 — Day-role sync implementation plan

**Parent issue:** `glitchwerks/mom-bot#6` (mom-bot v1.0 milestone — last open issue on it)
**Sibling issue:** `glitchwerks/siege-web#323` (siege-web v1.2 milestone)
**Contract spec (canonical):** `siege-web/docs/webhooks/day-role-sync.md` (delivered by sub-issue **D2** below). Both repos reference this spec as the single source of truth for the wire contract; this plan covers mom-bot's implementation as a **conforming receiver** of that contract.
**Created:** 2026-05-13
**Revised:** 2026-05-13 (round 2 inquisitor charges resolved; bot-agnostic generalization applied)
**Committed:** 2026-05-15 (retrospective commit after A2 #68, A3 #69, B1 #70, B2 #71, D1 #73 merged)
**Status:** GREENLIT for implementation (2 inquisitor passes complete per CLAUDE.md mandate)
**Architecture:** Locked by `glitchwerks/mom-bot#6` issue body (edited 2026-05-13). This plan decomposes that architecture into discrete tasks; it does not redesign it.

---

## 1. Overview

Epic 2.6 delivers automatic Discord role membership for siege day-1 / day-2 assignments. When a member's `attack_day` field is set, cleared, or changed on the siege-web side, siege-web emits a generic **day-role-sync webhook** (per the contract spec in `siege-web/docs/webhooks/day-role-sync.md`). The first conforming receiver — mom-bot — implements the contract via a sidecar endpoint that adds or removes the corresponding admin-managed Discord role on the member's Discord account. Roles persist between sieges until overwritten (no scheduler-driven cleanup).

The contract is **bot-agnostic**: siege-web makes no assumptions about which bot is on the other end of the webhook; switching receivers requires only a configuration change. Mom-bot is the current/first implementer.

The work spans two repos. Mom-bot ships the sidecar endpoint, role-toggle module, and `day_role_map` table first; siege-web then publishes the contract spec (D2) and wires the outbound webhook into the three mutation paths. **Cross-repo gating is enforced by a single feature flag** `DAY_ROLE_SYNC_ENABLED` on the siege-web side (default `false`) — siege-web ships with the flag off, operator flips it once mom-bot's B2 is deployed + smoked, and the same flag serves as the rollback kill switch.

### Verified context (do not re-derive)

- **No portable role-assignment code exists in `I:\games\raid\siege\clan\`.** The only role-touching helper is `clan_reminders.py:107-120` (`send_reminder_with_role()`), which formats a role mention into a channel message — it does not assign roles to users. Plan specifies *build* a fresh `mom_bot/roles/` module.
- **siege-web's `Siege` model has no `kind` / `siege_kind` column** (`siege-web/backend/app/models/siege.py:17-35` — fields are `id, date, status, defense_scroll_count, created_at, updated_at, autofill_preview*, attack_day_preview*, post_suggest_preview*`). Hydra / Chimera are reminder names in mom-bot's `reminders` seed table only. Role map is keyed on `day_number ∈ {1, 2}` alone.
- **`SiegeMember.attack_day` is `int | None` constrained to `IN (1, 2) OR NULL`** (`siege-web/backend/app/models/siege_member.py:17-28`). The transitions to sync are: `NULL → 1|2` (add), `1|2 → NULL` (remove), `1 → 2` or `2 → 1` (swap = remove old + add new).
- **Three siege-web mutation seams** trigger day-assignment changes (verified by reading the handlers 2026-05-13):
  - `siege-web/backend/app/api/siege_members.py:35-41` — `POST /sieges/{siege_id}/members` (`add_siege_member`). Service at `services/siege_members.py:42-77`. Newly-created `SiegeMember` always has `attack_day=NULL` (no API for caller to set it on create); webhook fire on this seam is **always a no-op for day-role purposes**. Kept in the plan defensively in case the create-shape changes later; current behavior emits no HTTP call.
  - `siege-web/backend/app/api/siege_members.py:44-51` — `PUT /sieges/{siege_id}/members/{member_id}` (`update_siege_member`). Service at `services/siege_members.py:80-108` — single-row update, last-write-wins on DB.
  - `siege-web/backend/app/api/attack_day.py:22-30` — `POST /sieges/{siege_id}/members/auto-assign-attack-day/apply` (`apply_attack_day`). Service at `services/attack_day.py:130-163`; mutates many rows in one commit then clears the preview.
  - Member deletion: cascades via `ondelete=CASCADE` on `siege_member.member_id` — no explicit DELETE endpoint to instrument.
- **Outbound HTTP pattern already exists** at `siege-web/backend/app/services/bot_client.py:8-84` (`BotClient` with `httpx.AsyncClient`, Bearer auth, 10s timeout). New day-role-sync method extends this class.
- **`member.discord_id` is `String, nullable=True, unique=True`** (`siege-web/backend/app/models/member.py:22`). Members without a Discord ID must be skipped + logged, not errored.
- **Reminders-seed pattern** at `src/mom_bot/reminders/seed.py:174-318` is the template for A2's seeding: KV name → `discord.utils.get(guild.roles, name=...)` → snowflake → DB row, with CRITICAL log + `ConfigError` + process exit on resolution failure.

---

## 2. Decomposition into sub-issues

Nine sub-issues, grouped into four sequencing tiers. Tier ordering reflects dependencies; within a tier, issues can be worked in parallel.

### Tier A — Pre-work / gates (must complete before tier B)

#### A1. Pre-deploy ops checklist for Discord role hierarchy
**Repo:** mom-bot
**Title:** `chore(epic-2-6): pre-deploy ops checklist — Day 1 / Day 2 roles exist + bot rank above them`
**Why this gates everything:** Discord bots cannot assign a role ranked at or above their own highest role (https://discord.com/developers/docs/topics/permissions, fetched 2026-05-13). If the admin-managed Day 1 / Day 2 roles are ranked above mom-bot's bot role, every sync call will 403. **Runtime enforcement is in B1's preflight self-check** (charge #5/#11 resolution); A1's deliverable is the human-facing pre-deploy checklist, not a runtime audit.
**Acceptance:**
- One-page checklist at `docs/operations/discord-roles-preflight.md`: confirm Day 1 / Day 2 roles exist in dev + prod guilds by name (matching the names that will be configured in `DAY_1_ROLE_NAME` / `DAY_2_ROLE_NAME` KV secrets), confirm mom-bot's bot role is ranked above them, confirm `MANAGE_ROLES` is granted.
- Checklist is referenced from D1's runbook and from mom-bot#6's deployment section.
- No code; pure documentation.
**Depends on:** none.

#### A2. Add `day_role_map` table + startup seed
**Repo:** mom-bot
**Title:** `feat(epic-2-6): add day_role_map(day_number, discord_role_id) table + startup seed by role name`
**Acceptance:**
- New SQLAlchemy model `DayRoleMap` with PK on `day_number`, unique on `discord_role_id`, `CHECK (day_number IN (1, 2))`.
- Alembic migration creates the empty table (no seed data in the migration itself).
- On startup, a `_maybe_seed_day_roles()` function (parallel to `_maybe_seed_reminders` at `src/mom_bot/reminders/seed.py:174-318`) reads `DAY_1_ROLE_NAME` and `DAY_2_ROLE_NAME` from Key Vault via `load_secret()`, resolves each to a snowflake via `discord.utils.get(guild.roles, name=...)`, and UPSERTs into `day_role_map` (idempotent — safe across restarts).
- **Discord role rename detection (round-2 charge #2):** before UPSERT, the seed compares the resolved snowflake against the existing `day_role_map.discord_role_id` row for the same `day_number`. If they differ:
  - Log CRITICAL with event `DAY_ROLE_SNOWFLAKE_CHANGED`, the `day_number`, both old and new snowflakes, and the list of guild members currently holding the **old** role (queried via `guild.get_role(old_snowflake).members`).
  - Raise `ConfigError` → process exits. **Do not** auto-update the row.
  - **Operator remediation (documented in A2's README section and in D1's runbook):**
    1. Manually strip the old role from current holders via Discord UI or an admin tool.
    2. Update KV with the new role name (or revert the role rename in Discord).
    3. Restart the bot. The seed will then UPSERT cleanly because the snowflake won't be changing relative to the (now-stripped) state.
  - **No automated mass role-strip in v1.0** — operator decides whether the rename is intentional and bears the cost of cleanup.
- On any other resolution failure (KV secret missing, role name not present in guild, guild not resolvable): CRITICAL log + raise `ConfigError` → bot exits. Matches reminders-seed behavior verbatim.
- Unit tests: model round-trip, `CHECK` constraint rejects `day_number = 3`, UPSERT idempotency (run twice, single row per day), seed-failure paths (missing KV secret → ConfigError; role-not-found-in-guild → ConfigError; **snowflake-changed-vs-existing-row → ConfigError with old+new logged**).
**Depends on:** none.

#### A3. Install-bitfield / OAuth-scope update
**Repo:** mom-bot (likely just docs + a one-time guild admin action)
**Title:** `chore(epic-2-6): verify mom-bot install bitfield includes MANAGE_ROLES`
**Acceptance:**
- Confirm the bot install URL / current grant includes the `MANAGE_ROLES` (`0x10000000`) guild permission.
- If missing, generate a re-invite URL with the corrected bitfield and document the admin re-install step in `docs/operations/discord-roles-preflight.md` (same doc as A1).
**Depends on:** A1 (checklist captures state).

### Tier B — Mom-bot core (sidecar endpoint + role-toggle module)

#### B1. Build `mom_bot/roles/` module — role-toggle service + hierarchy preflight + runtime detection
**Repo:** mom-bot
**Title:** `feat(epic-2-6): build mom_bot/roles/ — role toggle service + startup hierarchy preflight + runtime hierarchy loss detection`
**Acceptance:**
- New package `src/mom_bot/roles/` exposing a service callable from both the sidecar endpoint and (future) slash commands.
- Service resolves `day_number → discord_role_id` via `day_role_map`, calls discord.py `Member.add_roles()` / `Member.remove_roles()`, and returns a structured result `{status: "applied"|"skipped"|"failed"|"partial", added: bool, removed: bool, reason?: str}`. The `added` and `removed` fields are independently true for each role-mutation attempted by the call (set-action attempts both an add and a remove-of-the-other-day; clear-action attempts two removes).
- Skip reasons enumerated: `member_not_in_guild`, `role_not_seeded`, `already_has_role`, `already_lacks_role`. Failures (403, 5xx) bubble up with the exception type logged. `partial` status fires when one of the two role-mutations succeeded and the other failed (e.g. add succeeded but remove-of-other-day raised) — both booleans reflect actual outcome.
- **Startup preflight self-check** (charge #5/#11): on bot startup after `day_role_map` seed completes, iterate every row, call `guild.me.top_role` once, and compare against each role's position via `guild.get_role(row.discord_role_id).position`. If any mapped role is ranked at or above `guild.me.top_role.position`, log CRITICAL with event `ROLE_HIERARCHY_MISCONFIGURED` and the offending role IDs, then raise `ConfigError` → process exits.
- **Runtime hierarchy-loss detection (round-2 charge #5):** on any `403 Forbidden` from `Member.add_roles()` or `Member.remove_roles()`, re-fetch `guild.me.top_role.position` and the target role's position. If the bot's role is now at-or-below the target role's position (the hierarchy that passed startup preflight has since been changed by an admin), emit a distinct log event `ROLE_HIERARCHY_LOST_AT_RUNTIME` at ERROR level with the offending role IDs, both positions, and the affected member's `discord_id`. Track emitted (role_id) pairs in an in-process `set` so the event is one-per-affected-role-per-process (avoids log spam if many members hit the same broken hierarchy in quick succession). The 403 still surfaces as `status: failed` from the service; the runtime-detection log is additive observability, not control-flow.
- Unit tests mocking discord.py for all five skip/apply branches, plus the `partial` branch (one mutation succeeds, the other raises), the startup-preflight branch (mapped role above bot's top role → ConfigError), and the **runtime-hierarchy-loss branch** (preflight passed at startup → admin moves a role above bot mid-flight → next 403 emits `ROLE_HIERARCHY_LOST_AT_RUNTIME`).
**Depends on:** A2 (table must exist before preflight runs), A3 (permission must be in place — otherwise B1's preflight passes hierarchy but every runtime call still 403s).

#### B2. Sidecar endpoint `POST /api/internal/role-sync`
**Repo:** mom-bot
**Title:** `feat(epic-2-6): add POST /api/internal/role-sync sidecar endpoint`
**Acceptance:**
- New FastAPI route on the existing sidecar app, Bearer-token-gated using the same `discord_bot_api_key` as the other 6 sidecar endpoints.
- Request schema matches the **canonical contract spec** at `siege-web/docs/webhooks/day-role-sync.md` (sub-issue D2): `{discord_id: str, siege_id: int, day_number: int | null, action: "set" | "clear", assigned_at: str (ISO-8601 UTC), correlation_id: str}`. `action="set"` with `day_number=N` adds the day-N role and removes the other day role if present; `action="clear"` removes both day roles.
- Response per the contract spec: `200` with structured result; `400` on invalid schema; `401` on bad bearer. Member-not-in-guild is returned as `200 {status:"skipped", reason:"member_not_in_guild"}` (not 404) so callers do not retry-loop. Example responses:
  - Success: `200 {"status":"applied","added":true,"removed":false}`
  - Partial: `200 {"status":"partial","added":true,"removed":false,"reason":"remove_of_other_day_failed_403"}`
  - Skipped (no Discord member): `200 {"status":"skipped","added":false,"removed":false,"reason":"member_not_in_guild"}`
  - Skipped (unseeded): `200 {"status":"skipped","added":false,"removed":false,"reason":"role_not_seeded"}`
  - Stale-write replay: `200 {"status":"skipped","added":false,"removed":false,"reason":"stale_write","last_assigned_at":"<ts>"}`
- **Persisted idempotency + stale-write rejection (round-2 charges #1 and #3):**
  - New SQLite table `member_role_sync_state(discord_id TEXT PRIMARY KEY, last_assigned_at TEXT NOT NULL, last_action TEXT NOT NULL, last_day_number INTEGER, last_correlation_id TEXT, last_response_status TEXT NOT NULL, last_response_added INTEGER NOT NULL, last_response_removed INTEGER NOT NULL, last_response_reason TEXT)`. Survives restart, no eviction.
  - **Idempotency key:** `(discord_id, assigned_at, action, day_number)`. On request, look up the existing row for `discord_id`:
    1. **Exact replay** (incoming `(assigned_at, action, day_number)` exactly matches the stored row): return the **stored original response** (`status`, `added`, `removed`, `reason`). Do not re-invoke the role service. Log at INFO with event `role_sync_idempotent_replay`, `attempt: 2`-style flag.
    2. **Stale write** (incoming `assigned_at` < stored `last_assigned_at`, AND the key does not exactly match): return `200 {"status":"skipped","reason":"stale_write","last_assigned_at":"<stored>"}`. Do not invoke the role service. Do not update the stored row.
    3. **Fresh write** (incoming `assigned_at` > stored, OR no stored row): invoke the role service, then UPSERT the row with the new key + response payload.
  - Migration ships with B2.
- Integration test posting against the FastAPI TestClient with a mocked `roles/` service: success path, all four skip paths (including `stale_write`), partial path, bearer-auth failure, and **exact-replay returning the stored original response (not `stale_write`)**.
- App Insights / structured log line per call: `role_sync` event with `correlation_id`, `discord_id`, `siege_id`, `day_number`, `action`, `assigned_at`, `status`, `added`, `removed`, and **`attempt: int`** (1 for initial, 2 for replay — round-2 charge #7).
**Depends on:** B1.

### Tier C — siege-web outbound webhook (depends on tier B shipping to a deployed mom-bot AND on D2 contract spec being published)

#### C1. Extend `BotClient` with `sync_day_role()` method + feature flag + retry
**Repo:** siege-web (`glitchwerks/siege-web#323`)
**Title:** `feat(role-sync): add BotClient.sync_day_role() outbound webhook, behind DAY_ROLE_SYNC_ENABLED`
**Acceptance:**
- New async method on `BotClient` (`backend/app/services/bot_client.py`) following the existing 10s-timeout, Bearer-auth pattern. Implements the wire contract published in `siege-web/docs/webhooks/day-role-sync.md` (D2).
- Signature: `async def sync_day_role(discord_id: str, siege_id: int, day_number: int | None, action: Literal["set","clear"], assigned_at: datetime, correlation_id: str) -> bool`. Returns `True` on 2xx, `False` on any HTTP error (matches `notify()` / `post_message()` swallow-and-return-bool convention at `bot_client.py:18-42`).
- Endpoint URL sourced from `settings.DAY_ROLE_SYNC_URL` (env var; the full webhook URL including path, e.g. `https://mom-bot.../api/internal/role-sync`). Bearer secret continues to use the existing bot API key.
- **Internal retry on 5xx** (charge #1/#2): single retry after 500ms backoff on any 5xx response or network error. 4xx is not retried. After one retry, accept failure and return `False`. Retry is internal to `BotClient`; the bool return contract does not change.
- **Correlation ID preserved across retry** (round-2 charge #7): the same `correlation_id` is sent on both the initial call and the retry, so mom-bot's idempotency lookup matches and returns the stored response on the second attempt if the first actually applied server-side. Operators can count retries by querying App Insights for `role_sync` events grouped by `correlation_id`; rows with `attempt: 2` indicate a replay landed on mom-bot.
- **Feature flag** (charge #9/#10): the method checks `settings.DAY_ROLE_SYNC_ENABLED` (env var, default `false`) at call entry. When disabled, the method short-circuits, logs at DEBUG level `role_sync_skipped flag=false`, and returns `True` (does not signal failure — disabling is intentional, not an error). The flag is the cross-repo gate (ship C1 + C2 with mom-bot's B2 undeployed and the system is safe) AND the rollback kill switch (operator flips to `false` to stop the bleeding without redeploying mom-bot). Document the flag in C1's PR body and in D1's runbook.
- Unit test with `httpx.MockTransport` covering 200, 401, 500-then-200 (retry succeeds), 500-then-500 (retry exhausts), timeout, **the same correlation_id used on both initial and retry**, the flag-off short-circuit, and **a test asserting the default value of `DAY_ROLE_SYNC_ENABLED` is `false` and unset → short-circuits before any network call** (round-2 charge #6).
**Depends on:** B2 deployed to dev environment (so the endpoint exists to call once the flag is flipped on), D2 (contract spec published).

#### C2. Wire sync into the mutation seams (incl. correlation_id, assigned_at, bulk summary log)
**Repo:** siege-web
**Title:** `feat(role-sync): fire BotClient.sync_day_role from siege_member add/update + attack_day apply`
**Acceptance:**
- `update_siege_member` (`api/siege_members.py:44-51`, service at `services/siege_members.py:80-108`): after `await session.commit()` and `await session.refresh(siege_member)`, compute the `attack_day` transition (old → new) from the post-commit value (not the request payload) and fire the correct call via `BackgroundTasks`: `set` for `NULL→N`, `clear` for `N→NULL`, `set` for `M→N` (the sidecar handles "remove other day role" atomically per B2 — siege-web does not need to emit two calls). Pass `assigned_at=siege_member.updated_at` (the row's post-commit timestamp) and a fresh `correlation_id` (uuid4).
- `apply_attack_day` (`api/attack_day.py:22-30`, service at `services/attack_day.py:130-163`): after the bulk service call returns, **re-read the committed `SiegeMember` rows** (not the preview blob, not the request payload — charge #8) and iterate them. Implementation reads pre-commit `attack_day` values into a `dict[member_id, old_day]` before commit, then after commit calls `session.refresh(member)` for each affected member (round-2 charge #4) and diffs old vs new. For each row with a `discord_id` and a transition that matters, fan out one HTTP call per member via `BackgroundTasks`, sharing one `correlation_id` across all calls in the bulk batch. `assigned_at` is the post-commit `updated_at` of the row.
- **Bulk fan-out atomicity (round-2 charge #4):** the fan-out loop is wrapped in `try/finally`. The `finally` block emits the `role_sync_bulk_summary` log line with `correlation_id`, `siege_id`, `fired: k of N`, `skipped_no_discord_id: K`, `failed_at_layer: F`, and an explicit `scheduling_failed_at_index: int | None` field. If the loop raises at index k, the k tasks already enqueued via `BackgroundTasks` will still fire (FastAPI does not cancel already-enqueued BackgroundTasks); the summary records k as `fired`. `scheduling_failed_at_index` is `None` on the happy path.
- `add_siege_member` (`api/siege_members.py:35-41`): documented no-op for day-role purposes (newly-created `SiegeMember` always has `attack_day=NULL` per service inspection — see § Verified context). No webhook fire wired here in v1; revisit if the create-shape changes.
- Members without `discord_id` are skipped at the siege-web layer with an `INFO`-level log (do not emit the HTTP call at all — saves a round-trip).
- **Partial-response handling** (charge #6): C2 logs a WARNING when the response body indicates `status: "partial"` (`added != removed` and at least one is true) but does **not** retry — retry is BotClient-internal per charge #1/#2.
- Tests at the API layer asserting `BotClient.sync_day_role` is called with the right args for each transition, including the `discord_id=None` skip path, the flag-off short-circuit path (delegated to C1's tests), the bulk fan-out path emitting the summary log, **the bulk fan-out failing mid-loop emitting `scheduling_failed_at_index: k`**, and **`DAY_ROLE_SYNC_ENABLED=true` explicitly set in test fixtures so the short-circuit doesn't mask the assertion** (round-2 charge #6).
**Depends on:** C1.

### Tier D — End-to-end verification + contract spec publication

#### D1. End-to-end smoke + ops runbook
**Repo:** mom-bot (cross-repo concern, but the runbook lives with the bot)
**Title:** `docs(epic-2-6): end-to-end smoke checklist + ops runbook for day-role sync`
**Acceptance:**
- Manual smoke script / checklist in `docs/operations/`: create a siege, assign a member to Day 1, verify Discord role appears within the SLO; reassign to Day 2, verify swap; remove assignment, verify clear; bulk-apply auto-assign, verify N members get correct roles and the `role_sync_bulk_summary` log line is emitted with matching counts.
- **Smoke SLO** (charge #8): `p95 < 5s end-to-end`, measured from the moment siege-web's `PUT /sieges/{id}/members/{member_id}` returns to the operator's client to the moment the Discord member has the role visible in the client. (This is end-to-end wall-clock, *not* the BotClient HTTP timeout.)
- **Partial-response smoke (round-2 charge #9):** include a step that exercises a `partial` response from mom-bot. Use a test seam in B1's role service (e.g. a `MOM_BOT_FORCE_PARTIAL_FOR_DISCORD_ID` env var honored only in dev) that simulates one of the two underlying Discord API calls returning 403 deterministically. Observe and document:
  - The member holds both Day-1 and Day-2 roles for the dwell window (until the next legitimate `set` or `clear` for that member).
  - Whether `clan_reminders.py`-style role mentions double-fire when a member holds both day roles (test by triggering a Day-1 and a Day-2 reminder while the member is in the partial state). Record the observed behavior in both D1's runbook and D2's contract spec ("Partial-response semantics" section).
  - Whether operator intervention is required (manually strip the duplicate role) or whether the system self-heals on next assignment.
- App Insights query saved to the runbook for filtering `role_sync` events by `status`, joining `role_sync_bulk_summary` events by `correlation_id`, and **grouping by `correlation_id` to surface `attempt: 2` rows distinctly** so operators can count retries (round-2 charge #7). The query also surfaces `ROLE_HIERARCHY_LOST_AT_RUNTIME` events from B1.
- Documented behavior for the accepted-degradation cases: `member_not_in_guild`, missing `discord_id`, unseeded `day_role_map` (cannot actually occur post-A2 unless seed fails and bot starts anyway — which it doesn't, per A2 — so document as "should never appear in prod logs; if it does, treat as a bug").
- **Feature-flag operations** (charge #9/#10): document the `DAY_ROLE_SYNC_ENABLED` flag — current value, how to flip it (siege-web Container App env update), expected smoke order (flag off → deploy mom-bot B2 → smoke mom-bot endpoint directly via curl → flip flag on → re-smoke end-to-end → flag flip is the only rollback step needed for sync misbehavior).
- **Operator remediation for Discord role rename** (round-2 charge #2): include the A2 remediation steps in the runbook (strip old role from current holders → update KV → restart bot).
- **Open question carried into the issue** (charge #12, OQ4): who runs the manual smoke — router/Claude or the user on the live guild? Captured in the issue body, not the plan.
**Depends on:** A1–C2 all merged and deployed; D2 published (so the runbook can link to the contract spec).

#### D2. Publish day-role-sync webhook contract spec (NEW — round-2 bot-agnostic generalization)
**Repo:** siege-web
**Title:** `docs(role-sync): publish day-role-sync webhook contract spec`
**Why:** Both repos must reference a single source of truth for the wire contract so that siege-web's implementation stays bot-agnostic and any future receiver (replacing or supplementing mom-bot) can conform without changes to siege-web's code.
**Acceptance:**
- New file `siege-web/docs/webhooks/day-role-sync.md` containing the bot-agnostic contract spec. Required sections:
  1. **Overview** — what the webhook is for, the receiver's role, the agnostic stance.
  2. **Payload schema** — `discord_id`, `siege_id`, `day_number`, `action`, `assigned_at`, `correlation_id`. Types, semantics, ISO-8601 UTC requirement for `assigned_at`.
  3. **Response schema** — `status` enum (`applied | partial | skipped | failed`), `added`, `removed`, optional `reason`, optional `last_assigned_at` (on stale-write replies). Full enumeration of `reason` values: `member_not_in_guild`, `role_not_seeded`, `already_has_role`, `already_lacks_role`, `stale_write`, `remove_of_other_day_failed_403` (representative for `partial` responses). **Partial-response semantics** — what callers can assume about state after a `partial` (one mutation landed, the other did not; receiver may hold both roles until next overwrite).
  4. **Authentication** — Bearer token in `Authorization` header. The token is a shared secret managed via siege-web's `BOT_API_KEY` env var (or equivalent receiver-side secret). The contract does not constrain how either side stores the secret.
  5. **Retry semantics** — single 5xx retry, 500ms backoff, internal to the client. 4xx is not retried. After retry exhaustion, the call returns `False` at the client layer. No retry queue.
  6. **Idempotency rules** — key is `(discord_id, assigned_at, action, day_number)`. Exact replay returns the original response. Receivers SHOULD persist this state to survive restarts.
  7. **Ordering rules** — `assigned_at` is the monotonic ordering token. Receivers reject any request whose `assigned_at` is older than the last-seen value for that `discord_id` (and key does not exactly match) with `status: skipped, reason: stale_write`.
  8. **`correlation_id` conventions** — uuid4 per webhook call; one shared `correlation_id` across all calls in a bulk fan-out; preserved across retries.
  9. **Configuration** — `DAY_ROLE_SYNC_URL` (full webhook URL), `DAY_ROLE_SYNC_ENABLED` (default `false`; flag is the cross-repo gate and rollback kill switch), bearer token secret name.
  10. **Implementer guidance (non-normative)** — `discord_id=None` skip at sender layer; partial-response dwell behavior; role-hierarchy preflight as a recommended pattern for Discord-bot receivers but not contractual; observability conventions.
  11. **Current implementer** — one paragraph at the end: "mom-bot (https://github.com/glitchwerks/mom-bot) is the first / current conforming receiver. See `glitchwerks/mom-bot#6` and the Epic 2.6 plan for that implementation's specifics. Switching receivers requires only updating `DAY_ROLE_SYNC_URL` and the bearer secret."
- Doc is referenced by mom-bot's Epic 2.6 plan (this file), mom-bot#6, siege-web#323, C1, C2, B2, and D1.
- No code; pure documentation.
**Depends on:** none (can be drafted in parallel with tier A/B work, but should land before C1/C2 begin implementing).

---

## 3. Concurrency model (charge #4 resolution)

**Decision: hybrid — proven ordering for two seams, monotonic `assigned_at` token for the third, persisted in `member_role_sync_state` (round-2 charge #3).**

After reading the three service handlers (`services/siege_members.py:42-108`, `services/attack_day.py:130-163`) on 2026-05-13:

- **`add_siege_member`** (POST): cannot race meaningfully. New `SiegeMember` rows are created with `attack_day=NULL` (no field for the API caller to set on create — see `SiegeMemberCreate` schema at `api/siege_members.py:12-13`, only `member_id`). The check-then-insert at `services/siege_members.py:58-69` is racy on a duplicate-create attempt, but the DB unique constraint on `(siege_id, member_id)` (verified by re-reading `models/siege_member.py`) rejects the second insert with `IntegrityError`. **No webhook fires on this seam in v1** (no `attack_day` to sync), so concurrency is moot.
- **`update_siege_member`** (PUT) vs **`apply_attack_day`** (bulk): this is the hazard. Two interleaving cases:
  1. Two concurrent PUTs on the same `(siege_id, member_id)` — last-write-wins on the DB (`session.commit()` ordering), but the two `BackgroundTasks` queue webhooks independently and may fire out of commit-order if the second commit is faster than the first's BackgroundTask reaches `BotClient`. Plausible under load.
  2. One PUT on member X while `apply_attack_day` is running and assigns member X — the bulk handler reads `siege.siege_members` collection into memory via `selectinload` (`services/attack_day.py:131-133`), mutates rows in-memory, then commits all at once. A concurrent PUT can commit in between the bulk handler's read and its commit; the bulk handler will overwrite. The two webhooks may fire in either order.
- **Monotonic ordering via `assigned_at`, persisted in `member_role_sync_state`** (round-2 charges #1 + #3): both seams emit the row's post-commit `updated_at` timestamp as `assigned_at` in the webhook payload. B2's endpoint persists per-`discord_id` last-seen `assigned_at` (and the full last-response payload) in a SQLite table and:
  - Returns `status: skipped, reason: stale_write` for any incoming `assigned_at` older than the stored value (and key does not exactly match).
  - Returns the **stored original response** for any incoming request that exactly matches the stored idempotency key `(discord_id, assigned_at, action, day_number)` — this is replay-safe and preserves observability (a retried call shows `success` in the bulk summary, matching server-side reality).
  - Survives restart; no eviction. Storage cost is O(distinct members ever synced) which is bounded by guild size.

**Why not the "prove ordering holds" path:** the bulk-vs-PUT interleave is plausible (admin edits one member while another admin clicks bulk-apply), and the cost of `assigned_at` + persisted state is small enough that proving the absence of races doesn't pay back the operational confidence cost.

---

## 4. siege-web#323 body rewrite (round-2 charge #8)

The current `glitchwerks/siege-web#323` body was filed before this plan locked the architecture and is mom-bot-coupled. **Tier C cannot begin until #323's body has been edited to match the bot-agnostic shape below.** This is a tier-C precondition.

> **Historical note:** the router pasted the following as the new issue body via `gh issue edit 323 --body-file ...` after this plan locked. The future-tense phrasing below is preserved as historical instruction; the action has been executed.

### Replacement issue body (paste verbatim)

```markdown
## Overview

siege-web emits a generic **day-role-sync outbound webhook** on Day-Assignment changes. The receiver toggles Discord roles based on the published contract. **siege-web's implementation is bot-agnostic** — no assumptions are made about which bot is on the other end of the webhook. Switching receivers requires only a configuration change (`DAY_ROLE_SYNC_URL` + bearer secret).

The full wire contract is published at `siege-web/docs/webhooks/day-role-sync.md` (delivered by sub-issue **D2** of the Epic 2.6 plan). All payload, response, auth, retry, idempotency, and ordering details are defined there. This issue covers siege-web's **client-side implementation** of that contract.

## Contract spec (summary)

Full spec: `siege-web/docs/webhooks/day-role-sync.md`.

- **Payload:** `{discord_id, siege_id, day_number, action, assigned_at, correlation_id}`. `action` is `"set"` (with a `day_number`) or `"clear"` (no `day_number`). `assigned_at` is ISO-8601 UTC.
- **Response:** `200` with `{status, added, removed, reason?, last_assigned_at?}`. `status` enum: `applied | partial | skipped | failed`.
- **Auth:** Bearer token in `Authorization` header (shared secret).
- **Retry:** client retries once on 5xx after 500ms backoff. 4xx is not retried. After retry exhaustion, returns `False`. `correlation_id` is preserved across the retry.
- **Idempotency:** key is `(discord_id, assigned_at, action, day_number)`. Exact replay returns the original response.
- **Ordering:** `assigned_at` is the monotonic token. Older-than-last-seen writes are rejected as `stale_write`.

## siege-web implementation scope

- Extend `BotClient` (`backend/app/services/bot_client.py`) with `sync_day_role()` following the existing 10s-timeout / Bearer-auth pattern. Internal single-retry on 5xx (500ms backoff), `correlation_id` preserved.
- Wire the three mutation seams:
  - `update_siege_member` (`api/siege_members.py:44-51`) — fire on `attack_day` transition after commit + refresh.
  - `apply_attack_day` (`api/attack_day.py:22-30`) — re-read committed rows, diff against pre-commit snapshot, fan out one call per affected member, share one `correlation_id`, emit `role_sync_bulk_summary` log in a `try/finally`.
  - `add_siege_member` (`api/siege_members.py:35-41`) — **documented no-op** (newly-created `SiegeMember` has `attack_day=NULL`; webhook fire here is always a no-op for day-role purposes). No call is wired in v1; revisit if create-shape changes.
- Add `DAY_ROLE_SYNC_ENABLED` env var (default `false`). When disabled, `sync_day_role` short-circuits at DEBUG level and returns `True`. The flag is the cross-repo gate and the rollback kill switch.
- Add `DAY_ROLE_SYNC_URL` env var (the full webhook URL on the receiver).
- Bearer token via existing bot API key secret.
- Skip emit when `member.discord_id is None` (log at INFO; no HTTP call).
- Structured logging on every call with `correlation_id`; bulk summary log on fan-out with `fired: k of N`, `scheduling_failed_at_index: int | None`.

## Implementer agnosticism

siege-web's code makes **no assumptions** about which bot implements the receiving end of the webhook. The contract spec is the only coupling. Switching to a different receiver requires only updating `DAY_ROLE_SYNC_URL` and (if applicable) the bearer secret. No code change.

This is a deliberate design choice: the same Day-Assignment events may in future also drive non-Discord receivers (e.g. push notifications, web hooks into a different ops system).

## Current implementer

mom-bot (https://github.com/glitchwerks/mom-bot) is the current / first conforming receiver. See `glitchwerks/mom-bot#6` (Epic 2.6) for context on that implementation. mom-bot's deployment readiness gates the **operator-flips-flag-to-true step**, but does **not** gate this issue's implementation — siege-web can ship the code with the flag off in parallel with mom-bot's work.

## Sequence

This issue's implementation can proceed independently with `DAY_ROLE_SYNC_ENABLED=false`. The flag is the cross-repo gate.

The flag may only be flipped to `true` after:
1. A conforming receiver is deployed in the target environment (dev or prod).
2. The receiver has been smoked directly (curl against `DAY_ROLE_SYNC_URL`) and observed to behave per the contract spec.
3. End-to-end smoke per mom-bot's `docs/operations/` runbook has passed in the same environment.

## Out of scope

- Bulk re-sync on receiver startup (a receiver coming online does not catch up on missed assignments — siege-web does not re-emit historical events).
- Role cleanup at siege end (roles persist until overwritten by a subsequent assignment change — per locked architecture).
- Synchronous wait for the receiver's response (all calls are fire-and-forget via `BackgroundTasks`).

🤖 _Generated by Claude Code on behalf of @cbeaulieu-gt_

_Revised 2026-05-13 — body rewritten to be bot-agnostic and to reference the canonical contract spec doc (D2). Original body's mom-bot-specific framing was reconciled with Epic 2.6 plan locked architecture._
```

---

## 5. Risks & mitigations

| ID  | Risk | Likelihood | Impact | Mitigation |
|-----|------|------------|--------|------------|
| R1  | Day 1 / Day 2 roles ranked above mom-bot's bot role → silent 403 on every call | Medium | High | Tier A1 pre-deploy checklist; B1's startup hierarchy preflight (CRITICAL log + ConfigError + exit) catches it at startup; B1's runtime hierarchy-loss detection emits `ROLE_HIERARCHY_LOST_AT_RUNTIME` if an admin changes the hierarchy after startup (round-2 charge #5) |
| R2  | `day_role_map` unseeded at first call (KV name secret missing or role-not-found in guild) | Low | High | A2's seed runs at startup and exits the bot on failure (matches reminders-seed). Cannot reach B2's runtime path with unseeded map; the `role_not_seeded` reason in B1 is defensive and should never fire in prod |
| R3  | `apply_attack_day` bulk endpoint fires N HTTP calls — rate-limit risk if N is large (typical guild siege ~30 members) | Low | Medium | Discord per-bot global limit is 50 req/s (https://discord.com/developers/docs/topics/rate-limits, fetched 2026-05-13) — 30 sequential calls fit comfortably. Use `BackgroundTasks` (sequential within request, non-blocking on response). **Revisit trigger:** if a single guild exceeds 40 members assigned in one bulk op, batch endpoint becomes worth the complexity |
| R4  | Member deletion cascades the `siege_member` row but doesn't fire a sync → stale role persists | Medium | Low (matches accepted "persist until overwritten" semantics) | Documented in D1 runbook as accepted behavior |
| R5  | Siege deletion cascades all `siege_member` rows for that siege → same stale-role problem at scale | Low | Low | Same as R4 — accepted by architecture |
| R6  | `member.discord_id` collision (two members claim same Discord ID) | Very Low | Medium | `member.discord_id` has `unique=True` constraint (`member.py:22`) — DB rejects |
| R7  | mom-bot sidecar down when siege-web fires the webhook → `sync_day_role` returns `False` after internal retry, role never gets set | Low-Medium | Low-Medium | `BotClient` retries once on 5xx (charge #1/#2). After exhaustion, returns `False`; no retry queue. Bulk summary log surfaces the count via `failed: F`. Revisit if ops sees frequent drops in App Insights |
| R8  | Concurrent edits to same `(siege_id, member_id)` cause out-of-order webhook delivery → role state diverges from DB | Medium | Medium | `assigned_at` monotonic token + persisted `member_role_sync_state` table (round-2 charges #1, #3). Survives restart, no eviction window. Exact replays return the stored original response, preserving observability |
| R9  | Operator forgets to flip `DAY_ROLE_SYNC_ENABLED` to `true` after deploy → no sync ever happens, silently | Medium | Low | D1's runbook makes the flag-flip an explicit step; App Insights query for `role_sync` event volume = 0 is a quick post-deploy verification |
| R10 | Admin renames a Discord role mapped in `day_role_map` (snowflake changes for the same name) → silent re-mapping would orphan the old role on current holders | Low | Medium | A2's seed detects snowflake change vs existing row, logs CRITICAL with both snowflakes and current holders of the old role, raises ConfigError. Operator-driven remediation (strip old → update KV → restart) — no automated mass role-strip (round-2 charge #2) |
| R11 | Contract drift between siege-web and mom-bot (one repo evolves the wire shape, the other lags) | Low | High | Single source of truth: `siege-web/docs/webhooks/day-role-sync.md` (D2). Both repos reference it. Any contract change is a PR against D2 first, then implementing PRs in each repo (round-2 bot-agnostic generalization) |

---

## 6. Open questions for the user

(Round 1 had 4 OQs; OQ1/OQ3 resolved by user decisions, OQ4 moved into D1's issue body per charge #12, OQ2 resolved by the planner.)

**No remaining open questions.** This plan is GREENLIT per CLAUDE.md's 2-pass inquisitor mandate.

---

## 7. Definition of done (Epic 2.6 as a whole)

- All 9 sub-issues closed via merged PRs.
- mom-bot#6 closed via `Closes glitchwerks/mom-bot#6` in the final PR (likely D1).
- siege-web#323 closed in the siege-web v1.2 milestone.
- `siege-web/docs/webhooks/day-role-sync.md` published (D2) and referenced from both repos.
- D1 smoke executed successfully against the dev guild with `DAY_ROLE_SYNC_ENABLED=true`, App Insights confirming `role_sync` events at `p95 < 5s` and `role_sync_bulk_summary` events with matching counts.
- mom-bot v1.0 milestone has zero open issues.

---

## 8. Proposed sub-issue titles (flat list, in filing order — 9 issues)

1. `chore(epic-2-6): pre-deploy ops checklist — Day 1 / Day 2 roles exist + bot rank above them` (mom-bot) — **A1**
2. `feat(epic-2-6): add day_role_map(day_number, discord_role_id) table + startup seed by role name` (mom-bot) — **A2**
3. `chore(epic-2-6): verify mom-bot install bitfield includes MANAGE_ROLES` (mom-bot) — **A3**
4. `feat(epic-2-6): build mom_bot/roles/ — role toggle service + startup hierarchy preflight + runtime hierarchy loss detection` (mom-bot) — **B1**
5. `feat(epic-2-6): add POST /api/internal/role-sync sidecar endpoint` (mom-bot) — **B2**
6. `feat(role-sync): add BotClient.sync_day_role() outbound webhook, behind DAY_ROLE_SYNC_ENABLED` (siege-web — body replaces current #323) — **C1**
7. `feat(role-sync): fire BotClient.sync_day_role from siege_member add/update + attack_day apply` (siege-web) — **C2**
8. `docs(epic-2-6): end-to-end smoke checklist + ops runbook for day-role sync` (mom-bot) — **D1**
9. `docs(role-sync): publish day-role-sync webhook contract spec` (siege-web) — **D2** (NEW in round-2 revision)

---

## 9. Pending #6 issue body updates (for router action after plan lands)

> **Historical note:** these were instructions for the router agent at plan-locking time. The updates have since been executed against #6; the future-tense phrasing below is preserved for provenance.

The router should update `glitchwerks/mom-bot#6` after this plan lands to reflect the bot-agnostic shape and reference the canonical contract spec doc (D2). Specifically:

- Add a top-of-body note: "Wire contract is defined in `siege-web/docs/webhooks/day-role-sync.md` (sub-issue D2 of the Epic 2.6 plan). This issue covers mom-bot's role as a **conforming receiver** of that contract."
- Reference the 9-item sub-issue list from § 8 of this plan.
- Reference the renamed siege-web env vars (`DAY_ROLE_SYNC_ENABLED`, `DAY_ROLE_SYNC_URL`).

Do not edit #6 as part of this plan-file revision — it is a separate router action.
