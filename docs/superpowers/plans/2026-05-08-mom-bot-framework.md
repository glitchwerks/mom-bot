# Mom_bot Framework Plan

## Context

Two Discord bots run against a single Discord guild today, both authenticating with the **same Discord bot token**:

- **`siege-web/bot/`** — FastAPI HTTP sidecar (port 8001) that `siege-web`'s backend calls to send DMs, post images to channels, and look up guild members. No slash commands today; only an `on_ready` event handler. Self-contained, no imports from siege-web's backend.
- **`I:\games\raid\siege\clan\`** — async scheduler that posts hardcoded reminders (`Hydra` Tuesdays 07:00 UTC, `Chimera` Wednesdays 12:00 UTC) to a configured channel. Uses `discord.py>=2.3.2`, persists "sent today" state to a JSON file at `%APPDATA%\siege_reminders\reminders_sent.json`. The `siege/` sibling subfolder in that repo contains old siege-planning code — irrelevant. Today the bot runs as an Azure resource (Container App / VM / Function — exact RG and resource name confirmed pre-Epic-0).

There is **no token contention today** because both workloads are **one-way / post-only** — neither maintains a continuously-connected Discord gateway. (Note: this changes for mom_bot, which DOES maintain a gateway connection because of slash commands. Validating the existing Discord app's gateway intents is a Pre-Epic-0 task.)

This plan establishes the framework for consolidating both into a new product called **mom_bot**, hosted in a new repo at `I:\other\mom-bot`, with a new third capability: **interactive slash commands** scoped to (a) member self-service reads, (b) member self-service writes for post preferences only, and (c) reminder management.

## Goal

Land **mom_bot v1.0** as a new product that:

1. Preserves the existing siege-web sidecar contract (the 6 HTTP endpoints + Bearer auth, port 8001, JSON shapes unchanged)
2. Hosts the reminder system (lifted from `clan/`, with JSON-file state → SQLite)
3. Adds interactive slash commands across three narrow use-case clusters: member self-service reads, post-preference self-service writes, and reminder management

Mom_bot ships in a separate repo and on its own version track (`mom-bot v0.1` → `v1.0`) — but its **runtime is coupled to siege-web by design** (Epic 2.5 cross-cut, sidecar HTTP contract, shared Discord token, shared guild, shared `Clan Deputies` admin role). The separate-repo / separate-versioning is for code-organization clarity rather than real separability; an actual decoupling effort would require additional work the v1.0 plan does not deliver.

## Discord scope (locked)

**In scope** — three use-case clusters:

| Cluster | Auth | Description |
| --- | --- | --- |
| Member self-service reads | Open to all guild members | "Where am I assigned, when's the next siege, who is X" — read-only, no role check |
| Post-preference self-service writes | Per-user `me`-semantics (siege-web-side) | A member can set/view *their own* post preferences only |
| Reminder & event management | Admin-only (Discord role-gated bot-side) | Add / list / remove / pause / resume scheduled message reminders, plus auto-managed monthly **Tank Week** Discord scheduled events |

**Explicitly out of scope** for v1.0 — each with revisit conditions:

- **Operator quick-ops via Discord (assign/unassign/view board)** — revisit if 3+ unique operators request via Discord over 30 days post-launch
- **Admin broadcast / notify-all-from-Discord** — revisit at v1.x scoping if siege-web's existing notification UI proves insufficient for spontaneous broadcasts
- **Multi-step write flows (auto-fill, validate-and-apply)** — defer permanently unless web UI breaks for a workflow that genuinely requires Discord-side execution
- **Self-service writes to anything besides post preferences** — revisit per-feature when admins flag a specific tracking burden (analogous to how preferences was added)
- **Voice-channel or stage-channel scheduled events for tank-week** — revisit only if tank-week becomes a synchronous voice-call event

## Architecture

### Three concerns inside one async process

Mom_bot runs all three concerns concurrently in a single Python process via `asyncio.TaskGroup` (matching siege-bot's existing pattern). Single process is the right default — the three concerns share the discord.py `Client` object, and process-level isolation buys nothing because Discord rate-limits and token contention apply globally regardless.

- **Interactive half** — `discord.py` slash command + component handlers. Receives Discord interactions, calls outbound to siege-web's REST API with a Bearer service token and the `X-Acting-Discord-Id` header (for self-service writes), returns formatted ephemeral embeds.
- **Service half** — FastAPI HTTP server on port 8001. Bearer-token-gated. Exposes the same 6 endpoints siege-bot exposes today, ported essentially verbatim. Siege-web's backend continues to call these.
- **Scheduler** — async `on_clock` polling loop (port from `clan_reminders.py`, don't replace with apscheduler). Hourly wake, daily callback at UTC midnight, fires reminders via the shared discord.py client.

**Failure isolation — single-process, restart-as-recovery model.** Per-handler try/except wraps protect against Python exceptions in command handlers (the easiest failure mode). The harder failure modes — discord.py gateway disconnect, memory bloat in one concern, blocking calls stalling the shared event loop — are **not** handled in-process. Recovery for these relies on Container App's automatic restart policy + App Insights latency alerting. If the gateway dies, the scheduler dies with it, Container App restarts the container, mom_bot reconnects (typical 30-60s recovery). This is an explicit accept of the single-process trade-off: simpler operations + shared state, with Container App's restart-as-recovery handling the inevitable shared-failure modes.

### Wire-level contracts

| Boundary | Direction | Mechanism |
| --- | --- | --- |
| Discord ↔ mom_bot interactive | bidirectional | discord.py gateway + slash/component callbacks |
| mom_bot → siege-web | outbound | HTTPS + `Authorization: Bearer <SIEGE_WEB_SERVICE_TOKEN>`. `httpx.AsyncClient`, 10s timeout, exp-backoff (max 2 retries on 5xx), circuit-breaker on sustained failure. **Self-service writes also send `X-Acting-Discord-Id: <invoker_id>` header.** |
| siege-web → mom_bot | inbound | HTTP to mom_bot's port 8001 (the preserved sidecar surface). `Authorization: Bearer <MOM_BOT_API_KEY>`. Cutover seam: siege-web's `.env` flips `DISCORD_BOT_API_URL` from siege-bot's address to mom_bot's |
| mom_bot scheduler → Discord | outbound | Shared discord.py client posts reminder messages with role mentions; creates scheduled events for tank week |

**Defer-first discipline:** every interactive command must `await interaction.response.defer()` immediately. Enforced via a `@deferred` decorator (added to Epic 0 as a foundational pattern) — the **only** sanctioned way to register an interactive slash command. Without the decorator the command isn't registered; this fails at startup, not at first invocation, eliminating the "command #14 forgot to defer" failure mode.

### Self-service write auth — implicit `me` semantics (the load-bearing decision)

**Honest trust model.** Mom_bot's bearer service token gives it full impersonation power against siege-web. The `X-Acting-Discord-Id` header is for *attribution and "me" resolution*, not authorization. Siege-web trusts the bot to send the correct Discord ID; if mom_bot has a bug that supplies the wrong invoker's ID, the write goes to the wrong member's preferences regardless of how the URL is shaped. Bot compromise = full impersonation across all members.

What the implicit-`me` URL shape *does* buy:

- **Smaller endpoint authorization surface.** No ownership check logic in siege-web's endpoint handler — the path doesn't accept a member ID, so there's no `if requested_id != current_user.member_id: 403` to forget.
- **Smaller risk of cross-Member writes via siege-web bugs.** If a route handler accepted `member_id` in the path, a logic bug could swap it. The `/me/...` shape eliminates that class of bug from siege-web's side.
- **Cleaner contract.** `/api/members/me/preferences` is self-documenting; consumers don't have to know the invoker's member_id.

The actual security boundary is **mom_bot's correctness in populating the header.** Mitigations:

- **Single helper** `set_acting_discord_id(invoker)` — every interactive write goes through it; not inlined per command
- **Audit log** every `/me/preferences` write to mom_bot's local SQLite `audit_log` (Discord ID, resolved member ID per siege-web's response, timestamp) for after-the-fact verification
- **Audit-log read path** — `/admin audit-log <member>` slash command (admin-role-gated) returns recent rows for that member. Without a read path the audit log is feel-good control; the slash command is the recovery interface when a member reports "my preferences got overwritten"
- **Audit-log retention** — rolling 90 days, pruned daily by a scheduler task. Older rows are not load-bearing for the after-the-fact-verification use case the audit log was added to support
- **Integration tests** verify two distinct Discord users see distinct preferences (smoke check on the entire chain)

Endpoint shape:
- `GET /api/members/me/preferences` and `PUT /api/members/me/preferences` (new endpoints in siege-web)
- Mom_bot calls them with `Authorization: Bearer <SIEGE_WEB_SERVICE_TOKEN>` + `X-Acting-Discord-Id: <invoker_discord_id>`
- Siege-web's `get_current_user` extends to read the header on service-token requests; resolves to `Member` by `discord_id`
- Reject with 401 if header absent on service-token requests; reject with 404 if Discord ID maps to no Member
- **Two-Members-with-same-discord_id case:** structurally impossible — `members.discord_id` already has `unique=True` constraint (verified at `backend/app/models/member.py`)

## Confirmed design decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| **DB shape** | Own SQLite at a Container Apps mounted volume; **single replica (`scale=1`) enforced** | Cheapest infra; reminder workload is tiny (dozens of rows); SQLAlchemy abstracts so swap-to-Postgres later is mechanical. Single-replica constraint dodges Azure Files SMB+SQLite locking issues entirely (no concurrent writers across replicas). Mom_bot's workload doesn't need horizontal scaling. WAL mode validated against the volume backend during Epic 0 sanity check |
| **Self-service auth** | Implicit `me` semantics with `X-Acting-Discord-Id` header | Smaller endpoint authorization surface; honest trust model (bot is fully trusted; header is for attribution + resolution, not authorization) |
| **Discord token** | Inherit existing token | Both current bots share one token; cutover is process-replacement, not app-rotation. **Caveat:** existing Discord app's gateway intents must be audited Pre-Epic-0 (slash commands require gateway connection that current post-only bots don't use) |
| **Versioning track** | Own product (`mom-bot v0.x`); runtime coupled to siege-web by design | Clean release-cadence separation, but operationally tied to siege-web. Don't oversell the separability |
| **Discord scope** | Self-service reads + post-preference self-service writes + reminder management. No operator-ops, no admin-broadcast | Narrow, honest sizing. Web UI keeps owning operator workflows |
| **Reminder JSON state migration** | NOT migrated; cutover timing rule (Epic 4) prevents duplicates | Migrating `reminders_sent.json` across machines / RGs / processes costs more than the bounded one-time risk of a duplicate ping after cutover |

## Slash command surface (concrete list)

```
Member self-service (open to all guild members, read-only):
  /siege me                    — show your assignment in current siege
  /siege next                  — when's the next siege
  /siege status                — current siege state at a glance
  /siege member <name>         — look up a member by name or @

