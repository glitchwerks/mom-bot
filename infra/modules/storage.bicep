// storage.bicep — Azure Storage Account + File Share + Container Apps SMB binding
//                 for SQLite-on-AzureFile (SQLite stopgap, issue #87).
//
// Design choices:
// - Standard_LRS: cheapest tier adequate for a single-region SQLite file store.
//   Cost: ~$0.25-1.10/mo for the ≤1 GiB SQLite DB + WAL files.
//   Replication to ZRS or GRS is explicitly NOT needed for this stopgap — the
//   SQLite DB is the source of truth and is covered by daily share snapshots
//   (Policy 2, issue #87). Full HA replication is deferred to the PostgreSQL
//   migration (Epic 1+).
// - File Share quota: 1 GiB — smallest addressable quota. Adequate for mom-bot's
//   SQLite DB (expected to stay < 100 MiB for the foreseeable future).
// - SMB mount (storageType: AzureFile, accessMode: ReadWriteOnce) — matched to
//   the Container Apps managed-environment storage binding. NFS is not available
//   on Consumption plan environments.
// - Storage account name must be globally unique, 3-24 chars, lowercase
//   alphanumeric only. Derived via a 6-char hash of the resource group ID so
//   repeat deployments produce a stable name.

@description('Azure region for the storage account.')
param location string

@description('Name of the Container Apps managed environment (used for the SMB binding).')
param containerAppsEnvironmentName string

@description('Optional override for the storage account name (3-24 chars, lowercase alphanumeric). Defaults to a deterministic derived name.')
@minLength(3)
@maxLength(24)
param storageAccountName string = 'stomombot${uniqueString(resourceGroup().id)}'

// ---------------------------------------------------------------------------
// Storage Account
// ---------------------------------------------------------------------------

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    // Large File Shares must be enabled for FileStorage kind, but for StorageV2
    // standard file shares are available without it (quota ≤ 5 TiB).
  }
}

// ---------------------------------------------------------------------------
// File Share — mom-bot-data
// ---------------------------------------------------------------------------

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileService
  name: 'mom-bot-data'
  properties: {
    shareQuota: 1 // GiB — minimum addressable quota
    enabledProtocols: 'SMB'
  }
}

// ---------------------------------------------------------------------------
// Container Apps managed-environment storage binding (SMB)
// ---------------------------------------------------------------------------

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: containerAppsEnvironmentName
}

resource storageBinding 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: cae
  name: 'mom-bot-data-binding'
  properties: {
    azureFile: {
      accountName: storageAccount.name
      accountKey: storageAccount.listKeys().keys[0].value
      shareName: fileShare.name
      accessMode: 'ReadWriteOnce'
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Name of the Container Apps managed-environment storage binding. Pass to containerapp.bicep as storageBindingName.')
output storageBindingName string = storageBinding.name

@description('Name of the Storage Account. Use in az storage share snapshot create commands (Policy 2 runbook).')
output storageAccountName string = storageAccount.name
