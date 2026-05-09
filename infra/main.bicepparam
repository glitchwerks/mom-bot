// main.bicepparam — parameter bindings for main.bicep (prod / A++ model).
//
// Values bound here match the locked spec from Epic 0.4.
// Most values here are repo-stable. Provisioning-run-specific identifiers
// (e.g. ghaServicePrincipalObjectId) come from environment variables at
// deploy time via readEnvironmentVariable(). See infra/aad-runbook.md
// Step 5 for the env-var export commands.

using './main.bicep'

param location = 'eastus2'
param resourceGroupName = 'mom-bot'
param keyVaultName = 'kv-mombot-eastus2'
param managedIdentityName = 'mi-mom-bot'
param containerAppsEnvironmentName = 'cae-mom-bot-eastus2'
param containerAppName = 'ca-mom-bot'

// Container image — update to a real digest before first deploy.
// Image build+push to GHCR is Epic 1 work; for v0 testing, manually push
// and set this to ghcr.io/glitchwerks/mom-bot:<sha>.
param containerImage = 'ghcr.io/glitchwerks/mom-bot:latest'

// Provisioning-run-specific identifier sourced from deploy-time env var.
// Export GHA_SP_OBJECT_ID before deploying (see infra/aad-runbook.md Step 4).
// The empty default satisfies compile-time validation; an empty value at
// deploy time will cause the role-assignment module to fail loudly.
param ghaServicePrincipalObjectId = readEnvironmentVariable('GHA_SP_OBJECT_ID', '')
