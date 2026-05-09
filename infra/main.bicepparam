// main.bicepparam — parameter bindings for main.bicep (prod / A++ model).
//
// Values bound here match the locked spec from Epic 0.4.
// All values here are repo-stable. Provisioning-run-specific identifiers
// (e.g. ghaServicePrincipalObjectId) are passed as --parameters CLI overrides
// at deploy time. See infra/aad-runbook.md.

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
