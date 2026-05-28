# Discord day-role sync — pre-deploy ops checklist

**Scope:** Epic 2.6. Covers the one-time steps a guild administrator must complete before enabling the
day-role sync feature. This checklist applies to every environment (dev, prod) independently.

## Purpose

Epic 2.6 delivers automatic Discord role membership for siege day-assignments. When a member's
`attack_day` field is set, changed, or cleared in siege-web, siege-web emits a
**day-role-sync webhook** to mom-bot. Mom-bot — the first conforming receiver of that contract —
adds or removes the corresponding `Siege - Day N Attacker` Discord role on the member's account.

The wire contract is defined in [`contracts/day-role-sync.md`](https://github.com/glitchwerks/rsl-mom-apps/blob/main/contracts/day-role-sync.md)
inside the `glitchwerks/rsl-mom-apps` repo (pinned: [`@5576807`](https://github.com/glitchwerks/rsl-mom-apps/blob/5576807101c04a9b595192cee2b9a02aed1c9c12/contracts/day-role-sync.md);
tracked by `glitchwerks/rsl-siege-manager#400`). Siege-web is the producer;
mom-bot is one conforming receiver. Switching receivers requires only a configuration change — no
code change on the siege-web side.

Roles persist between sieges until overwritten. Mom-bot never creates or deletes the
`Siege - Day N Attacker` roles themselves — those are admin-managed and must exist before the first sync fires.

---

## Pre-flight checklist

Work through these items top-to-bottom before flipping `DAY_ROLE_SYNC_ENABLED` to `true`.

### Roles pre-created in the Discord guild

- [ ] `Siege - Day 1 Attacker` role exists in the guild (Server Settings → Roles)
- [ ] `Siege - Day 2 Attacker` role exists in the guild (Server Settings → Roles)

The exact display names matter. The startup seed (`src/mom_bot/roles/seed.py`,
`seed_day_role_map`) resolves roles by literal-name match against
`DAY_ROLE_NAME_TEMPLATE.format(day=N)`. The current values are `"Siege - Day 1 Attacker"` and
`"Siege - Day 2 Attacker"`. This constant is hardcoded in `src/mom_bot/roles/seed.py:40` — it
is not configurable via environment variable or Key Vault secret at runtime. If no guild role
matches the expected name (case-sensitive), the bot logs `DAY_ROLE_NOT_FOUND` and exits at
startup.

Mom-bot never creates these roles. If a role is deleted and recreated, its snowflake (ID) changes.
See "Common failure modes" below for the remediation steps.

### Role hierarchy ordering

- [ ] Mom-bot's bot role is ranked **above** every `Siege - Day N Attacker` role in the guild's role list
  (Server Settings → Roles — drag-to-reorder if needed)

Discord forbids a bot from modifying roles ranked at-or-above its own highest role. This is the
most common deploy footgun. The bot performs a startup preflight check and exits with
`ROLE_HIERARCHY_MISCONFIGURED` if this condition is not met, but the check is cheap only because
you verified it here first.

Any human-managed role ranked above mom-bot (e.g. `Clan Deputies`, `Admin`) is naturally out of
the bot's reach — that is by design.

### Install bitfield includes `MANAGE_ROLES`

- [ ] Mom-bot's integration role has **Manage Roles** ticked in Server Settings → Roles →
  _(mom-bot role)_ → Permissions. See [Verifying the current grant](#verifying-the-current-grant)
  below for step-by-step instructions.

#### Required permissions bitfield

The table below lists every guild permission mom-bot uses, derived from a codebase audit of
`src/mom_bot/` conducted on 2026-05-14 (issue #63). Bit values follow the Discord API
permissions reference.[^perms-ref]

| Permission        | Bit | Hex          | Why mom-bot needs it                                                                        |
| ----------------- | --- | ------------ | ------------------------------------------------------------------------------------------- |
| `Send Messages`   | 11  | `0x00000800` | Reminder delivery (`channel.send` — `src/mom_bot/reminders/scheduler.py:256,260`); `/ping` response (`src/mom_bot/main.py:314`) |
| `Embed Links`     | 14  | `0x00004000` | Ephemeral embed responses; reminder formatting (`docs/discord-permissions-reference.md § Layer 2`) |
| `Attach Files`    | 15  | `0x00008000` | Sidecar `post-image` endpoint (`docs/discord-permissions-reference.md § Layer 2`)           |
| `Manage Roles`    | 28  | `0x10000000` | Toggle `Siege - Day N Attacker` role membership when siege-web pushes assignment changes (Epic 2.6, A2 PR #68; role-toggle A4 #64) |
| `Create Events`   | 44  | `0x100000000000` | Autonomous tank-week creation; cancel of bot-created events (`docs/discord-permissions-reference.md § Layer 2`) |
| **Total**         |     | **`0x10001000C800`**                 | Decimal: **`17592454531072`** |

> The combined integer `17592454531072` is the authoritative conservative install bitfield for
> mom-bot. It equals `Send Messages | Embed Links | Attach Files | Manage Roles | Create Events`.
> Do not hand-compute this — use the Developer Portal → OAuth2 → URL Generator to tick the
> permissions and let Discord compute the integer, then verify it matches `17592454531072`.

`MANAGE_ROLES` alone is `1 << 28` — decimal `268435456`, hex `0x10000000`. The full bitfield
`17592454531072` is a superset that includes `MANAGE_ROLES`. If the installed grant's
`permissions=` integer equals or exceeds `17592454531072`, `MANAGE_ROLES` is included.

The full permissions reference (six configuration layers, scopes, intents, role-ordering caveat,
and install URL persistence) is at `docs/discord-permissions-reference.md`.

[^perms-ref]: Discord API — Permissions: https://docs.discord.com/developers/topics/permissions (fetched 2026-05-08 for the original reference doc; bit assignments are stable and version-independent for the permissions listed here).

#### Verifying the current grant

Two complementary methods — use either or both.

**Method A — Discord Developer Portal (pre-deploy, definitive)**

1. Open [Discord Developer Portal](https://discord.com/developers/applications) and select the
   mom-bot application.
2. In the left sidebar, click **Installation**.
3. Under **Default Install Settings → Guild Install**, check **Permissions**. The picker should
   show `Send Messages`, `Embed Links`, `Attach Files`, `Manage Roles`, and `Create Events` all
   ticked.
4. Alternatively, scroll to the **Install Link** field at the top of the Installation tab and
   copy it. If the link contains `?permissions=` as a query parameter, decode that integer
   (it must be `17592454531072` or a superset). The Installation-tab link format stores
   permissions server-side rather than in the URL itself, so you may need to use the
   **URL Generator** (same portal, left sidebar) to generate an equivalent explicit URL for
   comparison — see `docs/discord-permissions-reference.md § Pinned install configuration`.

**Method B — Live guild inspection (post-install, immediate)**

1. In the target Discord server, open **Server Settings → Roles**.
2. Find the mom-bot integration role (it is created automatically when the bot is invited).
3. Click the role and inspect the **Permissions** tab. `Manage Roles` must be ticked.
4. Alternatively, open **Server Settings → Integrations → mom-bot**. Discord shows the scopes
   and permissions the bot was granted at the time of the last invite. If `Manage Roles` is
   absent here, the bot was invited with an older or incomplete bitfield — re-invite using the
   corrected URL below.

#### Corrected re-invite URL (if `MANAGE_ROLES` is missing)

If the live guild inspection shows `Manage Roles` is not ticked, re-invite the bot using:

```
https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot%20applications.commands&permissions=17592454531072
```

Replace `<APPLICATION_ID>` with the mom-bot application ID found in the Discord Developer Portal
under **General Information → APPLICATION ID**. For the deployed application, this value is
recorded in `docs/discord-permissions-reference.md § Pinned install configuration`.

You can also use the Developer Portal's **Installation tab** to re-save the permissions and
then use the Install Link from that tab — see `docs/discord-permissions-reference.md § Saving /
persisting the install configuration` for the recommended workflow.

#### Admin re-install procedure

Re-inviting the bot with a corrected bitfield is safe:

- **Members keep their roles.** Re-inviting the same bot application (same APPLICATION_ID) does
  not remove roles from any guild members. Role assignments are stored by Discord on the member,
  not on the bot's grant.
- **The bot stays in the server during re-invite.** The bot is not kicked when you start the
  re-invite flow. The old grant remains active until the new invite is authorized by a server
  administrator with the **Manage Server** permission.
- **If the re-invite is abandoned mid-flow**, the old grant stays in place. There is no
  partial-grant state — authorization is atomic. The bot continues operating under the prior
  permissions until the re-invite is completed.
- **Only one guild administrator needs to authorize.** The admin who clicks the invite link
  and clicks **Authorize** in the OAuth2 consent screen is the only person whose action is
  required. Other admins do not need to take any action.
- **After re-invite**, verify the updated grant using Method B above before proceeding with the
  remaining pre-flight checklist items.

### `day_role_map` table seeded

- [ ] After first bot startup, the `day_role_map` table has one row per attack day with
  `discord_role_id` matching the actual Discord role snowflakes in the guild

The seed runs automatically on startup (sub-issue #62). To verify, connect to the SQLite database
and run:

```sql
SELECT day_number, discord_role_id FROM day_role_map ORDER BY day_number;
```

Expected output: two rows (`day_number` 1 and 2, each with a non-null `discord_role_id`). Cross-
check the `discord_role_id` values against the role IDs visible in Discord (Server Settings → Roles
→ right-click a role → Copy Role ID — requires Developer Mode enabled in Discord User Settings).

### Feature flag default

- [ ] `DAY_ROLE_SYNC_ENABLED` is `false` on first deploy (this is the default; verify it is not
  overridden in the Container App environment variables)

Do not flip this flag to `true` until the smoke test below passes. The flag is the cross-repo gate:
siege-web will not emit webhook calls while it is `false`, so the system is safe to deploy in any
order with the flag off.

---

## Smoke test recipe

Run these steps in order after all pre-flight items are checked. Use a test guild or a non-critical
member account.

1. Set `DAY_ROLE_SYNC_ENABLED=true` on the siege-web Container App for the target environment.
2. In siege-web, manually assign a test member to attack day 1 (via the UI or a direct API call to
   `PUT /sieges/{siege_id}/members/{member_id}`).
3. Within approximately 5 seconds, the `Siege - Day 1 Attacker` Discord role should appear on the
   member in the guild member list. If it does not appear within 10 seconds, check App Insights
   (see below) before retrying.
4. Re-assign the same member to attack day 2. The `Siege - Day 1 Attacker` role should be removed
   and `Siege - Day 2 Attacker` added in one sync cycle.
5. Clear the assignment (set `attack_day` to null). Both day roles should be removed from the
   member.
6. In App Insights, search for `role_sync` log events scoped to the test member's `discord_id`.
   Every toggle (steps 2–5) should produce a `role_sync` event with `outcome=success`. A missing
   event means the webhook call did not reach mom-bot — check siege-web's `DAY_ROLE_SYNC_URL`
   configuration.

If all six steps pass, the deployment is healthy. Flip `DAY_ROLE_SYNC_ENABLED=false` again if
you are not ready to go live, then re-flip when you are.

---

## Rollback

Flip `DAY_ROLE_SYNC_ENABLED=false` on the siege-web Container App. This stops all outbound webhook
calls immediately — no redeployment required. Already-assigned Discord roles remain on members
(per the "persist until overwritten" lifecycle decision in issue #6); no automated cleanup runs.

If you also want to prevent mom-bot from processing any stray in-flight calls that arrived before
the flag was flipped, set `MOM_BOT_ROLE_SYNC_ENABLED=false` on the mom-bot Container App as well
(if that variable is defined for the deployed version).

---

## Common failure modes and remediation

### `403` from Discord on role modify

Mom-bot does not have permission to modify the target role. The most likely cause is role hierarchy
regression: someone moved mom-bot's bot role below a `Siege - Day N Attacker` role, or a new role
was inserted above mom-bot's role in the guild list.

Remediation: open Server Settings → Roles and drag mom-bot's bot role above all
`Siege - Day N Attacker` roles. The next sync call will succeed. Mom-bot also logs `ROLE_HIERARCHY_LOST_AT_RUNTIME` at
ERROR level in App Insights when it detects this condition at runtime, which will surface the
affected role IDs.

### `Role not found` errors

The `day_role_map` table contains a stale snowflake. This happens when a `Siege - Day N Attacker`
role was renamed, deleted, or recreated in Discord (Discord issues a new snowflake on recreate,
even if the display name is the same).

Remediation:
1. Manually strip the old role from any members currently holding it via the Discord UI or an admin
   tool (the bot logged the current holders at `CRITICAL` when the mismatch was detected at
   startup).
2. Correct the mismatch using one of two options:
   - **Rename the Discord role back** to the expected literal name (`Siege - Day 1 Attacker` or
     `Siege - Day 2 Attacker`) so it matches what the seed looks for.
   - **Edit the source constant** `DAY_ROLE_NAME_TEMPLATE` in `src/mom_bot/roles/seed.py` to
     reflect the new naming convention, then redeploy the bot.
   There is no Key Vault secret to update — the role name is hardcoded in the source.
   See also `docs/operations/day-role-sync-runbook.md` § "Operator remediation: Discord role
   rename" for the full step-by-step sequence.
3. Restart the bot. The startup seed will re-resolve the name to the new snowflake and UPSERT the
   `day_role_map` row cleanly.

### No `role_sync` log events appearing

Either `DAY_ROLE_SYNC_ENABLED` is `false` on the siege-web side (webhook calls are suppressed
before they leave siege-web), or the `DAY_ROLE_SYNC_URL` environment variable on siege-web points
at the wrong URL. Check siege-web's Container App environment variables first — both the flag and
the URL — before investigating mom-bot.

---

## Cross-references

- Parent epic: `glitchwerks/mom-bot#6`
- Webhook contract (canonical, owned by producer): `glitchwerks/rsl-siege-manager#400`
- Related sub-issues:
  - `#62` — `day_role_map` table and startup seed
  - `#63` — install bitfield verification (`MANAGE_ROLES`)
  - `#64` — role-toggle module and hierarchy preflight
  - `#65` — sidecar endpoint (`POST /api/internal/role-sync`)
  - `#66` — end-to-end smoke test and ops runbook
- Permissions reference: `docs/discord-permissions-reference.md`
- Operator runbook (steady-state monitoring, role-rename remediation): `docs/operations/day-role-sync-runbook.md`
