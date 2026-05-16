// -----------------------------------------------------------------------------
// SQLite-on-SMB risk acknowledgement (Policy 3, issue #87)
// -----------------------------------------------------------------------------
// This Container App runs SQLite over an AzureFile (SMB) volume mount.
// SQLite + SMB is NOT a supported production database topology — SMB does not
// honour the fsync/lock semantics SQLite assumes. Specific risks:
//   - Corruption window if the SMB connection drops mid-write.
//   - Locking is advisory only; concurrent writers will corrupt the file.
//     Mitigated by Policy 1 (@allowed([1]) maxReplicas — single writer).
//   - No point-in-time recovery; daily share snapshots (Policy 2) are the
//     recovery SLA (7-day retention; granularity = 1 day).
// This is a STOPGAP until PostgreSQL migration (Epic 1+). Do not extend the
// SQLite-on-SMB pattern to additional services.
// -----------------------------------------------------------------------------

// containerapp.bicep — Container Apps Environment + Container App ca-mom-bot.
//
// Design choices:
// - Consumption profile only (no workload profiles) — cheapest tier; mom-bot's
//   workload is single-replica, tiny CPU/memory. WorkloadProfiles adds cost and
//   complexity that buys nothing here.
// - Ingress disabled — the Discord bot is outbound-only for v1.0. The sidecar
//   /api/internal/role-sync endpoint (Epic 2.6) will re-enable ingress when it
//   lands; keeping it off now is smallest blast radius.
// - User-assigned MI only (mi-mom-bot) — attached as the sole identity so
//   ManagedIdentityCredential unambiguously selects it for Key Vault access.
//   SystemAssigned was previously also attached but caused the SDK to default
//   to the empty SystemAssigned principal (no role assignments) → KV 403s.
//   See #81 for the diagnosis. AZURE_CLIENT_ID is sourced from
//   mi-mom-bot.clientId and is required by ManagedIdentityCredential; the
//   ACA IMDS endpoint does not auto-select the sole UserAssigned identity.
// - Single replica (scale 0-1) — SQLite + WAL requires single writer.
//   Policy 1 (issue #87): @allowed([1]) on maxReplicas makes the constraint
//   load-bearing at Bicep build time.
// - CAE storage binding lives here (not in storage.bicep) because the binding
//   is a child of the CAE. Co-locating them gives Bicep a symbol reference
//   (storageBinding.name) so the container app's volumes[] depends on the
//   binding automatically — no dependsOn needed, no ARM validation race.
//
// Role assignments:
// - mom-bot-gha (deploy pipeline) → Container Apps Contributor at RG scope
//   so the deploy workflow can call az containerapp update. Granted at RG scope
//   so any future Container Apps in the same RG are automatically covered.

@description('Azure region.')
param location string

@description('Name of the Container Apps Environment.')
param containerAppsEnvironmentName string

@description('Name of the Container App.')
param containerAppName string

@description('Resource ID of the user-assigned managed identity mi-mom-bot.')
param managedIdentityId string

@description('Client ID of the user-assigned managed identity — supplied to the container as AZURE_CLIENT_ID so ManagedIdentityCredential can select it via the SDK env-var convention.')
param managedIdentityClientId string

@description('Container image reference (ghcr.io/glitchwerks/mom-bot:<sha>).')
param containerImage string

@description('Name of the Key Vault (used to build env var KV_NAME).')
param keyVaultName string

@description('Object ID of the mom-bot-gha service principal for deploy-time Container Apps access.')
param ghaServicePrincipalObjectId string

@description('URI of the Key Vault (e.g. https://kv-mombot-eastus2.vault.azure.net/). Used for KV-backed secret references.')
param keyVaultUri string

@description('Name of the Storage Account backing the AzureFile volume (from storage.bicep output storageAccountName).')
param storageAccountName string

@description('Name of the Container Apps managed-environment storage binding to create on the CAE.')
param storageBindingName string = 'mom-bot-data-binding'

@description('Name of the Azure File Share to bind (must match the share created in storage.bicep).')
param fileShareName string = 'mom-bot-data'

