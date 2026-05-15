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
//   DefaultAzureCredential unambiguously selects it for Key Vault access.
//   SystemAssigned was previously also attached but caused the SDK to default
//   to the empty SystemAssigned principal (no role assignments) → KV 403s.
//   See #81 for the diagnosis.
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
          ]
          // Exec liveness probe — confirms the reminder scheduler loop is
          // alive by checking the sentinel file at /tmp/scheduler-heartbeat.
          // Three consecutive 30 s failures (~90 s) trigger a restart.
          // initialDelaySeconds: 30 matches the cold-start grace period.
          // See plan § 8 for full spec.
          probes: [
            {
              type: 'Liveness'
              // BCP037: 'exec' is valid in the ARM API for ContainerAppProbe but
              // the Bicep type definition is missing it. Suppress until the type
              // is updated upstream (https://aka.ms/bicep-type-issues).
              #disable-next-line BCP037
              exec: {
                command: [
                  'python'
                  '-m'
                  'mom_bot.health.liveness'
                ]
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
