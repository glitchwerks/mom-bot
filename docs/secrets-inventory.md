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
| `prod-guild-id` | No — different Discord server per environment | Discord server (guild) ID for the production guild — same purpose as `dev-guild-id` | Runtime | Discord Developer Portal — enable Developer Mode, then right-click the server icon → Copy ID | Static — only changes when migrating to a new guild |
| `dev-database-url` | No — local SQLite vs Azure storage | SQLAlchemy connection URL for local dev (SQLite file) | Runtime | Developer-set; default `sqlite:///./mom_bot_dev.db` | N/A (SQLite local path) |
| `prod-database-url` | No — local SQLite vs Azure storage | SQLAlchemy connection URL for prod (SQLite on Container Apps volume, or Postgres later) | Runtime | Infra provisioning; default `sqlite:////data/mom_bot.db` | When DB backend changes (e.g. migrate to Postgres at v1.x) |
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

## Reminder scheduler secrets (added in #29, collapsed in #43, role dropped in #45, name-not-snowflake in #47)

The following secret is read by `_maybe_seed_reminders`
(`src/mom_bot/reminders/seed.py`) on first boot if the `reminders` table is
empty. It must be populated in both `kv-mom-bot-dev` and `kv-mom-bot-prod`
**before** deploying the bot for the first time; the bot exits with CRITICAL if
it is missing.

Both Hydra and Chimera reminders fire to the **same channel per env** — a
single `reminder-channel-name` secret replaces the previous per-reminder
`reminder-hydra-channel-id` / `reminder-chimera-channel-id` pair (#43).
The `role_mention_id` column stays nullable in the schema but both seeded rows
have `NULL` — reminders post without pinging any role (#45). A future operator
can `UPDATE reminders SET role_mention_id = <snowflake> WHERE name = '<X>'`
to re-add a ping for a specific reminder without touching `seed.py`.

The secret value is the **channel name** (plain string, e.g. `"reminders"`),
not a snowflake integer (#47). `seed.py` resolves the name to a snowflake at
first boot using the connected discord.py client
(`discord.utils.get(bot.guilds[0].text_channels, name=channel_name)`). The
resolved snowflake is stored in the `channel_id` DB column. The `channel_id`
column schema is unchanged (still an integer). If the channel is renamed after
the first successful seed, update with SQL:
`UPDATE reminders SET channel_id = <new-snowflake> WHERE name = '<X>'`

| Secret name (in KV) | Same in dev/prod? | Purpose | Type | Source / owner | Rotation cadence |
|---|---|---|---|---|---|
| `dev-reminder-channel-name` | No (different guilds) | Discord channel name where both Hydra and Chimera reminders fire — dev guild | String (e.g. `"reminders"`) | The channel name as shown in Discord — no Developer Mode required | Static; only changes if the channel is renamed (update KV + SQL) |
| `prod-reminder-channel-name` | No (different guilds) | Discord channel name where both Hydra and Chimera reminders fire — prod guild | String (e.g. `"reminders"`) | The channel name as shown in Discord — no Developer Mode required | Static |

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

#### Role-mention secret removed (#45)

If you previously seeded `*-reminder-mention-role-id` (added in #44, removed
in #45), those secrets are now unused. Delete them at your convenience:
`az keyvault secret delete --vault-name kv-mombot-eastus2 --name dev-reminder-mention-role-id`
and the same for `prod-reminder-mention-role-id`.

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
