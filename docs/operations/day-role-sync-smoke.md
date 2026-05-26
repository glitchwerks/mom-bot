# Day-role sync — end-to-end smoke checklist

**Audience:** Guild administrator / operator running the first live smoke after Epic 2.6 B2 is
deployed. You will need access to the siege-web UI or API, the Discord guild, and the mom-bot
Container App logs.

**Who runs this:** The operator manually, on the live Discord guild. Claude does not have guild
access. Each step has a "you click / you run" action and an "expected result" to observe.

**Prerequisites:** Complete `docs/operations/discord-roles-preflight.md` before starting. That
checklist confirms the roles exist, the hierarchy is correct, and mom-bot has `MANAGE_ROLES`.

**SLO:** p95 < 5 s end-to-end, measured from the moment siege-web's `PUT` or `POST` response
returns to your client until the Discord role is visible in the member's profile. Time five or
more assignments individually with a stopwatch. Record the worst. Anything under 5 s at the 95th
percentile passes.

---

## Before you start

1. Confirm `DAY_ROLE_SYNC_ENABLED` is currently `false` on the siege-web Container App
   (Azure Portal → Container Apps → siege-web → Environment Variables). The flag is your rollback
   switch — leave it off until step 1 of Scenario 1 below.
2. Pick a test member. Use a Discord account you control, not a real clan member, so you can
   observe role changes immediately from that account's Discord client.
3. Note the test member's Discord snowflake ID. You will use it to filter logs in all diagnostic
   steps.
4. Open the Discord guild in a second window, navigate to the test member's profile, and keep it
   visible. Role changes appear without a page reload.

---

## Scenario 1 — Single assign

**Goal:** assigning a member to Day 1 in siege-web adds the `Attack Day 1` Discord role within
the SLO.

### Step 1.1 — Enable the feature flag

**Action:** In the Azure Portal, navigate to siege-web Container App → Environment Variables.
Set `DAY_ROLE_SYNC_ENABLED` to `true`. Save and allow the revision to restart (approximately
30 s).

**Expected result:** The Container App shows the new revision as active. No Discord role changes
happen yet — no assignment exists.

**If not:** Verify the environment variable name is exactly `DAY_ROLE_SYNC_ENABLED` (case-
sensitive). If the app fails to start, check siege-web logs for a startup error related to the
missing `DAY_ROLE_SYNC_URL` env var.

---

### Step 1.2 — Assign test member to Day 1

**Action:** In siege-web, create or open a siege. Assign your test member to Attack Day 1 (UI:
drag or click the Day 1 slot, or use the API directly:
`PUT /sieges/{siege_id}/members/{member_id}` with body `{"attack_day": 1}`).

**Start your stopwatch when the PUT response returns.**

**Expected result:** Within 5 s, the `Attack Day 1` role appears on the test member's profile in
Discord.

**Diagnostic if role does not appear within 10 s:**

1. Filter mom-bot's logs for `role_sync` events with `discord_id=<your test member snowflake>`:
   ```
   role_sync correlation_id=… discord_id=<ID> … status=applied
   ```
   If you see `status=applied`, the bot acted — check that you are looking at the correct
   Discord member. If you see `status=skipped reason=member_not_in_guild`, the member's Discord
   account is not a member of the guild the bot is connected to.

2. If no `role_sync` log appears at all, the webhook did not reach mom-bot. Check:
   - `DAY_ROLE_SYNC_URL` on siege-web Container App (must be the full URL of mom-bot's
     `/api/internal/role-sync` endpoint, including the scheme).
   - siege-web logs for `role_sync_skip flag=false` — if present, the flag did not take effect.
   - mom-bot Container App health — if it failed to start (e.g. `role_preflight_complete`
     log never appeared), no requests will be processed.

3. If you see `status=failed`, check for `ROLE_HIERARCHY_LOST_AT_RUNTIME` or
   `ROLE_HIERARCHY_MISCONFIGURED` log events. See the runbook
   (`docs/operations/day-role-sync-runbook.md`) under "Hierarchy violation incident playbook".

---

### Step 1.3 — Record timing

**Action:** Note the elapsed time from PUT return to role visible in Discord.

**Expected result:** Under 5 s. Record the value.

---

## Scenario 2 — Reassign (swap)

**Goal:** moving the same member from Day 1 to Day 2 removes the Day 1 role and adds the Day 2
role atomically.

### Step 2.1 — Reassign to Day 2

