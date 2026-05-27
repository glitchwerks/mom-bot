// observability.bicep — Log Analytics Workspace + Application Insights (workspace-based).
//
// Design choices:
// - PerGB2018 SKU: pay-per-GB ingestion with no per-node fee — appropriate for
//   a low-volume single-replica bot. Commitment tiers add cost for small workloads.
// - 30-day retention: minimum billable window; sufficient for mom-bot ops debugging.
//   Extend in a future PR if alert rules need longer lookback.
// - Workspace-based AI (IngestionMode: 'LogAnalytics'): the classic (non-workspace)
//   AI mode is deprecated and does not support Log Analytics query joins. Using
//   workspace-based mode from day one avoids a painful migration later.
// - Application_Type 'web': standard telemetry schema; correct for an HTTP-sidecar
//   process emitting availability/request/dependency telemetry.
// - Outputs marked @secure() where values are secrets (shared key, conn string).

@description('Azure region for all resources.')
param location string

@description('Base name used to derive resource names (e.g. mom-bot-eastus2 → log-mom-bot-eastus2).')
param baseName string

// ---------------------------------------------------------------------------
// Log Analytics Workspace
// ---------------------------------------------------------------------------

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${baseName}'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Application Insights (workspace-based)
// ---------------------------------------------------------------------------

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${baseName}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Log Analytics workspace customer ID (used by CAE appLogsConfiguration).')
output logAnalyticsCustomerId string = law.properties.customerId

@secure()
@description('Log Analytics workspace primary shared key (used by CAE appLogsConfiguration).')
output logAnalyticsSharedKey string = law.listKeys().primarySharedKey

@secure()
@description('Application Insights connection string — expose to mom-bot container as APPLICATIONINSIGHTS_CONNECTION_STRING.')
output appInsightsConnectionString string = appi.properties.ConnectionString

@description('Resource ID of the Log Analytics workspace (for downstream references).')
output logAnalyticsId string = law.id
