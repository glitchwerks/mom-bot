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

@description('Azure region for the Key Vault.')
param location string

@description('Name of the Key Vault (max 24 chars).')
@maxLength(24)
param keyVaultName string

@description('Principal ID of mi-mom-bot (user-assigned MI) for runtime read access.')
param managedIdentityPrincipalId string

@description('Environment prefix (e.g. \'prod\') used to derive KV secret names.')
param momBotEnv string

// ---------------------------------------------------------------------------
// Non-credential configuration values — provisioned via Bicep (issues #121, #236).
// Credentials (discord-token, database-url, app-insights-conn-string) remain
// operator-set via az keyvault secret set (see infra/aad-runbook.md Step 8).
// ---------------------------------------------------------------------------

@description('Discord channel name where reminder notifications fire (e.g. \'moms-reminders\').')
param reminderChannelName string

@description('Discord role name to mention when reminders fire (e.g. \'Member\').')
param reminderMentionRoleName string

@description('Discord guild (server) snowflake ID for this environment.')
param guildId string

// ---------------------------------------------------------------------------
// Built-in RBAC role definition IDs (stable; do not parameterize)
// ---------------------------------------------------------------------------

// Key Vault Secrets User — allows get + list on secrets (runtime reads).
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

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
// Non-credential KV secrets — provisioned by Bicep (issues #121, #236).
// Operators no longer run `az keyvault secret set` for these values;
// Bicep is the source of truth. Values come from main.bicepparam.
// ---------------------------------------------------------------------------

resource reminderChannelNameSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: '${momBotEnv}-reminder-channel-name'
  properties: {
    value: reminderChannelName
  }
}

resource reminderMentionRoleNameSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: '${momBotEnv}-reminder-mention-role-name'
  properties: {
    value: reminderMentionRoleName
  }
}

resource guildIdSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: '${momBotEnv}-guild-id'
  properties: {
    value: guildId
  }
}

@description('Resource ID of the Key Vault.')
output id string = kv.id

@description('URI of the Key Vault (used by azure-keyvault-secrets SDK).')
output uri string = kv.properties.vaultUri