**Action:** In siege-web, update the same test member's assignment from Day 1 to Day 2
(`PUT /sieges/{siege_id}/members/{member_id}` with `{"attack_day": 2}`).

**Start stopwatch when PUT response returns.**

**Expected result within 5 s:**
- `Attack Day 1` role is no longer visible on the member in Discord.
- `Attack Day 2` role is visible.

**Diagnostic if only one of the two changes happens (partial outcome):**

1. Filter logs for `role_sync … status=partial reason=remove_of_other_day_failed_403`.
   If present, the add of Day 2 succeeded but the remove of Day 1 failed with Discord 403.
   This indicates a hierarchy issue (Day 1 role moved above the bot's role after startup).
   See `docs/operations/day-role-sync-runbook.md` § "Hierarchy violation incident playbook".

2. The member now holds both `Attack Day 1` and `Attack Day 2` simultaneously. This is the
   expected partial-response state. The system does not self-heal automatically — the next
   legitimate assignment for this member will overwrite the state and correct it. Manual operator
   intervention: strip the unwanted role in Discord (Server Settings → Members → find the member
   → remove the stale role).

**Diagnostic if swap does not appear at all:** Follow the same diagnostics as Scenario 1 Step 1.2.

---

## Scenario 3 — Unassign (clear)

**Goal:** removing a member's day assignment removes the Discord role.

### Step 3.1 — Clear the assignment

**Action:** In siege-web, clear the test member's assignment (`PUT` with `{"attack_day": null}`,
or use the UI clear/remove action).

**Start stopwatch when PUT response returns.**

**Expected result within 5 s:** The `Attack Day 2` role (or whichever day role the member
currently holds) is removed from the member in Discord. No day roles remain.

**Diagnostic:**

1. Filter logs for `role_sync … status=applied`. The `removed` field should contain the role
   snowflake. A `status=skipped reason=already_lacks_role` means the member held no day role
   before the clear — correct if the role was never assigned, but unexpected if the preceding
   scenario passed.
2. If you see `WARNING mom_bot.roles.service role_not_seeded … day_number=None` instead of
   `status=applied`, the receiver is running a pre-#204 image and is incorrectly keying
   the role lookup off the inbound `day_number=null` rather than consulting prior state.
   Redeploy with the fix before continuing.
3. If no log appears at all, follow the diagnostics from Step 1.2.

---

## Scenario 4 — Bulk auto-assign

**Goal:** a siege-web bulk assignment operation fans out one webhook call per affected member;
all members receive the correct day role; and the `role_sync_bulk_summary` log event appears on
the siege-web side with matching counts.

