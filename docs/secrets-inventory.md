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

## Inventory

| Secret name (in KV) | Purpose | Class | Source / owner | Rotation cadence |
|---|---|---|---|---|
| `dev-discord-token` | Discord bot OAuth token for local dev (may be the same shared token or a test-bot token) | Runtime | Discord Developer Portal — @cbeaulieu-gt | On compromise; otherwise never unless moving to test bot |
| `prod-discord-token` | Discord bot OAuth token for production | Runtime | Discord Developer Portal — @cbeaulieu-gt | On compromise; otherwise never (token is tied to the app registration, not the user) |
| `dev-database-url` | SQLAlchemy connection URL for local dev (SQLite file) | Runtime | Developer-set; default `sqlite:///./mom_bot_dev.db` | N/A (SQLite local path) |
| `prod-database-url` | SQLAlchemy connection URL for prod (SQLite on Container Apps volume, or Postgres later) | Runtime | Infra provisioning; default `sqlite:////data/mom_bot.db` | When DB backend changes (e.g. migrate to Postgres at v1.x) |
| `dev-app-insights-conn-string` | Azure Application Insights connection string for local dev (placeholder until Epic 1+) | Runtime | Azure portal — @cbeaulieu-gt | On workspace recreation; set to `PLACEHOLDER` until provisioned |
| `prod-app-insights-conn-string` | Azure Application Insights connection string for prod (placeholder until Epic 1+) | Runtime | Azure portal — @cbeaulieu-gt | On workspace recreation; set to `PLACEHOLDER` until provisioned |

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

## Open question

**#9 — Siege-web service token rotation cadence:** the `prod-*` secret for the
siege-web Bearer service token (used by mom-bot's sidecar to call siege-web)
will be added when the sidecar is implemented (Epic 2). Rotation cadence and
mechanism (Key Vault reference + Container App restart vs. zero-downtime double
rotation) is tracked as Open Question #9 in the framework plan.
