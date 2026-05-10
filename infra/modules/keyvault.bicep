// keyvault.bicep — Key Vault kv-mombot-eastus2 with RBAC authorization.
//
// Design choices:
// - RBAC authorization mode (enableRbacAuthorization: true) — NOT access policies.
//   RBAC is the current best practice; access policies are a legacy model.
// - Soft-delete + purge protection enabled — prevents accidental data loss.
// - Public network access enabled — no private endpoint for v1.0.
//
// Role assignments:
// - mi-mom-bot (runtime) → Key Vault Secrets User (read-only; list + get)
// - mom-bot-gha (deploy pipeline) → Key Vault Secrets Officer (read + write)
//   so the deploy workflow can set/rotate secrets.

@description('Azure region for the Key Vault.')
param location string

@description('Name of the Key Vault (max 24 chars).')
@maxLength(24)
param keyVaultName string

@description('Principal ID of mi-mom-bot (user-assigned MI) for runtime read access.')
param managedIdentityPrincipalId string

@description('Object ID of the mom-bot-gha service principal for deploy-time write access.')
param ghaServicePrincipalObjectId string

// ---------------------------------------------------------------------------
// Built-in RBAC role definition IDs (stable; do not parameterize)
// ---------------------------------------------------------------------------

// Key Vault Secrets User — allows get + list on secrets (runtime reads).
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// Key Vault Secrets Officer — allows get, list, set, delete on secrets (deploy writes).
var kvSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

// ---------------------------------------------------------------------------
// Key Vault resource
// ---------------------------------------------------------------------------

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

// ---------------------------------------------------------------------------
// Role assignment — mi-mom-bot: Key Vault Secrets User (runtime, read-only)
// ---------------------------------------------------------------------------

resource roleAssignmentMI 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, managedIdentityPrincipalId, kvSecretsUserRoleId)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      kvSecretsUserRoleId
    )
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Role assignment — mom-bot-gha: Key Vault Secrets Officer (deploy, read+write)
// ---------------------------------------------------------------------------

resource roleAssignmentGHA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, ghaServicePrincipalObjectId, kvSecretsOfficerRoleId)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      kvSecretsOfficerRoleId
    )
    principalId: ghaServicePrincipalObjectId
    principalType: 'ServicePrincipal'
  }
}

@description('Resource ID of the Key Vault.')
output id string = kv.id

@description('URI of the Key Vault (used by azure-keyvault-secrets SDK).')
output uri string = kv.properties.vaultUri
