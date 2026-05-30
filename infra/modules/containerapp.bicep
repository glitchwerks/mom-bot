// containerapp.bicep — Container Apps Environment + Container App ca-mom-bot.
//
// Design choices:
// - Consumption profile only (no workload profiles) — cheapest tier; mom-bot's
//   workload is single-replica, tiny CPU/memory. WorkloadProfiles adds cost and
//   complexity that buys nothing here.
// - External public HTTPS ingress enabled (issue #76 A4, Epic #128 Phase 1). Target
//   port 8001 is the sidecar FastAPI port. IP allowlist restricts inbound to
//   siege-web prod CAE static egress (20.245.166.6/32, decision D-2). Bearer-token
//   auth on /api/internal/role-sync is the second layer of defence.
// - User-assigned MI only (mi-mom-bot) — attached as the sole identity so
//   ManagedIdentityCredential unambiguously selects it for Key Vault access.
//   SystemAssigned was previously also attached but caused the SDK to default
//   to the empty SystemAssigned principal (no role assignments) → KV 403s.
//   See #81 for the diagnosis. AZURE_CLIENT_ID is sourced from
//   mi-mom-bot.clientId and is required by ManagedIdentityCredential; the
//   ACA IMDS endpoint does not auto-select the sole UserAssigned identity.
// - Always-on single replica (minReplicas: 1, maxReplicas: 1). The two bounds
//   solve different problems and must not be conflated:
//   - maxReplicas: 1 keeps replica count bounded (retained from Policy 1, issue #87).
//     @allowed([1]) on maxReplicas makes this constraint load-bearing at Bicep build time.
//   - minReplicas: 1 keeps the Discord gateway WebSocket alive and the reminder
//     scheduler ticking — a Discord bot can't scale to zero. Without this, a new
//     revision provisions Healthy but never starts a replica, and the bot silently
//     goes offline (see issue #181).
//   Do not lower minReplicas to 0 to "save cost"; the bot stops working.
// - Postgres-backed (no AzureFile volume). The SQLite-on-AzureFile stopgap
//   (issue #87) was removed in #240 after the Postgres migration landed (#92).
//
// Role assignments:
// - (none managed by this module — GHA SP bootstrap roles are granted out-of-band
//   via the aad-runbook.md bootstrap steps, not in Bicep)

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

@description('URI of the Key Vault (e.g. https://kv-mombot-eastus2.vault.azure.net/). Used for KV-backed secret references.')
param keyVaultUri string

@description('Maximum replica count. Cap retained from the pre-Postgres era (Policy 1, issue #87); @allowed([1]) makes maxReplicas > 1 a hard Bicep build error at compile time.')
@allowed([1])
param maxReplicas int = 1

@description('Environment prefix used in the MOM_BOT_ENV env var and to derive KV secret names (e.g. \'prod\' → \'prod-database-url\'). Default \'prod\' preserves current single-env behavior.')
param momBotEnv string = 'prod'

@secure()
@description('Log Analytics workspace customer ID for CAE appLogsConfiguration.')
param logAnalyticsCustomerId string

@secure()
@description('Log Analytics workspace shared key for CAE appLogsConfiguration.')
param logAnalyticsSharedKey string

@secure()
@description('Application Insights connection string, exposed to mom-bot container as APPLICATIONINSIGHTS_CONNECTION_STRING.')
param appInsightsConnectionString string

// ---------------------------------------------------------------------------
// Container Apps Environment (Consumption profile)
// ---------------------------------------------------------------------------

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvironmentName
  location: location
  properties: {
    // Consumption-only: no workloadProfiles block → defaults to Consumption.
    zoneRedundant: false
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
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
      // External public HTTPS ingress — enabled for sidecar API (Epic #128 Phase 1, issue #76 A4).
      // Container Apps auto-provisions TLS when external=true; transport: 'http' is correct here
      // (the platform does TLS termination at the edge — specifying 'https' is wrong for this field).
      // IP allowlist restricts inbound to siege-web prod + dev CAE static egress IPs (D-2; mom-bot#76).
      // Single active revision: old replica drains before the new one starts, avoiding rolling overlap during deploys.
      ingress: {
        external: true
        targetPort: 8001
        transport: 'http'
        allowInsecure: false
        ipSecurityRestrictions: [
          {
            name: 'siege-web-prod-cae-egress'
            ipAddressRange: '20.245.166.6/32'
            action: 'Allow'
            description: 'siege-web prod Container Apps Environment static outbound IP (Epic #128 D-2)'
          }
          {
            name: 'siege-web-dev-cae-egress'
            ipAddressRange: '57.154.169.204/32'
            action: 'Allow'
            description: 'siege-web-api-dev Container Apps Environment static outbound IP (coord rsl-mom-apps#9, mom-bot#76)'
          }
        ]
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      activeRevisionsMode: 'Single'
      secrets: [
        {
          name: 'database-url'
          keyVaultUrl: '${keyVaultUri}secrets/${momBotEnv}-database-url'
          identity: managedIdentityId
        }
        {
          // Sourced directly from the AI Bicep resource (not KV) to avoid the
          // PLACEHOLDER drift documented in #199. The connection string is
          // injected at Bicep param resolution time.
          name: 'app-insights-connection-string'
          value: appInsightsConnectionString
        }
      ]
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: maxReplicas
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
              value: momBotEnv
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
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              secretRef: 'app-insights-connection-string'
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

@description('Fully qualified domain name of the Container App (empty when ingress disabled).')
output fqdn string = ca.properties.configuration.?ingress.?fqdn ?? ''

@description('Outbound IP addresses of the Container App. Used by postgres.bicep to build per-IP firewall rules.')
output outboundIpAddresses array = ca.properties.?outboundIpAddresses ?? []

@description('Resource ID of the Container Apps Environment. Used by sibling modules (e.g. migrations-job.bicep) that must share the same CAE.')
output caeId string = cae.id
