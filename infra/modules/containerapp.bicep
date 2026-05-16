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
//   See framework plan § Confirmed design decisions.
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
    }
    template: {
      scale: {
        minReplicas: 0
        maxReplicas: 1
      }
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
