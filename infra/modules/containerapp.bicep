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
//   - maxReplicas: 1 enforces single-writer for SQLite + WAL (Policy 1, issue #87).
//     @allowed([1]) on maxReplicas makes this constraint load-bearing at Bicep build time.
//   - minReplicas: 1 keeps the Discord gateway WebSocket alive and the reminder
//     scheduler ticking — a Discord bot can't scale to zero. Without this, a new
//     revision provisions Healthy but never starts a replica, and the bot silently
//     goes offline (see issue #181).
//   Do not lower minReplicas to 0 to "save cost"; the bot stops working.
// - CAE storage binding lives here (not in storage.bicep) because the binding
//   is a child of the CAE. Co-locating them gives Bicep a symbol reference
//   (storageBinding.name) so the container app's volumes[] depends on the
//   binding automatically — no dependsOn needed, no ARM validation race.
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
      // External public HTTPS ingress — enabled for sidecar API (Epic #128 Phase 1, issue #76 A4).
      // Container Apps auto-provisions TLS when external=true; transport: 'http' is correct here
      // (the platform does TLS termination at the edge — specifying 'https' is wrong for this field).
      // IP allowlist restricts inbound to siege-web prod + dev CAE static egress IPs (D-2; mom-bot#76).
      // Policy 1 reinforcement (issue #87): no rolling overlap — old replica drains before new one starts.
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
          keyVaultUrl: '${keyVaultUri}secrets/prod-database-url'
          identity: managedIdentityId
        }
      ]
    }
    template: {
      scale: {
        minReplicas: 1
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

@description('Fully qualified domain name of the Container App (empty when ingress disabled).')
output fqdn string = ca.properties.configuration.?ingress.?fqdn ?? ''

@description('Outbound IP addresses of the Container App. Used by postgres.bicep to build per-IP firewall rules.')
output outboundIpAddresses array = ca.properties.?outboundIpAddresses ?? []
