// managed-identity.bicep — user-assigned managed identity for runtime KV access.
//
// mi-mom-bot is assigned to the Container App so it can read dev-* / prod-*
// secrets from Key Vault via DefaultAzureCredential at runtime.
// The Key Vault Secrets User role assignment happens in keyvault.bicep.

@description('Azure region for the managed identity.')
param location string

@description('Name of the user-assigned managed identity.')
param managedIdentityName string

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
}

@description('Resource ID of the managed identity (for Container App assignment).')
output id string = identity.id

@description('Principal ID of the managed identity (for RBAC role assignments).')
output principalId string = identity.properties.principalId

@description('Client ID of the managed identity (for DefaultAzureCredential hints).')
output clientId string = identity.properties.clientId