@description('Policy 1 (issue #87): single-writer enforcement. @allowed([1]) makes maxReplicas > 1 a hard Bicep build error, preventing accidental multi-replica deployments that would corrupt the SQLite DB.')
@allowed([1])
param maxReplicas int = 1

// ---------------------------------------------------------------------------
// Built-in RBAC role definition IDs (stable; do not parameterize)
// ---------------------------------------------------------------------------

// Container Apps Contributor — allows create, update, delete on Container Apps and Environments.
var containerAppsContributorRoleId = '358470bc-b998-42bd-ab17-a7e34c199c0f'

// ---------------------------------------------------------------------------
// Container Apps Environment (Consumption profile)
// ---------------------------------------------------------------------------

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvironmentName
  location: location
  properties: {
    // Consumption-only: no workloadProfiles block → defaults to Consumption.
    zoneRedundant: false
  }
}

// ---------------------------------------------------------------------------
// Storage Account reference (existing — created by storage.bicep)
// ---------------------------------------------------------------------------

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName
  scope: resourceGroup()
}

// ---------------------------------------------------------------------------
// CAE storage binding — child of the CAE, must exist before the Container App
// references it in volumes[].storageName. Bicep derives the ordering
// automatically via the storageBinding.name symbol reference below.
// ---------------------------------------------------------------------------

resource storageBinding 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: cae
  name: storageBindingName
  properties: {
    azureFile: {
      accountName: storageAccount.name
      accountKey: storageAccount.listKeys().keys[0].value
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
}

// ---------------------------------------------------------------------------
// Container App
// ---------------------------------------------------------------------------

resource ca 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    environmentId: cae.id
    configuration: {
      // Ingress disabled — Discord bot is outbound-only for v1.0.
      // Epic 2.6 (role-sync sidecar) will re-enable ingress.
      // Policy 1 reinforcement (issue #87): no rolling overlap — old replica drains before new one starts.
      activeRevisionsMode: 'Single'
      secrets: [
        {
          name: 'database-url'
          keyVaultUrl: '${keyVaultUri}secrets/prod-database-url'
          identity: managedIdentityId
        }
      ]
    }
    template: {
      scale: {
        minReplicas: 0
        maxReplicas: maxReplicas
      }
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          // Symbol reference — Bicep sees the dependency on storageBinding and
          // ensures the binding is created before the container app is applied.
          storageName: storageBinding.name
        }
      ]
      containers: [
        {
          name: 'mom-bot'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'MOM_BOT_ENV'
              value: 'prod'
            }
            {
              name: 'MOM_BOT_KEY_VAULT_NAME'
              value: keyVaultName
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: managedIdentityClientId
            }
            {
              name: 'MOM_BOT_DATABASE_URL'
              secretRef: 'database-url'
            }
          ]
          volumeMounts: [
            {
              volumeName: 'data'
              mountPath: '/data'
            }
          ]
          // httpGet liveness probe — calls GET /healthz on port 8080.
          // The /healthz endpoint returns 200 when the reminder scheduler has
          // produced a heartbeat within the last 60 s, and 503 otherwise.
          // ACA does NOT support exec probes (ARM API rejects them at
          // deployment time despite the schema allowing the field — see #85).
          // Three consecutive 30 s failures (~90 s) trigger a restart.
          // initialDelaySeconds: 30 matches the cold-start grace period so
          // the probe does not fire before the scheduler has had a chance to
          // tick for the first time.
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8080
              }
              periodSeconds: 30
              failureThreshold: 3
              initialDelaySeconds: 30
            }
          ]
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Role assignment — mom-bot-gha: Container Apps Contributor at RG scope
// ---------------------------------------------------------------------------

resource roleAssignmentGHA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, ghaServicePrincipalObjectId, containerAppsContributorRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      containerAppsContributorRoleId
    )
    principalId: ghaServicePrincipalObjectId
    principalType: 'ServicePrincipal'
  }
}

@description('Fully qualified domain name of the Container App (empty when ingress disabled).')
output fqdn string = ca.properties.configuration.?ingress.?fqdn ?? ''
