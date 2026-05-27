// main.bicepparam — parameter bindings for main.bicep (prod / A++ model).
//
// Values bound here match the locked spec from Epic 0.4.
// Most values here are repo-stable. The container image is resolved at
// deploy time via readEnvironmentVariable() — see infra/aad-runbook.md Step 5.

using './main.bicep'

param location = 'eastus2'
param resourceGroupName = 'mom-bot'
param keyVaultName = 'kv-mombot-eastus2'
param managedIdentityName = 'mi-mom-bot'
param containerAppsEnvironmentName = 'cae-mom-bot-eastus2'
param containerAppName = 'ca-mom-bot'

// Container image — resolved from the CONTAINER_IMAGE env var at deploy time.
// The workflow sets this to the currently-running live image before invoking
// Bicep, preventing a phantom containerImage diff in every what-if run.
// The quickstart fallback preserves cold-start behavior for fresh bootstraps
// where no image has been deployed yet.
param containerImage = readEnvironmentVariable('CONTAINER_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')

// Environment prefix for KV secret names (e.g. 'prod' → 'prod-database-url').
// Default 'prod' preserves current single-env behavior.
param momBotEnv = 'prod'

// Non-credential KV values — provisioned via Bicep (issues #121, #236).
// Credentials (discord-token, database-url, app-insights-conn-string) remain
// operator-set per infra/aad-runbook.md Step 8.
param reminderChannelName     = 'moms-reminders'
param reminderMentionRoleName = 'Member'
// UNVERIFIED: confirm the actual prod guild ID before deploying.
// Read from KV: az keyvault secret show --vault-name kv-mombot-eastus2 --name prod-guild-id --query value -o tsv
// Then update this value and re-run: az deployment sub what-if ... --parameters infra/main.bicepparam
param guildId = 'NEEDS_OPERATOR_CONFIRMATION'

