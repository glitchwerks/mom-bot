# Secrets Inventory

Catalog of all secrets stored in Key Vault `kv-mombot-eastus2`
(subscription `213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0`, tenant
`cmbdevoutlook333.onmicrosoft.com`).

**Values do not belong in this file.** This file records names, purpose,
ownership, and rotation cadence only. Actual values are set via
`az keyvault secret set` (see `infra/aad-runbook.md` Step 8).

## Secret prefix scheme

All secrets follow the pattern `<env>-<name>`:

- `dev-*` — read by developer laptops via `az login` + `DefaultAzureCredential`
- `prod-*` — read by `mi-mom-bot` (Container App managed identity) at runtime

Most secrets vary by environment. A notable exception: `discord-token` holds the
**same value** in both the `dev-` and `prod-` slots while a single bot application
serves both environments. The "Same in dev/prod?" column in the inventory below
makes this explicit so you know whether to paste once or twice during seeding.

## Inventory

| Secret name (in KV) | Same in dev/prod? | Purpose | Class | Source / owner | Rotation cadence |
|---|---|---|---|---|---|
| `dev-discord-token` | Yes — single bot app (paste once, write twice; see runbook Step 8) | Discord bot OAuth token for local dev | Runtime | Discord Developer Portal — @cbeaulieu-gt | On compromise; or when splitting into a separate dev bot application |
| `prod-discord-token` | Yes — single bot app (same value as `dev-discord-token`) | Discord bot OAuth token for production | Runtime | Discord Developer Portal — @cbeaulieu-gt | On compromise; or when splitting into a separate dev bot application |
| `dev-guild-id` | No — different Discord server per environment | Discord server (guild) ID for the dev guild — used to register guild-scoped slash commands instantly at startup | Runtime | Discord Developer Portal — enable Developer Mode, then right-click the server icon → Copy ID | Static — only changes when migrating to a new guild |
| `prod-guild-id` | No — different Discord server per environment | Discord server (guild) ID for the production guild — same purpose as `dev-guild-id` | Runtime | **Bicep-managed** (issues #121, #236): `param guildId` in `infra/main.bicepparam`; was operator-set via `az keyvault secret set` | Static — only changes when migrating to a new guild |
| `dev-database-url` | No — SQLite (dev) vs Postgres (prod) | SQLAlchemy connection URL for local dev (SQLite file for developer convenience) | Runtime | Developer-set; default `sqlite:///./mom_bot_dev.db` | N/A (SQLite local path) |
| `prod-database-url` | No — SQLite (dev) vs Postgres (prod) | SQLAlchemy connection URL for prod (Postgres flexible server `pg-mombot-*` in resource group `mom-bot`) | Runtime | Infra provisioning; set to the Postgres connection URL with AAD token injection | On flexible server recreation or credential rotation |
| `dev-app-insights-conn-string` | No — separate AI instances when Epic 1 ships | Azure Application Insights connection string for local dev (placeholder until Epic 1+) | Runtime | Azure portal — @cbeaulieu-gt | On workspace recreation; set to `PLACEHOLDER` until provisioned |
| `prod-app-insights-conn-string` | No — separate AI instances when Epic 1 ships | Azure Application Insights connection string for prod (placeholder until Epic 1+) | Runtime | Azure portal — @cbeaulieu-gt | On workspace recreation; set to `PLACEHOLDER` until provisioned |

## Access matrix

| Identity | Role | Scope | What it can do |
|---|---|---|---|
| `mi-mom-bot` (Container App MI) | Key Vault Secrets User | `kv-mombot-eastus2` | `get` + `list` secrets (read-only runtime access) |
| `mom-bot-gha` SP (GitHub Actions OIDC) | Key Vault Secrets Officer | `kv-mombot-eastus2` | `get`, `list`, `set`, `delete` secrets (deploy-time management) |
| Developer user account | Key Vault Secrets User | `kv-mombot-eastus2` | `get` + `list` secrets (local dev reads via `az login`) |

## Secrets NOT stored here

The following identifiers are stored as GitHub **repository variables** (not
KV secrets, because they are non-sensitive OIDC identifiers):

| Variable | Value |
|---|---|
| `AZURE_CLIENT_ID` | Client ID of `mom-bot-gha` app registration |
| `AZURE_TENANT_ID` | `48bca6c3-6d4f-4884-bc1a-648ae2362a32` |
| `AZURE_SUBSCRIPTION_ID` | `213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0` |

## Reminder scheduler secrets (added in #29, collapsed in #43, role dropped in #45, name-not-snowflake in #47, role restored by name in #51)

The following secrets are read by `_maybe_seed_reminders`
(`src/mom_bot/reminders/seed.py`) on first boot if the `reminders` table is
empty. They must be populated in `kv-mombot-eastus2` **before** deploying
the bot for the first time; the bot exits with CRITICAL if any are missing.

> **Warning — channel or role renames require a manual SQL UPDATE.**
> Both the channel name and role name are resolved to snowflakes once at first
> boot and stored in `channel_id` and `role_mention_id` DB columns. If either
> is renamed after the initial seed, `seed.py` will not re-run (the table is
> non-empty). Update with SQL:
> `UPDATE reminders SET channel_id = <new-snowflake> WHERE name = '<X>'`
> `UPDATE reminders SET role_mention_id = <new-snowflake> WHERE name = '<X>'`

Both Hydra and Chimera reminders fire to the **same channel per env** and
ping the **same role per env**. `seed.py` resolves both names to snowflakes
at first boot via the connected discord.py client.

Secret values are **plain strings** (not snowflake integers):

- `reminder-channel-name` stores the channel name (e.g. `"reminders"`).
  `seed.py` resolves via `discord.utils.get(guild.text_channels, name=...)`.
- `reminder-mention-role-name` stores the role name (e.g. `"Member"`).
  `seed.py` resolves via `discord.utils.get(guild.roles, name=...)`.

The resolved snowflakes are stored in the `channel_id` and `role_mention_id`
DB columns respectively. Column schemas are unchanged (both INTEGER).

| Secret name (in KV) | Same in dev/prod? | Purpose | Type | Source / owner | Rotation cadence |
|---|---|---|---|---|---|
| `dev-reminder-channel-name` | No (different guilds) | Discord channel name where both Hydra and Chimera reminders fire — dev guild | String (e.g. `"reminders"`) | The channel name as shown in Discord — no Developer Mode required | Static; only changes if the channel is renamed (update KV + SQL) |
| `prod-reminder-channel-name` | No (different guilds) | Discord channel name where both Hydra and Chimera reminders fire — prod guild | String (e.g. `"reminders"`) | **Bicep-managed** (issue #121): `param reminderChannelName` in `infra/main.bicepparam`; was operator-set via `az keyvault secret set` | Static |
| `dev-reminder-mention-role-name` | Yes (role names typically match across guilds; same `"Member"` default as source bot `clan_reminders.py:L107`) | Discord role name to ping when Hydra and Chimera fire — dev guild | String (e.g. `"Member"`) | Discord: Settings → Roles — no Developer Mode required for the name; right-click role → Copy Role ID for the snowflake (for manual SQL fixes) | Static; only changes if the role is renamed (update KV + SQL) |
| `prod-reminder-mention-role-name` | Yes (same as dev; see above) | Discord role name to ping when Hydra and Chimera fire — prod guild | String (e.g. `"Member"`) | **Bicep-managed** (issue #121): `param reminderMentionRoleName` in `infra/main.bicepparam`; was operator-set via `az keyvault secret set` | Static |

### Migration history

#### From per-reminder channel secrets → consolidated secret (#43)

If you previously seeded `*-reminder-{hydra,chimera}-channel-id` (the old
two-secret layout from before #43), follow these steps before deploying the
#43 code:

1. Copy either old value (both should be the same channel snowflake) into the
   new consolidated secret: `az keyvault secret set --vault-name kv-mombot-eastus2
   --name <env>-reminder-channel-id --value <channel-id>`. Repeat for both
   `dev-` and `prod-`.
2. Deploy the updated bot and verify it boots cleanly.
3. After confirming a clean boot, delete the old secrets:
   `az keyvault secret delete --vault-name kv-mombot-eastus2 --name <env>-reminder-hydra-channel-id`
   and the same for `<env>-reminder-chimera-channel-id`.

#### Role-mention secret removed (#45), restored as name-valued secret (#51)

If you previously seeded `*-reminder-mention-role-id` (added in #44, removed
in #45), those secrets used a snowflake integer value and are now superseded
by the name-valued `*-reminder-mention-role-name` secrets added in #51.
Delete the old snowflake secrets at your convenience:
`az keyvault secret delete --vault-name kv-mombot-eastus2 --name dev-reminder-mention-role-id`
and the same for `prod-reminder-mention-role-id`.

Then seed the new name-valued secrets:
```powershell
az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-reminder-mention-role-name --value "Member"
az keyvault secret set --vault-name kv-mombot-eastus2 --name prod-reminder-mention-role-name --value "Member"
```
Confirm the role name matches your guild's actual role before deploying.

#### From snowflake → channel name (#47)

If you previously seeded `*-reminder-channel-id` with a snowflake integer
(the format used from #43 through #46), replace it with the channel name
string:

```powershell
# Replace the snowflake secret with the channel name.
# No Developer Mode or right-click required — just type the channel name.
az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-reminder-channel-name --value "reminders"
az keyvault secret set --vault-name kv-mombot-eastus2 --name prod-reminder-channel-name --value "reminders"

# Delete the old snowflake secret (no longer read by seed.py).
az keyvault secret delete --vault-name kv-mombot-eastus2 --name dev-reminder-channel-id
az keyvault secret delete --vault-name kv-mombot-eastus2 --name prod-reminder-channel-id
```

New installs (no prior seeding) need only `reminder-channel-name` per env.
Go straight to Step 8 of `infra/aad-runbook.md`.

## Open question

**#9 — Siege-web service token rotation cadence:** the `prod-*` secret for the
siege-web Bearer service token (used by mom-bot's sidecar to call siege-web)
will be added when the sidecar is implemented (Epic 2). Rotation cadence and
mechanism (Key Vault reference + Container App restart vs. zero-downtime double
rotation) is tracked as Open Question #9 in the framework plan.