Post-preference self-service (per-user, writes own data only):
  /siege preferences view      — see your current post preferences
  /siege preferences set ...   — set your preferred post conditions
                                 (likely a button/menu interactive flow given
                                  the multi-condition shape)

Reminder & event management (reads open; writes admin-role-gated):
  /reminder list                    — list all scheduled message reminders
  /reminder add ...                 — admin-only; multi-arg or button-flow
  /reminder remove <name>           — admin-only
  /reminder pause <name>            — admin-only
  /reminder resume <name>           — admin-only
  /reminder tank-week list          — show upcoming auto-created tank-week events
  /reminder tank-week create [date] — admin-only manual override (defaults to next month's first-Wed week)
  /reminder tank-week cancel <id>   — admin-only; remove an auto-created or manual event
```

**Grouping:** Discord-native single-level groups (`/siege` and `/reminder`). Two places use Discord's one-level-of-nesting:
- `/siege preferences <action>` (view, set)
- `/reminder tank-week <action>` (list, create, cancel)

**Total surface:** ~13 commands. Small enough that flat-within-group works; no need for `/info` vs `/manage` split.

### Tank Week feature — autonomous + manual override

Tank Week is a recurring monthly Discord **scheduled event** (Discord's native "external" event type — calendar entry, RSVP-able, no voice-channel commitment). The week is the calendar week containing the **first Wednesday of the month**.

**Autonomous behavior** — mom_bot's scheduler checks daily whether tank-week events are missing for any month within the next 30 days:
- If no event exists for an upcoming tank-week within the lookahead window AND today is within the auto-creation window for that month (e.g. 7-14 days ahead of the event start), create the event via `guild.create_scheduled_event(entity_type=external, ...)` with title `"Tank Week (Hydra)"`, computed start/end times, and a description template
- **First-deployment / mid-month catch-up:** the 30-day lookahead naturally catches the current month's tank week if it hasn't fired yet at first deploy. Same logic; no separate backfill code path
- The 30-day lookahead also covers cutover scenarios

**Idempotency via SQLite-tracked event IDs.** A `tank_week_events(year, month, discord_event_id, source: 'auto'|'manual', created_at)` table is the bot's source of truth for what it created. Before creating an event for `(year, month)`, query the table — if a row exists, no-op. Discord is the rendering target. This avoids the string-pattern fragility of "list Discord events and match by name pattern" — the bot doesn't depend on event names matching across releases or human-created events accidentally being interpreted as bot-created.

**Manual override** — admin runs `/reminder tank-week create [date]`:
- If a SQLite row already exists for that month → reject with "Tank week already scheduled for [month]; cancel it first if you want to replace"
- If no row exists → create the event in Discord, write the SQLite row with `source: 'manual'`

**Discord bot permission required:** `Manage Events` on the guild scope. Audited and confirmed Pre-Epic-0 (see Pre-Epic-0 task).

## Phasing — 5 epics + 1 cross-cut + 1 pre-epic gate

### Pre-Epic-0 — Discord application audit + reminder-bot deployment typing (gates Epic 0)

Before any mom_bot code work, complete these gating tasks:

**A. Discord application audit:**

- Confirm gateway intents needed for slash commands are enabled (Application Commands; members intent if member-lookup commands need it)
- Confirm `Manage Events` permission is in the bot's invite scope; if not, identify owner who can update via Discord developer portal
- Confirm guild permissions: Send Messages, Embed Links, Use Application Commands, Manage Events
- Confirm the existing token still works for the new gateway-connected workload (different from today's post-only usage)

**Fallback branch — if token inheritance is not feasible:**

If the audit reveals that the existing token cannot be inherited for mom_bot's gateway-connected workload (intents irrevocable on the existing app, app owned by an inaccessible Discord account, OAuth scopes unfixable, etc.), **trigger framework re-planning before Epic 0 begins**. Fallback path: register a new Discord application; Epic 4's cutover runbook gains additional steps (siege-web `.env` token rotation, guild re-invite of new bot, role audit on new bot identity). Re-planning at this stage is cheap because no Epic 0 code has shipped; discovering this constraint mid-Epic-3 would be much more expensive. **The framework's "inherit existing token" decision pillar is contingent on this audit passing — if it doesn't, the plan adjusts here, not later.**

**B. Reminder-bot deployment typing:**

- Identify the **specific Azure resource type** (Container App / VM / Function App / App Service) hosting reminder-bot today, plus its RG and resource name
- Update the Cutover runbook (Epic 4, step 2) with the per-type stop command:
  - Container App: `az containerapp stop -g <RG> -n <name>`
  - VM: `az vm deallocate -g <RG> -n <name>`
  - Function App: `az functionapp stop -g <RG> -n <name>`
  - App Service: `az webapp stop -g <RG> -n <name>`
- Knowing the type matters because each has different stop semantics and cost/restart implications

Document findings in `pre-epic-0-checklist.md` (or as a tracked GitHub issue).

**Owner:** repository admin / @cbeaulieu-gt (only person with confirmed Discord developer portal + cross-RG Azure access).

### Epic 0 — Skeleton (mom_bot side only)
New repo at `I:\other\mom-bot`. Discord client connecting via inherited token (Key Vault reference). App Insights wired. CI green. SQLite + alembic baseline. Single `/ping` slash command for health-check. **`@deferred` decorator pattern committed as the only sanctioned interactive-command registration mechanism.** **No functionality yet.**

### Epic 1 — Reminder lift-and-shift
Port `clan/clan_reminders.py`, `clan/reminder_sent_store.py`, `clan/clan.py`, `discord_api/discordClient.py` into `mom_bot/reminders/`. Replace `%APPDATA%\siege_reminders\reminders_sent.json` with SQLite-backed `reminder_sent` table via alembic. Seed `reminders` table with `Hydra` and `Chimera` so behavior is unchanged at cutover. Reminders **not yet user-configurable** (Epic 3-adjacent).

### Epic 2 — Sidecar lift-and-shift
Port `siege-web/bot/app/` (5 modules + 6 tests) into `mom_bot/service/`. Bearer auth, same JSON shapes, same port (8001). Existing tests carry over essentially as-is. Verify with siege-web pointed at mom_bot's dev URL exercising all 6 endpoints.

### Epic 2.5 — Siege-web `/me/preferences` endpoints + header support (cross-cuts into siege-web v1.2)

Small siege-web PR:
- Extend `get_current_user` to read `X-Acting-Discord-Id` header on service-token requests; resolve to `Member` by `discord_id`; attach to `AuthenticatedUser` (new field, e.g. `acting_member_id: int | None`)
- New endpoints: `GET /api/members/me/preferences` and `PUT /api/members/me/preferences`. Both resolve "me" from the header, reject if header absent on service-token requests
- Tests: header present + valid Discord ID → resolves; header absent → 401; header present + unknown Discord ID → 404; cookie-authed (non-service) requests with header → header ignored (path uses cookie's `member_id`)

**Tracking contract for cross-repo dependency:**
- The siege-web v1.2 issue is filed at the **start of mom_bot Epic 2** (not at completion), so it's already in flight as Epic 2 lands
- Cross-link convention: mom_bot's Epic 3 issue body references the siege-web issue number; the siege-web issue body references mom_bot's Epic 3 issue
- **What can proceed against pending Epic 2.5:** mom_bot Epic 3's *read* commands depend only on existing siege-web GET endpoints — they land independently. Only the *preferences-write* commands gate on Epic 2.5 merging. Sequence Epic 3's reads first; advance to writes only after Epic 2.5 ships

### Epic 2.6 — Day-role sync (cross-cuts into siege-web)

Port the day-role assignment feature from `clan/`: when a member is assigned to an attack day in siege-web, mom-bot toggles the corresponding `Attack Day N` Discord role on that member.

**Architecture (locked):**

- **Sync model — push from siege-web (real-time).** siege-web calls a new mom-bot sidecar endpoint whenever a Day-Assignment row changes. No polling loop in mom-bot; no scheduler involvement
- **Lifecycle — persist until overwritten.** Day-roles stay on members between sieges; next siege's assignment changes overwrite them. No siege-state-change signal needed; no scheduler cleanup. Observable consequence: between sieges, role mentions reflect the prior siege's roster — surface this in the v1.0 release notes
- **Role provisioning — pre-existing, admin-managed.** Discord roles named `Attack Day 1`, `Attack Day 2`, etc. are created by guild admins once. mom-bot only calls `member.add_roles()` / `member.remove_roles()` — never `guild.create_role()` / `role.delete()`. Smallest blast radius

**Mom-bot side:**

- Port `clan/` role-assignment code into `mom_bot/roles/`
- New SQLite table `day_role_map(siege_kind, day_number, discord_role_id)` seeded once per guild via Pre-Epic-0 admin work
- New sidecar endpoint `POST /api/internal/role-sync` (Bearer-auth, payload: `{discord_id, siege_id, day_number, action: 'assign'|'unassign'}`). Idempotent — siege-web may retry
- App Insights logging for every role toggle (member, role, action, success/failure, layer-4-403 vs other failure modes)

**Siege-web side (cross-repo, similar shape to Epic 2.5):**

- Wire siege-web's Day-Assignment create/update/delete handlers to call mom-bot's `/api/internal/role-sync`
- Fire-and-forget with retry (the Discord-side state will reconcile naturally on the NEXT assignment change for any member, so isolated drops are self-healing across normal usage)
- Tracked as a separate sibling issue against siege-web, cross-linked from mom-bot's Epic 2.6 issue

**Permission additions (gates Pre-Epic-0):**

- Layer 2: add `Manage Roles` (`1 << 28`) to install bitfield. New conservative integer: `17592454531072`. See `docs/discord-permissions-reference.md` for the recomputed permissive variant
- Layer 4: mom-bot's role must be ranked **above** every `Attack Day N` role in the guild's role list. Added to Pre-Epic-0 audit checklist (issue #1)

**Out of scope, revisit conditions:**

- Role cleanup at siege end — revisit if stale-role pings become an actual annoyance
- Discord-side `/siege sync-roles` admin command for forced reconciliation — revisit if drift incidents happen
- Auto-creation of missing `Attack Day N` role objects — revisit only if admin-managed pre-creation proves to be a recurring stumbling block
- Bulk re-sync on bot restart — relies on push being reliable; revisit if push drops show up in telemetry

**Position in epic graph:** lands between Epic 2.5 and Epic 3 because it depends on the sidecar surface (Epic 2) but doesn't gate on the slash-command interactive surface (Epic 3). The siege-web outbound-call sibling issue lands in parallel — same cadence as Epic 2.5.

### Epic 3 — Interactive slash commands

Implement the locked command surface above. **Order — read-then-reminder-then-tank-week-then-writes — sequenced so SQLite-writer surface is exercised on simpler ground first:**

1. **Member self-service reads** (`/siege me`, `/siege next`, `/siege status`, `/siege member`) — pure read commands calling existing siege-web GET endpoints. Battle-tests the bot's outbound httpx + `@deferred` pattern + ephemeral-embed shape on simple surface first
2. **Reminder management** (`/reminder list|add|remove|pause|resume`) — exercises the SQLite-writer surface with a simple schema (no autonomous loop, no externally-deleted detection). Establishes the `@require_admin_role` decorator pattern. By landing this before tank-week, schema bugs / locking / migration issues surface on a simpler write path that's easier to debug
3. **Tank Week** (`/reminder tank-week list|create|cancel` + autonomous scheduler logic) — most novel surface (new Discord scheduled-events API integration, autonomous logic, SQLite event tracking, externally-deleted detection). Lands AFTER reminder mgmt has proven out the SQLite-writer + admin-role-check patterns; tank-week's autonomous loop runs against a battle-tested DB layer
4. **Post-preference self-service** (`/siege preferences view|set` + `/admin audit-log <member>`) — requires Epic 2.5 to have merged. Calls `/me/preferences` endpoints. Last because it has the highest external dependency (cross-repo) and the most novel auth model (`X-Acting-Discord-Id` header passing); landing it last lets earlier Epic 3 commands prove out the bot's integration shape first. Audit-log slash command lands in this step too

### Epic 4 — Cutover

**Cutover-time-of-day rule:** schedule cutover for **Thursday or Friday post-13:00 UTC**. Both reminders for the week (Hydra Tuesday 07:00 UTC, Chimera Wednesday 12:00 UTC) have already fired by Thursday. Avoids the ambiguous case of cutting over on a Wednesday at 12:00+ where Chimera may or may not have fired. Wider safety margin; lower risk than the originally-drafted "post-12:00 UTC" rule which fails on Wednesday days.

**Cutover runbook (per-step credential / persona):**

| Step | Credential / persona |
| --- | --- |
| 1. Stop siege-bot Container App in `siege-web-prod` RG | siege-web's deploy credential (Azure CLI `az containerapp stop` or via siege-web's existing deploy workflow extended with a stop mode) |
| 2. Stop reminder-bot's Azure resource (per-type command parameterized by Pre-Epic-0 typed answer: `az containerapp stop` / `az vm deallocate` / `az functionapp stop` / `az webapp stop`) | Owner of reminder-bot's Azure RG (same person/credential as today's deployer of that resource) |
| 3. Verify mom_bot is sole holder of the Discord token (no contention) | Visual check via Discord developer portal's online-sessions view |
| 4. Flip siege-web's `DISCORD_BOT_API_URL` env var from siege-bot's URL to mom_bot's prod URL | siege-web's deploy credential; updates Container App env var via Azure CLI or portal |
| 5. Verify siege-web → mom_bot calls work (post a test image, send a test DM) | Anyone with siege-web admin access |
| 6. Decommission old siege-bot Container App | siege-web's deploy credential |
| 7. Decommission reminder-bot's Azure resource | Owner of reminder-bot's RG |

**Pre-cutover checklist** captures the specific RG and resource name for reminder-bot's deployment, the specific URL for mom_bot's prod sidecar, and the rollback procedure (re-flip `DISCORD_BOT_API_URL` back; restart siege-bot's old Container App).

## Critical files

### Source A: siege-web bot — extract from
- `I:\games\raid\siege-web\bot\app\main.py` — entry point pattern (asyncio.TaskGroup with discord.py + uvicorn)
- `I:\games\raid\siege-web\bot\app\http_api.py` — 6-endpoint FastAPI surface (notify, post-message, post-image, members, members/{id}, version+health)
- `I:\games\raid\siege-web\bot\app\discord_client.py` — `SiegeBot(Client)` with `on_ready` guild caching + `send_dm` / `post_message` / `post_image` / `get_members` methods
- `I:\games\raid\siege-web\bot\app\config.py` — pydantic-settings pattern
- `I:\games\raid\siege-web\bot\app\telemetry.py` — App Insights wiring
- `I:\games\raid\siege-web\bot\tests\` — 6 test files, port over

### Source B: reminder system — extract from
- `I:\games\raid\siege\clan\clan_reminders.py` — 277 lines, `on_clock` async loop + `daily_callback_template`
- `I:\games\raid\siege\clan\reminder_sent_store.py` — 73 lines, JSON-file persistence with `RLock` (replace with SQLite)
- `I:\games\raid\siege\clan\clan.py` — 54 lines, scheduler entrypoints
- `I:\games\raid\siege\discord_api\discordClient.py` — `DiscordAPI` wrapper (~80 lines)
- `I:\games\raid\siege\guild_config.ini` — static config (channels, reminder times) — port to SQLite seed
- `I:\games\raid\siege\tests\test_reminder*.py` — port over
- `I:\games\raid\siege\clan\` — locate day-role-assignment module (likely a `*role*.py` or `*assignment*.py` file; pre-Epic-1 grep to identify exact paths). Port logic into `mom_bot/roles/`

### Target: siege-web — Epic 2.5 modifies
- `I:\games\raid\siege-web\backend\app\dependencies\auth.py` — `get_current_user` extended to read `X-Acting-Discord-Id` header on service-token paths
- `I:\games\raid\siege-web\backend\app\api\members.py` — add `GET /me/preferences` and `PUT /me/preferences` endpoints
- `I:\games\raid\siege-web\backend\tests\test_auth.py` and member tests — coverage for header semantics

### Target: siege-web — read-only references mom_bot calls
- `GET /api/sieges` — `/siege next`, `/siege status`
- `GET /api/sieges/{id}/board` — `/siege me` (find your position in the board)
- `GET /api/members` and `GET /api/members/{id}` — `/siege member <name>`

### Preconditions verified
- `members.discord_id` already has `unique=True` constraint at `backend/app/models/member.py`. The implicit-`me` Discord-ID lookup is structurally unambiguous; no two-Members-with-same-discord_id resolution rule needed.

### Reusable patterns
- **Worktree convention** — `I:\other\mom-bot\.worktrees\<branch>` matches the siege-web pattern
- **Per-component versioning** — once siege-web's #311 plan lands, mom_bot adopts the same discipline (own `VERSION` file, runtime `/version` endpoint, `CHANGELOG.md` updated at tag-time)
- **Single role-check decorator** for reminder management (`@require_admin_role`) — centralized; not inlined per command
- **`@deferred` decorator** for interactive slash commands — the only registration path; failure-at-startup beats failure-at-invocation

## Risks and mitigations

| Risk | Severity | Mitigation |
| --- | --- | --- |
| **Self-service `me` resolves to wrong Member.** Bug in mom_bot supplies wrong invoker's Discord ID. | High | Single helper `set_acting_discord_id(invoker)` — every interactive write goes through it. Audit log every `/me/preferences` write to mom_bot's local SQLite with both Discord ID and resolved member ID for after-the-fact verification. Integration tests verify two distinct Discord users see distinct preferences |
| **Reminder admin role-check bug.** A bug in `@require_admin_role` lets any guild member modify reminders. | Medium | Single decorator, never inlined. Unit tests covering every role × command. Integration test: role-less fake user invokes `/reminder add` → expect unauthorized embed. Log every privileged invocation to App Insights |
| **siege-web API unavailable during interactive commands.** Discord users see hung interactions or timeout errors. | Medium | `defer()` immediately on every interactive command (enforced by `@deferred`); 10s timeout on outbound httpx calls; circuit-breaker that flips after sustained failure; fallback embed: *"Siege-web is currently unavailable. Try again in a few minutes, or use the web UI at <url>."* |
| **Day-role sync drift after dropped webhook calls.** siege-web push fails (network blip, mom-bot down); Discord roles get out of sync with siege-web's day-board. | Low | Self-healing in normal usage: the NEXT assignment change for any drifted member fires a fresh push that reconciles. App Insights alerting on 5xx responses from mom-bot's `/api/internal/role-sync` surfaces sustained drift. Manual `/siege sync-roles` recovery command deferred (out of scope) but design doesn't preclude adding it later |
| **Cutover duplicate-reminder risk.** Today's `reminders_sent.json` state isn't migrated; if mom_bot deploys near a reminder-fire time, the reminder might fire from both old and new bot. | Low | Cutover-time-of-day rule: **Thursday or Friday post-13:00 UTC** ensures both Hydra (Tue) and Chimera (Wed) reminders have already fired earlier in the week. Avoids the Wednesday-12:00 edge case where Chimera fires concurrent with cutover |
| **Member-not-registered self-service.** Discord user issues `/siege preferences set` but they're not in the siege Member roster. | Low | Endpoint returns 404; bot embed: *"You're not registered as a clan member yet. Ask leadership to add you."* No silent failure |
| **Tank-week duplicate creation on bot restart.** Without idempotency tracking, scheduler check on restart could create a second event for the same month. | Low | SQLite-tracked event IDs in `tank_week_events(year, month, discord_event_id, source)` — bot is source of truth for what it created. Before creating, query the table; no-op on existing row. Avoids the string-pattern fragility of matching against Discord's event names |
| **Tank-week event externally deleted.** Admin deletes the event via Discord portal; bot's SQLite row remains. | Low | Next scheduler tick detects mismatch (SQLite row exists, Discord event doesn't). Default behavior: alert (App Insights warning) and require manual `/reminder tank-week create` for re-creation. See Open Question #5 for confirmation |
| **First-Wednesday math edge cases.** Months that start on a Wednesday → that Wednesday IS the first. Months across year boundaries. Timezone confusion. | Low | Pure utility function (`first_wednesday_of(year, month)` returning a date in UTC), unit-tested against ~24 months of fixtures including edge cases (Apr 2026 starts Wednesday → first Wed is Apr 1) |
| **Existing Discord app's gateway intents may not cover slash commands.** Pre-Epic-0 audit might surface required portal-side changes. | Medium | Pre-Epic-0 task explicitly audits this. If changes needed, owner (@cbeaulieu-gt) makes them via Discord developer portal before Epic 0 starts. Plan does not assume "inherit token" is automatic — the audit step is gating |

## Verification per epic

- **After Pre-Epic-0:** `pre-epic-0-checklist.md` exists, all bullets verified or have known follow-ups assigned. The "are gateway intents in place" question has a definitive yes/no with a confirmed remediation path if no.
- **After Epic 0:** mom_bot connects to Discord, `/ping` returns ephemeral pong (verified through `@deferred` decorator), App Insights receives traces, CI green on `mom-bot` repo. SQLite migration applies cleanly (empty schema baseline).
- **After Epic 1:** custom test reminder set 2 minutes in the future fires on time. SQLite migration applies cleanly. `Hydra` and `Chimera` rows seeded.
- **After Epic 2:** siege-web's `DISCORD_BOT_API_URL` pointed at mom_bot's **dev** URL — exercise all 6 endpoints, all return same JSON shapes as today. Mom_bot version endpoint reports `mom-bot v0.x.y`.
- **After Epic 2.5:** new `/api/members/me/preferences` endpoints work end-to-end. With header `X-Acting-Discord-Id` set to a known Discord ID, GET returns that member's preferences, PUT updates them. Without header (service token only), 401. With unknown Discord ID, 404. Cookie-authed (non-service) requests ignore the header.
- **After Epic 3:** all 13 commands work in dev guild. Member-self-service reads return correct data. `/siege preferences set` updates the invoker's record (verify via siege-web web UI). Role-less test user gets unauthorized embed for every `/reminder` write command. Tank-week event auto-creation triggers in dev (force window override for testing); manual `/reminder tank-week create` produces a Discord scheduled event visible in the guild's events panel; idempotent on retry. SQLite `tank_week_events` table tracks correctly.
- **After Epic 4:** in prod, both flows work end-to-end with mom_bot as sole bot. siege-bot's old container app is stopped (or deleted). Reminder-bot's Azure resource is stopped (or deleted). Discord guild sees one bot identity. Verify via Discord developer portal that no other bot session contends with mom_bot's gateway connection.

## Open questions for next round

These don't shape the framework but need answers before specific epics begin:

1. **`/siege preferences set` interaction shape** — multi-condition selection works best as a button/select-menu flow rather than typed args. How many post conditions can a member set? Is it ranked? Confirm before Epic 3.
2. **Tank-week duration boundaries** — "the week with the first Wednesday" → which calendar week shape? Sun-Sat? Mon-Sun? Just the Wednesday itself? And the time bounds — whole-day events, or specific hours (00:00 UTC start, 23:59 UTC end)?
3. **Tank-week auto-creation window** — how many days ahead of the event should the bot create it (7? 14? 30?). Affects when guild members see the upcoming event.
4. **Tank-week event description template** — static template with date interpolation, or admin-configurable via SQLite?
5. **Tank-week externally-deleted handling** — when SQLite row exists but Discord event was deleted by admin via portal, should next tick alert or auto-recreate? Default: alert + manual recreate. Confirm.
6. **Reminder-bot's exact RG / resource name** — Pre-Epic-0 deliverable; needed for the cutover runbook step 2 + 7
7. **Repo visibility** (public / private / org-internal) — affects CI secret strategy in Epic 0
8. **Azure region** — same as siege-web (East US 2) or different?
9. **Siege-web service token rotation cadence** — how often, and what's the rotation mechanism (Key Vault reference + Container App restart)?
10. **App Insights instance shape** — separate (recommended for blast-radius isolation) or shared with siege-web (one-pane observability)?
11. **Reminder schema timezone-awareness** — UTC-only (today's behavior) or guild-local? Decide before Epic 1 schema lands
12. **Slash command guild-scoping** — register globally (3-hour propagation, cleaner) or per-guild (instant, tied to specific guild IDs)? For single-guild bot, per-guild is the natural choice
13. **Maintenance window for Epic 4 cutover** — naturally aligns with Thursday-or-Friday post-13:00 UTC rule; pick the specific date
14. **Mom_bot's GitHub Project / milestone setup** — should a milestone exist before Epic 0 starts? Per the issue-first discipline, yes
15. **Admin role identity for reminder management** — which Discord role authorizes `/reminder add|remove|pause|resume` and `/reminder tank-week create|cancel`? `Clan Deputies` (existing siege-web admin role)? Different role?

## Sequencing relative to siege-web's v1.2

Mom_bot is its own track. There are two sync points with siege-web v1.2: **Epic 2.5** (preferences `/me/` endpoints) and **Epic 2.6** (outbound webhook from Day-Assignment changes to mom-bot's role-sync endpoint). Both land as v1.2 tickets in siege-web. v1.2's other work (per-component versioning discipline #311, lock siege mutations #242, validation message hygiene #321) is independent of mom_bot.

```
siege-web track:    v1.1 (shipped) ──→ v1.2 (in-flight, Epic 2.5 + 2.6 outbound) ──→ v1.3 ...
                                                ↓ (sync points)
mom-bot track:      Pre-0 ──→ Epic 0 ──→ Epic 1 ──→ Epic 2 ──→ Epic 2.6 ──→ Epic 3 ──→ Epic 4 ──→ v1.0
```
