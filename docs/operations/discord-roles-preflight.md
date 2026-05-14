# Discord day-role sync — pre-deploy ops checklist

**Scope:** Epic 2.6. Covers the one-time steps a guild administrator must complete before enabling the
day-role sync feature. This checklist applies to every environment (dev, prod) independently.

## Purpose

Epic 2.6 delivers automatic Discord role membership for siege day-assignments. When a member's
`attack_day` field is set, changed, or cleared in siege-web, siege-web emits a
**day-role-sync webhook** to mom-bot. Mom-bot — the first conforming receiver of that contract —
adds or removes the corresponding `Attack Day N` Discord role on the member's account.

The wire contract is defined in `docs/webhooks/day-role-sync.md` inside the
`glitchwerks/rsl-siege-manager` repo (tracked by `glitchwerks/rsl-siege-manager#400`;
not yet authored). Siege-web is the producer;
mom-bot is one conforming receiver. Switching receivers requires only a configuration change — no
code change on the siege-web side.

Roles persist between sieges until overwritten. Mom-bot never creates or deletes the
`Attack Day N` roles themselves — those are admin-managed and must exist before the first sync fires.

---

## Pre-flight checklist

Work through these items top-to-bottom before flipping `DAY_ROLE_SYNC_ENABLED` to `true`.

### Roles pre-created in the Discord guild

- [ ] `Attack Day 1` role exists in the guild (Server Settings → Roles)
- [ ] `Attack Day 2` role exists in the guild (Server Settings → Roles)

The exact display names matter. The startup seed (sub-issue #62) resolves roles by name using
the `DAY_1_ROLE_NAME` and `DAY_2_ROLE_NAME` Key Vault secrets. If the name in KV does not match
the Discord display name exactly (case-sensitive), the bot will log `CRITICAL` and exit at startup.

Mom-bot never creates these roles. If a role is deleted and recreated, its snowflake (ID) changes.
See "Common failure modes" below for the remediation steps.

### Role hierarchy ordering

- [ ] Mom-bot's bot role is ranked **above** every `Attack Day N` role in the guild's role list
  (Server Settings → Roles — drag-to-reorder if needed)

Discord forbids a bot from modifying roles ranked at-or-above its own highest role. This is the
most common deploy footgun. The bot performs a startup preflight check and exits with
`ROLE_HIERARCHY_MISCONFIGURED` if this condition is not met, but the check is cheap only because
you verified it here first.

Any human-managed role ranked above mom-bot (e.g. `Clan Deputies`, `Admin`) is naturally out of
the bot's reach — that is by design.

### Install bitfield includes `MANAGE_ROLES`

- [ ] Mom-bot's integration role has **Manage Roles** ticked in Server Settings → Roles →
  _(mom-bot role)_ → Permissions

`MANAGE_ROLES` is bit `1 << 28` — decimal `268435456`, hex `0x10000000`. The conservative install
URL integer for mom-bot is `17592454531072`; this already includes `MANAGE_ROLES`. If reinstalling,
verify the URL's `permissions=` integer equals or is a superset of `17592454531072`.

For the full permissions reference, including layer definitions, scopes, and intents, see
`docs/discord-permissions-reference.md`.

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
2. In siege-web, manually assign a test member to Attack Day 1 (via the UI or a direct API call to
   `PUT /sieges/{siege_id}/members/{member_id}`).
3. Within approximately 5 seconds, the `Attack Day 1` Discord role should appear on the member in
   the guild member list. If it does not appear within 10 seconds, check App Insights (see below)
   before retrying.
4. Re-assign the same member to Attack Day 2. The `Attack Day 1` role should be removed and
   `Attack Day 2` added in one sync cycle.
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
regression: someone moved mom-bot's bot role below an `Attack Day N` role, or a new role was
inserted above mom-bot's role in the guild list.

Remediation: open Server Settings → Roles and drag mom-bot's bot role above all `Attack Day N`
roles. The next sync call will succeed. Mom-bot also logs `ROLE_HIERARCHY_LOST_AT_RUNTIME` at
ERROR level in App Insights when it detects this condition at runtime, which will surface the
affected role IDs.

### `Role not found` errors

The `day_role_map` table contains a stale snowflake. This happens when an `Attack Day N` role was
renamed, deleted, or recreated in Discord (Discord issues a new snowflake on recreate, even if the
display name is the same).

Remediation:
1. Manually strip the old role from any members currently holding it via the Discord UI or an admin
   tool (the bot logged the current holders at `CRITICAL` when the mismatch was detected at
   startup).
2. Update the `DAY_N_ROLE_NAME` Key Vault secret to match the current display name, or revert the
   rename in Discord so the name matches the stored secret.
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
