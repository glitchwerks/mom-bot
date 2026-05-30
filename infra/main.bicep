// main.bicep — top-level orchestrator for mom-bot Azure infrastructure.
// Scope: subscription — creates the resource group and delegates to modules.
//
// Deploy with (PowerShell):
//   az deployment sub create `
//     --location eastus2 `
//     --template-file infra/main.bicep `
//     --parameters infra/main.bicepparam `
//     --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0

targetScope = 'subscription'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Azure region for all resources.')
param location string = 'eastus2'

@description('Name of the resource group to create.')
param resourceGroupName string = 'mom-bot'

@description('Name of the Key Vault to create (max 24 chars).')
@maxLength(24)
param keyVaultName string = 'kv-mombot-eastus2'

@description('Name of the user-assigned managed identity for runtime KV access.')
param managedIdentityName string = 'mi-mom-bot'

@description('Name of the Container Apps Environment.')
param containerAppsEnvironmentName string = 'cae-mom-bot-eastus2'

@description('Name of the Container App.')
param containerAppName string = 'ca-mom-bot'

@description('Container image reference. Defaults to Microsoft quickstart (always pullable) until Epic 1 wires up GHCR image build+push.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Tenant ID — needed for Postgres AAD admin configuration.')
param tenantId string = subscription().tenantId

@description('Environment prefix used in MOM_BOT_ENV and to derive KV secret names (e.g. \'prod\' → \'prod-database-url\'). Default \'prod\' preserves current single-env behavior.')
param momBotEnv string = 'prod'

// ---------------------------------------------------------------------------
// Non-credential configuration values (issues #121, #236).
// These are plain config strings with no security value — source of truth is
// main.bicepparam. Credentials remain operator-set (see aad-runbook.md §8).
// ---------------------------------------------------------------------------

@description('Discord channel name where reminder notifications fire.')
param reminderChannelName string

@description('Discord role name to mention when reminders fire (e.g. \'Member\').')
param reminderMentionRoleName string

@description('Discord guild (server) snowflake ID for this environment. Must be a 17–20 digit numeric string.')
@minLength(17)
@maxLength(20)
param guildId string

// ---------------------------------------------------------------------------
// Resource group
// ---------------------------------------------------------------------------

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
}

// ---------------------------------------------------------------------------
// Managed identity
// ---------------------------------------------------------------------------

module identity 'modules/managed-identity.bicep' = {
  name: 'deploy-managed-identity'
  scope: rg
  params: {
    location: location
    managedIdentityName: managedIdentityName
  }
}

// ---------------------------------------------------------------------------
// Key Vault
// ---------------------------------------------------------------------------

module kv 'modules/keyvault.bicep' = {
  name: 'deploy-keyvault'
  scope: rg
  params: {
    location: location
    keyVaultName: keyVaultName
    managedIdentityPrincipalId: identity.outputs.principalId
    momBotEnv: momBotEnv
    reminderChannelName: reminderChannelName
    reminderMentionRoleName: reminderMentionRoleName
    guildId: guildId
  }
}

// ---------------------------------------------------------------------------
// PostgreSQL (replaces AzureFile + SQLite stopgap — issue #91)
// Depends implicitly on containerApp because containerAppOutboundIps is
// sourced from containerApp.outputs.outboundIpAddresses — Bicep resolves
// the ordering automatically via the symbol reference (issue #120 Part 2).
// ---------------------------------------------------------------------------

module postgres 'modules/postgres.bicep' = {
  name: 'deploy-postgres'
  scope: rg
  params: {
    location: location
    tenantId: tenantId
    managedIdentityPrincipalId: identity.outputs.principalId
    managedIdentityName: managedIdentityName
    containerAppOutboundIps: containerApp.outputs.outboundIpAddresses
  }
}

// ---------------------------------------------------------------------------
// Observability (Log Analytics Workspace + Application Insights)
// Must be declared before containerApp so Bicep resolves the symbol
// references in the containerApp params block below.
// ---------------------------------------------------------------------------

module observability 'modules/observability.bicep' = {
  name: 'deploy-observability'
  scope: rg
  params: {
    location: location
    baseName: 'mom-bot-eastus2'
  }
}

// ---------------------------------------------------------------------------
// Container Apps (environment + app)
// ---------------------------------------------------------------------------

module containerApp 'modules/containerapp.bicep' = {
  name: 'deploy-containerapp'
  scope: rg
  params: {
    location: location
    containerAppsEnvironmentName: containerAppsEnvironmentName
    containerAppName: containerAppName
    managedIdentityId: identity.outputs.id
    managedIdentityClientId: identity.outputs.clientId
    containerImage: containerImage
    keyVaultName: keyVaultName
    keyVaultUri: kv.outputs.uri
    maxReplicas: 1
    momBotEnv: momBotEnv
    logAnalyticsCustomerId: observability.outputs.logAnalyticsCustomerId
    logAnalyticsSharedKey: observability.outputs.logAnalyticsSharedKey
    appInsightsConnectionString: observability.outputs.appInsightsConnectionString
  }
}

// ---------------------------------------------------------------------------
// Migrations job (Container Apps Job — issue #255)
// Replaces the exec-based alembic step in deploy.yml.  Runs alembic upgrade
// head under mi-mom-bot UAMI in the same CAE as ca-mom-bot, sharing the CAE
// outbound IPs already allowlisted in the Postgres firewall (spec §3 Q7).
// Depends implicitly on containerApp (shares the same CAE resource ID).
// ---------------------------------------------------------------------------

module migrationsJob 'modules/migrations-job.bicep' = {
  name: 'deploy-migrations-job'
  scope: rg
  params: {
    location: location
    // CAE resource ID — sourced from containerApp.outputs.caeId so Bicep
    // establishes an implicit dependency: the CAE must exist before the job
    // is deployed.  No explicit dependsOn needed.
    environmentId: containerApp.outputs.caeId
    containerImage: containerImage
    managedIdentityId: identity.outputs.id
    managedIdentityClientId: identity.outputs.clientId
    postgresHost: postgres.outputs.fqdn
    postgresDatabase: postgres.outputs.databaseName
  }
}
