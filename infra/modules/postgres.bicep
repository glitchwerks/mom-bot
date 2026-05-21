// postgres.bicep — Azure Database for PostgreSQL Flexible Server for mom-bot.
//
// Tier: Burstable B1ms (1 vCore, 2 GiB RAM, 640 max IOPS) per
//   https://learn.microsoft.com/en-us/azure/postgresql/compute-storage/concepts-compute
//   (fetched 2026-05-16). Adequate for a Discord bot's reminder/role tables.
//   Burstable is officially "for nonproduction" per the same doc — acceptable
//   risk here given the workload profile (idle most of the day, sub-second
//   bursts on reminder ticks). Revisit if we ever see CPU credit exhaustion
//   on the "CPU Credits Remaining" metric.
//
// Auth: Microsoft Entra ID only. passwordAuth = 'Disabled'. Entra admins
//   (mi-mom-bot + mom-bot-gha) are created post-deploy by
//   infra/scripts/create-entra-admins.sh to avoid the admin-race error on
//   initial provision. See issue #106.
//
// Networking: Public access + specific firewall rules. AllowAllAzureServices
//   (0.0.0.0) is NOT used — it admits all Azure tenant IPs (spike #101 §
//   Bonus Finding 4 / docs/spike/2026-05-17-postgres-aad-findings.md).
//   Container App outbound IPs are resolved at deploy time from
//   containerApp.outputs.outboundIpAddresses — one firewall rule per IP,
//   so all egress IPs are covered even if Azure assigns more than one (issue #120).
//   Operator ad-hoc access is NOT managed by Bicep — see infra/aad-runbook.md
//   "Dev-laptop ad-hoc Postgres access" for the runbook (issue #166).

@description('Azure region for the Postgres server.')
param location string

@description('Postgres server name (3-63 lowercase chars, must be globally unique within azure.postgres). Defaults to a deterministic derived name.')
@minLength(3)
@maxLength(63)
param serverName string = 'pg-mombot-${uniqueString(resourceGroup().id)}'

@description('Initial database name to create on the server.')
param databaseName string = 'mom_bot'

@description('Tenant ID for AAD admin assignment.')
param tenantId string

@description('Principal ID (object ID) of the user-assigned managed identity to set as Entra admin (mi-mom-bot).')
param managedIdentityPrincipalId string

@description('Display name of the UAMI (used as the Entra admin login name).')
param managedIdentityName string

@description('Outbound IP addresses of the Container App (ca-mom-bot). One firewall rule is created per IP so all egress IPs are covered even if Azure assigns more than one.')
param containerAppOutboundIps array

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    storage: {
      storageSizeGB: 32 // minimum per concepts-compute (fetched 2026-05-16)
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7 // valid range: 7-35 days per az CLI help; B1ms Burstable supports PITR
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenantId
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource db 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: pg
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Firewall: one rule per Container App outbound IP (issue #120 Part 2).
// Resolved at deploy time from containerApp.outputs.outboundIpAddresses so
// the rules stay correct if Azure assigns additional egress IPs in future.
@batchSize(1)
resource fwCaOutbound 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = [
  for (ip, index) in containerAppOutboundIps: {
    parent: pg
    name: 'allow-ca-egress-${index}'
    properties: {
      startIpAddress: ip
      endIpAddress: ip
    }
  }
]

// Entra admins (mi-mom-bot + mom-bot-gha) are NOT declared here. They are
// created post-deploy by infra/scripts/create-entra-admins.sh because the
// `Microsoft.DBforPostgreSQL/flexibleServers/administrators` resource races
// against the server's post-provision Updating window, producing
// `AadAuthOperationCannotBePerformedWhenServerIsNotAccessible` on initial
// deploy. See issue #106 for the full diagnosis and option comparison.

@description('Server name of the provisioned Postgres Flexible Server.')
output serverName string = pg.name

@description('Fully qualified domain name of the Postgres server.')
output fqdn string = pg.properties.fullyQualifiedDomainName

@description('Name of the initial database created on the server.')
output databaseName string = db.name
