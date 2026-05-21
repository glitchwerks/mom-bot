// main.bicep — top-level orchestrator for mom-bot Azure infrastructure.
// Scope: subscription — creates the resource group and delegates to modules.
//
// Deploy with (PowerShell):
//   $env:GHA_SP_OBJECT_ID = $SpObjectId
//   if (-not $env:GHA_SP_OBJECT_ID) { throw "Set GHA_SP_OBJECT_ID first" }
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

@description('Object ID (principal ID) of the mom-bot-gha service principal. Run: az ad sp show --id <appId> --query id -o tsv')
param ghaServicePrincipalObjectId string

@description('Container image reference. Defaults to Microsoft quickstart (always pullable) until Epic 1 wires up GHCR image build+push.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Tenant ID — needed for Postgres AAD admin configuration.')
param tenantId string = subscription().tenantId

@description('Static egress IP of the Container Apps Environment (cae-mom-bot-eastus2) for Postgres firewall whitelist. Retrieve with: az containerapp env show -n cae-mom-bot-eastus2 -g mom-bot --query properties.staticIp -o tsv')
param caeEgressIp string

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
    ghaServicePrincipalObjectId: ghaServicePrincipalObjectId
  }
}

// ---------------------------------------------------------------------------
// PostgreSQL (replaces AzureFile + SQLite stopgap — issue #91)
// ---------------------------------------------------------------------------

module postgres 'modules/postgres.bicep' = {
  name: 'deploy-postgres'
  scope: rg
  params: {
    location: location
    tenantId: tenantId
    managedIdentityPrincipalId: identity.outputs.principalId
    managedIdentityName: managedIdentityName
    caeEgressIp: caeEgressIp
  }
}

// ---------------------------------------------------------------------------
// Storage (AzureFile backing for SQLite — stopgap, issue #87)
// Deploys before containerApp so its storageAccountName output is available
// when containerapp.bicep creates the CAE storage binding. No dependsOn
// needed — the output reference creates the ordering implicitly.
// ---------------------------------------------------------------------------

module storage 'modules/storage.bicep' = {
  name: 'deploy-storage'
  scope: rg
  params: {
    location: location
  }
}

// ---------------------------------------------------------------------------
// Container Apps (environment + app + CAE storage binding)
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
    ghaServicePrincipalObjectId: ghaServicePrincipalObjectId
    keyVaultUri: kv.outputs.uri
    storageAccountName: storage.outputs.storageAccountName
    maxReplicas: 1
  }
}
