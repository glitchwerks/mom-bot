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
//   Instead, pin operator IP and CAE static egress IP. Task 1.4 (CAE egress
//   firewall rule) is folded into this module per the merged plan.

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

@description('Operator egress IP address to whitelist in the firewall (single IP; update if the operator\'s IP changes).')
param operatorIpAddress string

@description('Static egress IP of the Container Apps Environment (cae-mom-bot-eastus2). Used to allow the bot to connect to Postgres. Retrieve with: az containerapp env show -n cae-mom-bot-eastus2 -g mom-bot --query properties.staticIp -o tsv')
param caeEgressIp string

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

// Firewall: operator IP only (not 0.0.0.0 — see networking decision above).
// GHA runner IPs are added transiently at deploy time and removed after migration.
// Update operatorIpAddress in main.bicepparam if the operator's egress changes.
resource fwOperator 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: pg
  name: 'operator-ip'
  properties: {
    startIpAddress: operatorIpAddress
    endIpAddress: operatorIpAddress
  }
}

// Firewall: CAE static egress — pinned here (Task 1.4 folded into Phase 1 module)
// so the rule is in place before any connection attempt at Phase 4 cutover.
resource fwCae 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: pg
  name: 'allow-cae-egress'
  properties: {
    startIpAddress: caeEgressIp
    endIpAddress: caeEgressIp
  }
}

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