**Note:** The `role_sync_bulk_summary` log is emitted by siege-web (PR #411), not by mom-bot.
You will need siege-web's logs for this step, in addition to mom-bot's logs.

### Step 4.1 — Trigger bulk auto-assign

**Action:** In siege-web, use the attack-day balance auto-assign feature
(`POST /sieges/{siege_id}/members/auto-assign-attack-day/apply`) to assign a batch of members
(at least 3, ideally 5 or more, to make the fan-out observable).

**Expected result:**

1. In siege-web's logs, one `role_sync_bulk_summary` event appears with:
   - `fired: N of N` (where N is the number of members with a Discord ID who had a transition)
   - `skipped_no_discord_id: K` (count of members without a Discord ID — these were filtered
     at siege-web before any webhook call was emitted)
   - `scheduling_failed_at_index: null` (no partial fan-out)
   - A `correlation_id` that matches across all individual mom-bot `role_sync` log events from
     this batch

2. In mom-bot's logs, one `role_sync` event per affected member appears, each carrying the same
   `correlation_id`. Each should have `status=applied` (or `status=skipped reason=already_has_role`
   for members who already held the correct role before the bulk op).

3. In Discord, every affected member now holds the correct `Attack Day N` role.

**Diagnostic if `fired` count is lower than expected:**

- `skipped_no_discord_id` covers members without a linked Discord account. This is expected and
  not an error — siege-web filters at the sender layer and never emits a webhook for them.
- If `fired` is lower than expected and `skipped_no_discord_id` does not account for the gap,
  check whether `scheduling_failed_at_index` is non-null. A non-null value means the fan-out
  loop raised an exception partway through. Members whose index was before the failure point were
  already enqueued and will fire; members after the failure index were not enqueued. This is a
  siege-web bug — file it.

**Diagnostic if some mom-bot `role_sync` events are `status=skipped reason=stale_write`:**

This is expected under concurrent edits. See the runbook § "Stale-write skips".

---

## Scenario 5 — Partial-response smoke

**Status: cannot be exercised in the current codebase.**

The plan (§ D1) specifies a `MOM_BOT_FORCE_PARTIAL_FOR_DISCORD_ID` environment variable as a
dev-only test seam that would force a 403 on one of the two role mutations, allowing the operator
to observe the partial state on a live member. This seam does not exist in the deployed code.
Grepping `src/` for `FORCE_PARTIAL` returns no matches (verified 2026-05-14).

The `partial` response path is covered by unit tests (via mocked `discord.Forbidden` in
`tests/`) but cannot be triggered against the live Discord API without either corrupting the
hierarchy or adding the test seam.

**Tracked in [#74](https://github.com/glitchwerks/mom-bot/issues/74).** Once that ships, the
seam will be set via the bot's `MOM_BOT_FORCE_PARTIAL_FOR_DISCORD_ID` env var; rerun this
scenario then.

**What a partial state looks like in practice (documented for operator awareness):**

When a partial response occurs (for example, add of Day 2 succeeded but remove of Day 1 failed),
the member holds both `Attack Day 1` and `Attack Day 2` simultaneously. The log reads:
```
role_sync … status=partial reason=remove_of_other_day_failed_403 added=[<day2_role_id>] removed=[]
```

The member remains in this dual-role state until the next legitimate `assign` or `unassign` for
that member triggers a fresh sync. The system does not self-heal on a timed basis.

**Reminder behavior in the dual-role state:** The reminder scheduler (`src/mom_bot/reminders/`)
fires channel pings using the `role_mention_id` column on the `reminders` table. That column
stores the snowflake of a reminder's associated Discord role (e.g. a "Hydra" or "Chimera"
reminder role), which is seeded from a separate KV secret entirely unrelated to the `Attack Day N`
roles. A member holding both `Attack Day 1` and `Attack Day 2` simultaneously will receive pings
from any reminder whose `role_mention_id` matches either day role — but in the current codebase,
no reminder row has its `role_mention_id` set to either `Attack Day 1` or `Attack Day 2`.
Day roles are assignment roles, not reminder roles. Double-pinging from the reminder scheduler is
not a risk in the current configuration.

---

## Timing record

After running Scenarios 1 through 4, record the worst observed end-to-end latency for each:

| Scenario | Repetitions | Worst observed latency | SLO pass (< 5 s)? |
|---|---|---|---|
| 1 — Single assign | | | |
| 2 — Reassign (swap) | | | |
| 3 — Unassign (clear) | | | |
| 4 — Bulk auto-assign (per member) | | | |

The bulk scenario timing is measured per member (from the moment the HTTP bulk request returns
to the moment any single member's role is visible). Individual timing for each member in the bulk
fan-out is not observable end-to-end; measure the last member to receive their role as the
representative worst case.

---

## If something breaks

If any scenario fails and you need to stop the feature immediately, flip the producer flag:

```
az containerapp update -g siege-web-dev -n siege-web-api-dev --set-env-vars DAY_ROLE_SYNC_ENABLED=false
```

This halts all outbound webhook calls without touching mom-bot. For a full rollback decision tree —
including receiver-side revert, state cleanup, and verification steps — see
`docs/operations/day-role-sync-runbook.md` § 7 (Rollback).

---

## After the smoke passes

If all scenarios pass within SLO:

1. Leave `DAY_ROLE_SYNC_ENABLED=true` if you are ready to go live.
2. If you want to remain in a staging state, flip back to `false` — no harm done; any roles
   already assigned to the test member stay in place (per the "persist until overwritten" design).
3. File the partial-response seam issue (Scenario 5) before the epic is closed.

Cross-references:
- Runbook: `docs/operations/day-role-sync-runbook.md`
- Pre-flight checklist: `docs/operations/discord-roles-preflight.md`
- Parent epic: `glitchwerks/mom-bot#6`
- Wire contract: [`glitchwerks/rsl-mom-apps` — `contracts/day-role-sync.md`](https://github.com/glitchwerks/rsl-mom-apps/blob/main/contracts/day-role-sync.md) (pinned: [`@5576807`](https://github.com/glitchwerks/rsl-mom-apps/blob/5576807101c04a9b595192cee2b9a02aed1c9c12/contracts/day-role-sync.md))
