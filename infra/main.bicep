// main.bicep — top-level orchestrator for mom-bot Azure infrastructure.
// Scope: subscription — creates the resource group and delegates to modules.
//
// Deploy with:
//   az deployment sub create \
//     --location eastus2 \
//     --template-file infra/main.bicep \
//     --parameters infra/main.bicepparam \
//     --parameters ghaServicePrincipalObjectId=$SpObjectId \
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

@description('Container image reference (ghcr.io/glitchwerks/mom-bot:<sha>).')
param containerImage string = 'ghcr.io/glitchwerks/mom-bot:latest'

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
    containerImage: containerImage
    keyVaultName: keyVaultName
  }
}
