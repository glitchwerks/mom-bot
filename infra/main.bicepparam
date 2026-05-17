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

// Container image — resolved from the CONTAINER_IMAGE env var at deploy time.
// The workflow sets this to the currently-running live image before invoking
// Bicep, preventing a phantom containerImage diff in every what-if run.
// The quickstart fallback preserves cold-start behavior for fresh bootstraps
// where no image has been deployed yet.
param containerImage = readEnvironmentVariable('CONTAINER_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')

// Provisioning-run-specific identifier sourced from deploy-time env var.
// Export GHA_SP_OBJECT_ID before deploying (see infra/aad-runbook.md Step 4).
// The empty default satisfies compile-time validation; an empty value at
// deploy time will cause the role-assignment module to fail loudly.
param ghaServicePrincipalObjectId = readEnvironmentVariable('GHA_SP_OBJECT_ID', '')

// ---------------------------------------------------------------------------
// PostgreSQL firewall parameters (Phase 1, issue #104 / epic #91)
// ---------------------------------------------------------------------------

// Operator egress IP for Postgres firewall.
// Look up your current egress IP with: (Invoke-WebRequest -Uri 'https://api.ipify.org').Content
// Or: curl -s https://api.ipify.org
param operatorIpAddress = 'CHANGE_ME_OPERATOR_IP'

// Static egress IP of the CAE environment for Postgres firewall.
// Look up with: az containerapp env show -n cae-mom-bot -g mom-bot --query 'properties.staticIp' -o tsv
param caeEgressIp = 'CHANGE_ME_CAE_STATIC_IP'
