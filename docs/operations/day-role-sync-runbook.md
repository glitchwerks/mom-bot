# Day-role sync — operator runbook

**Audience:** Operator maintaining the day-role sync feature in a deployed environment. Assumes
you have completed the pre-flight checklist (`docs/operations/discord-roles-preflight.md`) and
the initial smoke (`docs/operations/day-role-sync-smoke.md`). This runbook covers steady-state
monitoring, incident playbooks, and operator remediation steps.

**Source of truth for the wire contract:** [`glitchwerks/rsl-mom-apps` —
`contracts/day-role-sync.md`](https://github.com/glitchwerks/rsl-mom-apps/blob/main/contracts/day-role-sync.md)
(pinned: [`@5576807`](https://github.com/glitchwerks/rsl-mom-apps/blob/5576807101c04a9b595192cee2b9a02aed1c9c12/contracts/day-role-sync.md)).
Any payload/response shape detail not covered here defers to that document.

---

## 1. Feature flag operations

### Reading the current state

The `DAY_ROLE_SYNC_ENABLED` flag lives on the **siege-web** Container App, not on mom-bot.

Azure Portal path:
```
Azure Portal
  → Container Apps
  → <siege-web app name>
  → Settings → Environment variables
  → DAY_ROLE_SYNC_ENABLED
```

`az` CLI equivalent (replace `<rg>` and `<app>` with the actual resource group and app name):
```bash
az containerapp show \
  --resource-group <rg> \
  --name <app> \
  --query "properties.template.containers[0].env[?name=='DAY_ROLE_SYNC_ENABLED'].value" \
  --output tsv
```

### Flipping the flag

**Via Azure Portal:** Edit the `DAY_ROLE_SYNC_ENABLED` environment variable → save → allow the
revision to roll out (~30 s). The new revision is the only one that will see the updated value.

**Via `az` CLI:**
```bash
az containerapp update \
  --resource-group <rg> \
  --name <app> \
  --set-env-vars DAY_ROLE_SYNC_ENABLED=true
```

Replace `true` with `false` to disable. The Container App creates a new revision automatically.

### Recommended flip sequence (first-time enablement)

Follow this order when enabling the feature for the first time in a given environment. Do not
skip steps — the sequence exists to catch configuration errors before live traffic touches Discord.

1. **Flag off.** Confirm `DAY_ROLE_SYNC_ENABLED=false` on siege-web (this is the default and
   should already be the case).

2. **Confirm mom-bot B2 is deployed.** Check that the mom-bot Container App is running the
   revision that includes the `POST /api/internal/role-sync` endpoint. Verify with a health-check
   request (no auth needed — the endpoint returns `401` on missing auth, which proves the route
   exists):
   ```bash
   curl -s -o /dev/null -w "%{http_code}" \
     https://<mom-bot-hostname>/api/internal/role-sync \
     -X POST \
     -H "Content-Type: application/json" \
     -d '{}'
   ```
   Expected response: `401`. A `404` means the endpoint is not deployed yet.

3. **Curl-smoke the sidecar endpoint directly.** Send a valid payload with the correct bearer
   token. This tests the mom-bot receiver in isolation, without siege-web involved.

   Construct a test payload (replace placeholder values):
   ```bash
   curl -v \
     https://<mom-bot-hostname>/api/internal/role-sync \
     -X POST \
     -H "Authorization: Bearer <discord_bot_api_key>" \
     -H "Content-Type: application/json" \
     -d '{
       "discord_id": "<test_member_snowflake>",
       "siege_id": 1,
       "day_number": 1,
       "action": "assign",
       "assigned_at": "2026-05-14T00:00:00.000Z",
       "correlation_id": "00000000-0000-0000-0000-000000000001"
     }'
   ```

   Expected response body: `{"status":"applied","added":[<role_snowflake>],"removed":[],"reason":null,"last_assigned_at":null}`

   Alternatively, if the member already holds Day 1:
   `{"status":"skipped","added":[],"removed":[],"reason":"already_has_role","last_assigned_at":null}`

   A `status=applied` or `status=skipped/already_has_role` confirms the endpoint, auth, discord
   connection, and day_role_map seed are all functioning.

   **Note:** the bearer token (`discord_bot_api_key`) is stored in mom-bot's Azure Key Vault as
   `{env}-discord-bot-api-key`. Retrieve it with:
   ```bash
   az keyvault secret show \
     --vault-name kv-mombot-eastus2 \
     --name {dev|prod}-discord-bot-api-key \
     --query value \
     --output tsv
   ```

4. **Flip flag on.** Set `DAY_ROLE_SYNC_ENABLED=true` on siege-web.

5. **Re-smoke end-to-end.** Run the smoke checklist (`docs/operations/day-role-sync-smoke.md`)
   Scenarios 1 through 4.

6. **Flag is the only rollback step.** If anything goes wrong after enabling, flip
   `DAY_ROLE_SYNC_ENABLED` back to `false` on siege-web. This stops all outbound webhook calls
   immediately — no redeployment of either app is required.

---

## 2. Log queries

Mom-bot emits structured log lines to stdout. In the deployed environment, stdout is routed to
the Container App's log stream, which can be tailed via the Azure Portal or CLI. Application
Insights integration is planned but not yet wired (the `{env}-app-insights-conn-string` Key Vault
secret is present but set to `PLACEHOLDER` as of the Epic 2.6 deployment — see
`docs/secrets-inventory.md`). The queries below are written in plain log-grep form for the
Container App log stream.

### Accessing mom-bot logs

**Azure Portal:** Container Apps → mom-bot app → Monitoring → Log stream.

**CLI (streams live):**
```bash
az containerapp logs show \
  --resource-group <rg> \
  --name <app> \
  --follow \
  --tail 100
```

### Accessing siege-web logs

Siege-web emits the `role_sync_bulk_summary` log. Access siege-web logs via the same Portal or
CLI path, substituting the siege-web app name.

---

### Query A — All role_sync events for a member

Filter by `discord_id` to trace all sync activity for one member:

```
role_sync … discord_id=<SNOWFLAKE> …
```

Fields in each line:
- `correlation_id` — ties to the siege-web action that triggered this call
- `siege_id` — which siege the assignment belongs to
- `day_number` — which day was being assigned or unassigned
- `action` — `assign` or `unassign`
- `assigned_at` — ISO-8601 UTC timestamp from siege-web; the monotonic ordering token
- `status` — `applied`, `partial`, `skipped`, or `failed`
- `added` — list of role snowflakes added (may be `[]`)
- `removed` — list of role snowflakes removed (may be `[]`)
- `attempt` — `1` for a fresh delivery; `2` for an exact-replay return from the idempotency cache

---

### Query B — All role_sync events by status

Filter by status to isolate problem categories:

```
# Applied (happy path)
role_sync … status=applied …

# Partial (one mutation succeeded, one failed — needs attention)
role_sync … status=partial …

# Skipped (deliberate no-op — usually normal)
role_sync … status=skipped …

# Failed (all mutations failed — needs investigation)
role_sync … status=failed …
```

---

### Query C — Retry detection (attempt=2)

Every exact replay returns `attempt=2`. The `role_sync_idempotent_replay` event is emitted for
these cases. Group by `correlation_id` to identify which sieges caused retries:

```
role_sync_idempotent_replay …
```

Fields: same as a `role_sync` event plus confirmation that the stored response was returned.

A single `attempt=2` event is expected and normal — siege-web retries exactly once on 5xx. If
you see multiple `attempt=2` events for the same `correlation_id`, that indicates repeated
delivery beyond the single-retry contract, which is a siege-web issue.

---

### Query D — Bulk fan-out reconstruction

The `role_sync_bulk_summary` event is emitted by siege-web, not by mom-bot. Look for it in
siege-web's logs:

```
role_sync_bulk_summary correlation_id=<ID> …
```

Fields (from the siege-web implementation, PR #411):
- `correlation_id` — matches all individual mom-bot `role_sync` events from this batch
- `siege_id` — the siege the bulk op was applied to
- `fired` — number of webhook calls successfully enqueued (e.g. `5 of 8`)
- `skipped_no_discord_id` — members without a Discord ID who were filtered at the sender layer
- `failed_at_layer` — count of HTTP failures recorded by `BotClient` (after its internal retry)
- `scheduling_failed_at_index` — `null` on the happy path; non-null if the fan-out loop raised

Cross-reference: take the `correlation_id` from this event and query mom-bot logs for all
`role_sync` events with that `correlation_id` to see the per-member outcomes.

---

### Query E — Hierarchy loss events

`ROLE_HIERARCHY_LOST_AT_RUNTIME` is emitted at ERROR level when a 403 from Discord reveals that
a role the bot previously managed is now above the bot's top role:

```
ROLE_HIERARCHY_LOST_AT_RUNTIME …
```

Fields:
- `correlation_id` — the call that surfaced the loss
- `discord_id` — the affected member
- `role_id` — the Discord snowflake of the problematic role
- `role_position` — the role's current position in the hierarchy
- `bot_top_role_position` — the bot's top role's current position

This event is emitted at most once per `role_id` per process lifetime (deduplication prevents
log spam). If you see it, the role hierarchy needs correction — see the "Hierarchy violation
incident playbook" section.

---

### Query F — Startup preflight result

After every bot restart, look for the preflight summary to confirm the bot started cleanly:

```
role_preflight_complete guild_id=… total=… violations=… missing=…
```

Expected: `violations=0 missing=0`. Any other value means the bot exited at startup (it raises
`ConfigError` on violations). If the bot is running, the preflight passed.

---

### Query G — Seed events (at startup)

```
DAY_ROLE_SEEDED guild_id=… day=… role_id=…
DAY_ROLE_SNOWFLAKE_CHANGED guild_id=… day=… old_role_id=… new_role_id=…
DAY_ROLE_NOT_FOUND guild_id=… day=…
```

`DAY_ROLE_SEEDED` on every restart is normal (the seed is idempotent — if the snowflake and
display name both match, the log is not emitted; if either changed, one of the other events
fires). `DAY_ROLE_NOT_FOUND` means the guild has no role with the expected name for that day —
see "Operator remediation: Discord role rename" below.

---

## 3. Accepted degradation cases

The following conditions produce a `200 status=skipped` response. They are not errors. No
operator action is required when you see them in steady state.

### `member_not_in_guild`

**What you see:**
```
role_sync … status=skipped reason=member_not_in_guild discord_id=<ID> …
```

**Why:** The Discord account associated with this siege-web member has left the guild (or was
never a guild member). The bot looked up the member by snowflake and got `None`. The webhook
round-trip completes cleanly — siege-web gets `200`, no retry fires.

**Action required:** None. If the member rejoins the guild, the next assignment change will apply
the role normally.

---

### Missing `discord_id` (no webhook emitted)

**What you see:** A siege-web `INFO` log at the sender layer:
```
role_sync_skip discord_id=None siege_id=… member_id=…
```

No mom-bot `role_sync` event appears at all — the call was filtered before leaving siege-web.

**Why:** The `SiegeMember` record does not have a linked Discord account. The producer-side
filter (PR #411) suppresses the webhook before any HTTP call is made.

**Action required:** None. Operators should NEVER see a webhook arrive at mom-bot for a member
with no `discord_id` — if a `role_sync` event appears with `discord_id=` empty or missing, that
is a siege-web producer bug. Escalate to the siege-web maintainer.

---

### Unseeded `day_role_map` (`role_not_seeded`)

**What you see:**
```
role_sync … status=skipped reason=role_not_seeded day_number=<N> …
```

**Why:** The bot has no row in `day_role_map` for the requested `day_number`. Per the A2 design
(PR #68), this should never occur in production: the seed runs at startup and the bot exits with
`ConfigError` if the seed fails. If you see `role_not_seeded` in production logs, the bot did
not start cleanly.

**Action required:** Check the startup logs. Look for `DAY_ROLE_NOT_FOUND` during the seed phase,
which means the guild had no role with the expected name (`Attack Day 1` / `Attack Day 2`). If
the role does not exist, create it (or fix the rename — see "Operator remediation" below). Then
restart the bot.

---

## 4. Operator remediation: Discord role rename

**When this applies:** A guild administrator deleted and recreated an `Attack Day N` role, or
renamed it and Discord issued a new snowflake. The bot detects this at startup and logs
`DAY_ROLE_SNOWFLAKE_CHANGED` (informational — it updates the map automatically).

The scenario that requires operator intervention is when a role's **snowflake changes between
deployments** and existing members still hold the old role. The startup seed logs the holders,
but does not strip the old role from them.

**Sequence:**

1. **Identify current holders of the old role.** At startup, the bot logs the members holding the
   old role snowflake when a `DAY_ROLE_SNOWFLAKE_CHANGED` event fires. Review those log entries
   to build the list. Alternatively, in Discord: Server Settings → Roles → old role → Members.

2. **Strip the old role from all current holders.** In Discord:
   - Server Settings → Roles → select the old role → Members tab.
   - Remove the role from each member individually, or use a Discord admin tool that supports
     bulk role removal.
   - There is no automated mass-strip in mom-bot v1.0 — this is intentional (the operator
     verifies the rename is intentional before stripping).

3. **Confirm the new role exists in the guild.** The seed looks for roles named
   `Attack Day 1` and `Attack Day 2` by name in the guild. If the rename changed this display
   name, you must either revert the rename in Discord (so the name matches again) or create a new
   role with the expected name.

4. **Restart the bot.** The startup seed re-resolves the name to the current snowflake and
   UPSERTs the `day_role_map` row. Check the startup logs for `DAY_ROLE_SEEDED` (new row) or a
   clean no-op (snowflake already matches).

5. **Verify.** Run:
   ```sql
   SELECT day_number, discord_role_id, role_display_name FROM day_role_map ORDER BY day_number;
   ```
   Cross-check `discord_role_id` against Discord (Server Settings → Roles → right-click a role
   → Copy Role ID, with Developer Mode on). They must match.

6. **Confirm preflight passes.** In the startup logs: `role_preflight_complete … violations=0`.

---

## 5. Hierarchy violation incident playbook

Two distinct events signal a hierarchy problem. Each has a different trigger and remediation.

### ROLE_HIERARCHY_MISCONFIGURED (startup, fail-fast)

**Trigger:** At startup, `run_preflight` (`src/mom_bot/roles/service.py`) checks every
`day_role_map` row. If any mapped role is ranked at-or-above the bot's top role in the guild
hierarchy, this event fires and the bot raises `ConfigError` — the bot exits. The Container App
will restart and exit repeatedly until the hierarchy is fixed.

**What you see in logs:**
```
ROLE_HIERARCHY_MISCONFIGURED guild_id=… day_number=… role_id=… role_name=… role_position=… bot_top_role_position=…
role_preflight_complete guild_id=… total=2 violations=1 missing=0
```
The Container App then exits (no further log lines from that instance).

**Remediation:**
1. Open Discord: Server Settings → Roles.
2. Find mom-bot's bot role. Find every `Attack Day N` role listed in the `ROLE_HIERARCHY_MISCONFIGURED` log.
3. Drag mom-bot's bot role to a position above all `Attack Day N` roles. Save.
4. Restart the mom-bot Container App. Confirm the startup logs show
   `role_preflight_complete … violations=0`.

---

### ROLE_HIERARCHY_LOST_AT_RUNTIME (runtime, 403 detected)

**Trigger:** The bot passed startup preflight (hierarchy was correct at startup), but a Discord
administrator moved mom-bot's role or an `Attack Day N` role during runtime, causing a subsequent
`add_roles` or `remove_roles` call to return `403 Forbidden`. The bot detects this and emits the
event at ERROR level. Emission is deduplicated to once per `role_id` per process lifetime.

**What you see in logs:**
```
ROLE_HIERARCHY_LOST_AT_RUNTIME correlation_id=… discord_id=… role_id=… role_position=… bot_top_role_position=…
```
The individual `role_sync` event for that call shows `status=failed` (if the primary mutation
was the one that 403'd) or `status=partial` (if only the secondary remove-of-other-day 403'd).

**Remediation:** Same as startup case — fix the role ordering in Discord. Unlike the startup
case, the bot does not exit — it continues processing other members while the hierarchy is wrong.
Once the hierarchy is corrected in Discord, no restart is needed: subsequent calls to the
corrected roles will succeed. However, the specific member whose `role_sync` call failed will
not be retried automatically. To correct their role state, trigger a fresh assignment in siege-web
(update their `attack_day` to any value — this generates a new webhook with a fresh `assigned_at`
which the idempotency layer treats as a fresh write).

**Monitoring:** After fixing the hierarchy, confirm no further `ROLE_HIERARCHY_LOST_AT_RUNTIME`
events appear. Also confirm `role_sync … status=failed` rates return to zero.

---

## 6. Stale-write skips

**What you see:**
```
role_sync … status=skipped reason=stale_write assigned_at=<older_ts> …
```
The response body also contains `last_assigned_at` with the stored (newer) timestamp.

**Why this is expected:** The stale-write check compares the incoming `assigned_at` against the
stored `last_assigned_at` for that `discord_id`. If a newer write already landed for the same
member (from a concurrent or sequential webhook), the older write is rejected as stale.

Common causes:
- Concurrent edits: two administrators updated the same member's assignment in quick succession.
  The later `assigned_at` (sourced from PostgreSQL `clock_timestamp()` per siege-web PR #411)
  arrived first. The earlier one arrives afterward and is correctly rejected.
- Siege-web's single retry: the initial delivery succeeded but the network dropped the response.
  Siege-web retried with the same payload and same `correlation_id`. The retry is an exact replay
  (same idempotency key), so it returns the stored response — **not** a stale-write. A stale-write
  skip with a different `correlation_id` indicates a genuinely older write, not a retry.

**When to care:** A sustained rate of stale-write skips on a single `discord_id` (multiple per
minute) suggests concurrent writes at a rate that exceeds what the monotonic ordering can absorb.
This is a siege-web producer concern — check whether multiple administrators are editing the same
member's assignment simultaneously. No mom-bot action is required.

---

## 7. Rollback

Use this section when the day-role sync feature needs to be disabled or its receiver reverted
after a bad deployment. Two paths are available; choose based on which side is the cause.

---

### 7.1 Primary rollback — flip the producer flag (preferred)

**Use this first.** Flipping `DAY_ROLE_SYNC_ENABLED=false` on siege-web stops all outbound
webhook calls immediately. Mom-bot's `ca-mom-bot` Container App continues running without
interruption — it simply receives no new calls. This is zero-downtime on the mom-bot side and
fully reversible by re-flipping the flag.

**Dev:**
```
az containerapp update -g siege-web-dev -n siege-web-api-dev --set-env-vars DAY_ROLE_SYNC_ENABLED=false
```

**Prod:**
```
az containerapp update -g <TODO: confirm prod resource name> -n <TODO: confirm prod resource name> --set-env-vars DAY_ROLE_SYNC_ENABLED=false
```

Allow approximately 30 seconds for siege-web to roll out the new revision. Then confirm the flag
took effect:

```
az containerapp show -g <rg> -n <app> --query "properties.template.containers[0].env[?name=='DAY_ROLE_SYNC_ENABLED']" -o json
```

Expected: the returned object includes `"value": "false"`.

**Confirm silence on the receiver side.** After the revision rolls out, tail mom-bot's logs for a
5-minute window and confirm no new `role_sync` lines appear:

```
az containerapp logs show -g mom-bot -n ca-mom-bot --tail 50
```

No new `role_sync` lines in that window confirms the producer is gated and the receiver has gone
quiet.

---

### 7.2 Receiver rollback — revert `ca-mom-bot` to a prior revision

**Use this when the receiver itself is the cause** — for example, a bad image was deployed to
`ca-mom-bot` and is producing runtime errors — and the producer (siege-web) is still emitting
webhooks cleanly.

`ca-mom-bot` runs in **single revision mode**: only one revision receives traffic at a time.
Reverting traffic to a prior revision requires deactivating the current (bad) revision and
activating the last-known-good one. No load-balancer weights or traffic-split rules are involved.

#### Step 1 — List revisions and identify last-known-good

```
az containerapp revision list -g mom-bot -n ca-mom-bot --query "[].{name:name, active:properties.active, health:properties.healthState, created:properties.createdTime, image:properties.template.containers[0].image}" -o table
```

Review the output. The last-known-good revision is the most recent one with `health=Healthy`
that predates the bad deployment. Note its `name` value.

#### Step 2 — Deactivate the bad revision

```
az containerapp revision deactivate -g mom-bot -n ca-mom-bot --revision <bad-rev>
```

#### Step 3 — Activate the prior healthy revision

```
az containerapp revision activate -g mom-bot -n ca-mom-bot --revision <good-rev>
```

In single revision mode, activating the good revision routes all traffic back to it. The bad
revision remains listed (deactivated) until it is cleaned up.

#### When to use receiver rollback vs. flag-off

| Situation | Preferred path |
|---|---|
| Producer is healthy; receiver has a runtime error or bad image | Receiver rollback (§ 7.2) |
| Either side is uncertain; need immediate stop | Flag-off (§ 7.1) — faster, no revision management |
| Both are suspect | Flag-off first to stop the firehose; then diagnose receiver independently |

---

### 7.3 State considerations

#### In-flight events

Any webhook already in HTTP transit when the producer flag flips will still arrive at the
receiver and be processed. This is acceptable — the endpoint is idempotent and these events will
complete normally.

#### `member_role_sync_state` table

No cleanup is needed. The table is idempotent: stale rows are harmless because the table is only
consulted on subsequent `action=unassign` events (fixed in #205). A stale row from a partial cycle
will either be overwritten by the next `assign` or no-op cleanly on the next `unassign`.

#### Discord role residue

If rollback occurs mid-cycle, some members may hold a stale `Attack Day N` role in Discord —
for example, a Day 1 role was assigned but the expected unassign (from a subsequent reassign) never
fired because the producer was stopped. The system does not self-heal on a timed basis. Recovery
is manual:

1. Identify affected members via Discord Server Settings → Roles → `Attack Day N` → Members.
2. Strip the stale role from each member individually via Discord Server Settings → Members.

There is no "sync all members" reconciliation command — that facility is deferred. When the feature
is re-enabled, the next legitimate `assign` or `unassign` event for each affected member will
overwrite any residue automatically.

---

### 7.4 Verification after rollback

Confirm the rollback is effective before closing the incident:

1. **No new `role_sync` log lines in receiver logs** over a 5-minute window post-rollback (see
   § 7.1 for the `az containerapp logs show` command).

2. **A test toggle in siege-web produces no Discord role change.** If you optionally flip the
   feature flag on in a controlled test, no Discord role change should result — because the flag is
   the gate and will be returned to `false` immediately after the test.

3. **Member state in `member_role_sync_state` is unchanged from the pre-rollback snapshot.**
   Query the table to confirm no new rows or updates appeared after the rollback completed:
   ```sql
   SELECT discord_id, last_assigned_at, updated_at FROM member_role_sync_state ORDER BY updated_at DESC LIMIT 20;
   ```
   If rows are still being updated after flag-off, in-flight events are still draining — this is
   expected for up to a minute after the flip. Stable rows confirm the drain is complete.

---

## Cross-references

- Pre-flight checklist: `docs/operations/discord-roles-preflight.md`
- Smoke checklist: `docs/operations/day-role-sync-smoke.md`
- Wire contract: [`glitchwerks/rsl-mom-apps` — `contracts/day-role-sync.md`](https://github.com/glitchwerks/rsl-mom-apps/blob/main/contracts/day-role-sync.md) (pinned: [`@5576807`](https://github.com/glitchwerks/rsl-mom-apps/blob/5576807101c04a9b595192cee2b9a02aed1c9c12/contracts/day-role-sync.md))
- Parent epic: `glitchwerks/mom-bot#6`
- A2 seed implementation: PR #68 (`src/mom_bot/roles/seed.py`)
- B1 role service + preflight: `src/mom_bot/roles/service.py`
- B2 sidecar endpoint: `src/mom_bot/sidecar/app.py`
- Idempotency table: `src/mom_bot/sidecar/models.py` (`MemberRoleSyncState`)
- siege-web C1 (BotClient): PR #409 (`glitchwerks/rsl-siege-manager`)
- siege-web C2 (wiring + bulk summary log): PR #411 (`glitchwerks/rsl-siege-manager`)
