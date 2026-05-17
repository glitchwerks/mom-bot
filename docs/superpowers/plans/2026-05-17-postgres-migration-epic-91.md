# PostgreSQL Migration Epic (#91) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the failed SQLite-on-AzureFile stopgap with Azure Database for PostgreSQL Flexible Server as the durable persistence layer for mom-bot, in shippable increments with independent rollback per phase.

**Architecture:** A new Bicep module provisions a Burstable B1ms Postgres Flexible Server with **public endpoint + firewall (specific operator IPs + GHA runner CIDR ranges)** and **Microsoft Entra ID-only authentication**. The existing `mi-mom-bot` user-assigned managed identity is promoted to Entra admin on the server and used by the Container App to acquire AAD tokens as the Postgres password. Token TTL is ~86 minutes (observed in spike #101 — not the earlier ~24h assumption); the SQLAlchemy pool is configured with `pool_recycle=4800` to stay under that ceiling. Schema is applied by an `alembic upgrade head` step in the deploy workflow, not by the bot at startup. AzureFile storage is removed entirely. The single env-var `MOM_BOT_DATABASE_URL` shape is retained.

**Tech Stack:** Azure Database for PostgreSQL Flexible Server (Burstable B1ms), Bicep, `psycopg[binary]` 3.x, SQLAlchemy 2.x, Alembic, GitHub Actions OIDC, Azure Key Vault.

---

## 1. Goals & Non-Goals

### Goals (testable)

1. `alembic upgrade head` runs cleanly end-to-end against the provisioned Postgres instance from CI. Verifiable: green deploy run.
2. The bot starts, connects to Postgres via AAD token auth, and the reminder scheduler executes one tick without error. Verifiable: container logs show `reminder scheduler started` and at least one `select 1`-equivalent query against Postgres succeeds.
3. `infra/modules/storage.bicep` is removed from the repo and the storage account `stomombot*` is deleted from the `mom-bot` resource group. Verifiable: `git ls-tree main -- infra/modules/storage.bicep` returns empty (per `CLAUDE.md § Verify Artifact Persistence`); `az resource list -g mom-bot --resource-type Microsoft.Storage/storageAccounts` returns `[]`.
4. `run_migrations()` is no longer called in `MomBot.setup_hook` (`src/mom_bot/main.py:76-105` removed or marked unused). Verifiable: grep returns zero callers.
5. `MOM_BOT_DATABASE_URL` shape unchanged from the app's point of view — single env var, SQLAlchemy-compatible URL. Verifiable: `src/mom_bot/main.py:147` still reads the same variable name.

### Non-Goals

- High availability / zone-redundant Postgres (deferred — single-zone Burstable is adequate for a Discord bot per `concepts-compute.md` "Best suited for ... small databases"; cited below).
- VNet-injected ("private access") Postgres networking — rejected in § 2 Q1.
- Data migration from the previous SQLite database — the bot has been broken; there is no data to preserve (see § 5 Risk R1).
- Replacing `MOM_BOT_DATABASE_URL` with discrete `DB_HOST` / `DB_NAME` / etc. variables — rejected in § 2 Q4.
- Lifting the `maxReplicas` lock above 1 — out of scope; the operational complexity of a multi-replica Discord bot (sharding, presence, command dispatch) exceeds any benefit Postgres unlocks.
- Backup automation beyond Azure Postgres's built-in 7-day point-in-time-restore. Closes #90 as superseded — Postgres PITR replaces the bespoke AzureFile snapshot script.

---

## 2. Open Questions — Resolved

### Q1. Network model: public endpoint + firewall vs. private endpoint + VNet

**Decision: Public endpoint + firewall rules.** Specifically: whitelist the GitHub Actions runner CIDR ranges (published at `https://api.github.com/meta`, `actions` key) plus the operator's current egress IP. No `0.0.0.0` (any Azure tenant) rule. See also Charge 4 resolution and Bonus Finding 4 from spike #101.

**Reasoning:**
- The Container App today runs in the **default (non-VNet) Container Apps Environment** — see `infra/modules/containerapp.bicep:1-253` (no `vnetConfiguration` block). Switching to private Postgres would require:
  - Creating a VNet with at least two `/27` subnets (one for the CAE — workload-profiles minimum per [Container Apps networking](https://learn.microsoft.com/en-us/azure/container-apps/networking#environment-selection) (fetched 2026-05-16), one delegated to `Microsoft.DBforPostgreSQL/flexibleServers` minimum `/28` per [Postgres private networking](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private#virtual-network-concepts) (fetched 2026-05-16)),
  - Migrating the existing CAE to a workload-profiles VNet-injected environment (a destructive recreate — "Once you create an environment with either the default Azure network or an existing VNet, the network type can't be changed" per the same doc),
  - Creating + linking a Private DNS zone ending in `.postgres.database.azure.com`,
  - Adding NSG rules for outbound port 5432 + Microsoft Entra service-tag traffic.
- Cost and complexity: this is a non-trivial infra change touching #93 (network ACLs), well beyond the stated scope of #91.
- Public-endpoint + specific firewall rules + AAD-only auth + TLS gives a reasonable security profile: no password to leak, public network blocked except for enumerated source IPs, no extra cost.
- We are trading the storage-account ACL surface (closed by removing AzureFile) for a Postgres-firewall surface that is bounded to GHA runner ranges + operator IPs. This is materially narrower than the prior `AllowAllAzureServices` posture and is acceptable security mitigation for the AAD-only auth wall. Private endpoint remains the long-term answer when CAE network mode is changed (separate work).
- Issue #93 (networkAcls) can be addressed against the Postgres firewall surface in a separate later epic without blocking this work.

**Citation:** [Postgres private networking concepts](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private) (fetched 2026-05-16) §§ "Virtual network concepts", "Unsupported virtual network scenarios" — confirms the subnet delegation requirement, `/28` minimum, and the irreversible CAE network choice. `infra/modules/containerapp.bicep:1-253` (current state — no `vnetConfiguration`). Spike #101: `docs/spike/2026-05-17-postgres-aad-findings.md` § Bonus Finding 4 confirms `0.0.0.0` semantics (any Azure tenant, not operator-only).

### Q2. Auth mode: AAD-token auth vs. password in KV

**Decision: Microsoft Entra ID authentication only (no password in KV).** Concretely:
- Provision the server with `authConfig.passwordAuth = 'Disabled'` and `authConfig.activeDirectoryAuth = 'Enabled'`.
- Assign the user-assigned managed identity `mi-mom-bot` (created in `infra/modules/managed-identity.bicep`) as the **Entra admin** on the server via the `administrators` child resource (`Microsoft.DBforPostgreSQL/flexibleServers/administrators`).
- The bot acquires an AAD token for audience `https://ossrdbms-aad.database.windows.net` via `ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)` (the same pattern already used in `src/mom_bot/secrets.py` per PR #84/#86) and passes the token as the Postgres password on each connection.
- The KV secret `prod-database-url` becomes a **passwordless** DSN of the form `postgresql+psycopg://mi-mom-bot@<server>.postgres.database.azure.com/mom_bot?sslmode=require`. The password is injected at connect-time by a SQLAlchemy `do_connect` event handler that fetches a fresh token. Observed token TTL is **~86 minutes** (spike #101, `docs/spike/2026-05-17-postgres-aad-findings.md` § Charge 3) — not the ~24h cited in the Entra concepts FAQ, which is the upper bound for user tokens. A `pool_recycle=4800` (80 min) is required to force new physical connections before token expiry (see Phase 3 and Risk R2).

**Reasoning:**
- This is the established pattern in this repo. The same `mi-mom-bot` UAMI already auths to Key Vault via `ManagedIdentityCredential` (PR #84 commit `8b0e10a` — "pass AZURE_CLIENT_ID to ManagedIdentityCredential"). Reusing that identity for Postgres means **zero new secrets to rotate**, **zero new principals**.
- AAD admin can be a user-assigned managed identity directly per [Entra concepts](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts) (fetched 2026-05-16) §§ "Differences between a PostgreSQL administrator and a Microsoft Entra administrator" ("The Microsoft Entra administrator can be a Microsoft Entra user, Microsoft Entra group, service principal, or managed identity").
- Password-in-KV would add a rotation burden, a leak surface, and a secret to manage in `infra/aad-runbook.md` — for no functional gain.

**Trade-off / known sharp edge:** Alembic CLI run from the GHA runner also needs a token. The GHA service principal (`mom-bot-gha`) must also be added as an Entra admin (multiple Entra admins are supported per the same FAQ: "you can set as many Microsoft Entra administrators as you want"). The deploy workflow uses `az account get-access-token --resource-type oss-rdbms` to mint the token and injects it as `PGPASSWORD`. Verified working end-to-end against a real Flexible Server in spike #101 (`docs/spike/2026-05-17-postgres-aad-findings.md` § Charge 2).

**Citation:** [Microsoft Entra Authentication for PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts) (fetched 2026-05-16). PR #84, PR #86 (`mi-mom-bot` + `AZURE_CLIENT_ID` pattern). Spike #101 `docs/spike/2026-05-17-postgres-aad-findings.md` § Charge 2 (end-to-end verification), § Charge 3 (86-min TTL measurement).

### Q3. Data migration approach: drain-and-cutover vs. dual-write

**Decision: No migration. Schema-only cutover.**

**Reasoning:** The bot has been failing on first write since the SQLite-on-AzureFile attempt (issue #91 status section confirms "first write hangs indefinitely on fsync over SMB"). There is no production data to preserve. The reminders table is repopulated on bot start by the seed function (`src/mom_bot/reminders/seed.py:225-311` — idempotent on empty DB). The `member_role_sync_state` table accumulates per-member idempotency state that is regenerated naturally as members are re-synced. The `day_role_map` table is seeded by `src/mom_bot/roles/seed.py`.

No dual-write infrastructure, no migration script, no cutover dance. **Skip the question entirely.**

**Verification step before declaring "no data to migrate":** in Phase 1 Task 1.3 (moved from Phase 4), the operator must run an `az storage file list` against the existing `mom-bot-data` share to confirm there is no `.db` file with non-trivial content. If a populated `.db` file is present, halt and convert this section to a real data-migration plan.

### Q4. Env-var shape: retain `MOM_BOT_DATABASE_URL` or split into discrete vars

**Decision: Retain `MOM_BOT_DATABASE_URL`.**

**Reasoning:**
- `src/mom_bot/main.py:147` and `migrations/env.py:8,52,95` both consume this single variable directly — splitting it adds parsing code with no benefit.
- The variable is referenced by name in `infra/aad-runbook.md:278`, `docs/secrets-inventory.md:32`, `README.md`, and several test fixtures (`tests/test_main_wireup.py` et al.). Each is a doc/test churn cost with zero functional payoff.
- Discrete vars would still need to be reassembled into a SQLAlchemy URL string before `create_engine()`. The reassembly logic is exactly what we'd remove — replacing one DSN env-var with N env-vars and a `urlencode` helper is a net code increase.
- The AAD-token-as-password injection happens via a SQLAlchemy `do_connect` event hook regardless of env-var shape, so the auth design is orthogonal to this choice.

**Citation:** `src/mom_bot/main.py:147`, `migrations/env.py:52`.

---

## 3. File Structure

### New files

- `infra/modules/postgres.bicep` — Postgres Flexible Server, firewall rules, AAD admin assignment.
- `src/mom_bot/db.py` — SQLAlchemy engine factory with AAD-token `do_connect` event hook and `pool_recycle=4800`. **New module** — extracts `_build_session_factory` from `main.py` so the token-injection logic lives separately and is unit-testable. (Engine factory is fewer than 60 LOC; this is a focused responsibility split, not bloat.)
- `tests/test_db_token_injection.py` — verifies the AAD-token hook is invoked on connect and stamps `connection.password` from the credential.
- `tests/test_alembic_postgres.py` — runs `alembic upgrade head` against a real Postgres instance (via `testcontainers-python` or GitHub Actions `services: postgres:16`) to catch dialect-specific DDL failures before they reach production.

### Modified files

- `infra/main.bicep` — instantiate `postgres` module; remove `storage` module instantiation; pass Postgres FQDN to `containerapp.bicep`.
- `infra/main.bicepparam` — add Postgres admin object IDs (UAMI + GHA SP).
- `infra/modules/containerapp.bicep` — strip storage binding (lines 120-131), volumes (166-173), volumeMounts (201-205); update `database-url` secret reference (KV secret already exists; only the value changes); remove `COPY alembic.ini` and `COPY migrations/` from Dockerfile (Alembic runs only in CI — see Phase 3 deliverable).
- `infra/aad-runbook.md` — replace the SQLite-on-SMB policy section with the Postgres Entra-admin runbook step; update `prod-database-url` example. Note guest-UPN URL-encoding requirement for operator probe commands.
- `src/mom_bot/main.py` — replace `_build_session_factory` with import from new `db` module; remove `run_migrations()` (lines 76-105) and its call site in `setup_hook` (line 206); remove the alembic imports at lines 51-52 (`from alembic.command import upgrade as alembic_upgrade`, `from alembic.config import Config as AlembicConfig`); remove `_ALEMBIC_INI` constant (line 73).
- `pyproject.toml` — add `psycopg[binary]>=3.2,<4`, pin `sqlalchemy>=2,<3`, pin `alembic>=1.13,<2` to `dependencies`; materialize `uv.lock` into the image (see dep-pinning decision below).
- `migrations/versions/0002_reminders_schema.py` — **rewrite the `ck_fire_time_no_seconds` CHECK constraint** to use `EXTRACT(SECOND FROM fire_time_utc) = 0` (dialect-portable: works on both SQLite ≥ 3.38 and Postgres). This is a destructive edit to a committed migration — acceptable because #91 is explicitly fresh-Postgres-no-data-migration. See Phase 2 for rationale.
- `.github/workflows/deploy.yml` — add steps: install Python+`uv`+`psycopg[binary]`+`alembic`, mint AAD token via `az account get-access-token --resource-type oss-rdbms`, add transient firewall rule for runner IP, run `alembic upgrade head`, remove firewall rule. Pin `az` CLI ≥ 2.86 in the runner prereq check.
- `README.md` — update the Epic 0 / Alembic section to reflect Postgres prod + SQLite local-dev.
- `docs/secrets-inventory.md` — update `prod-database-url` description (passwordless DSN, not SQLite path).
- `Dockerfile` — remove `COPY alembic.ini ./` and `COPY migrations/ ./migrations/` lines (lines 11-12); switch `pip install` to `uv sync --frozen --no-dev` after adding `COPY uv.lock` (dep-pinning Option A).

### Files deleted

- `infra/modules/storage.bicep` — entire file.
- `migrations/versions/0003_postgres_check_constraint_portability.py` — **not created** (replaced by the in-place rewrite of 0002; see Phase 2 pivot).

---

## 4. Phases & Tasks

Each phase produces a separately-mergeable PR. Each phase has a rollback path that does not require touching the prior phase.

---

### Phase 1 — Provision Postgres (additive, dark)

**Goal:** Postgres Flexible Server exists in the `mom-bot` resource group, with firewall + AAD admin configured. Nothing connects to it yet.
**Entry criteria:** PR for this plan is merged. Branch off `main`.
**Exit criteria:** `az postgres flexible-server show -g mom-bot -n <name>` returns `state: Ready`. `az postgres flexible-server execute -n <name> --admin-user <uami-client-id> --querytext "select 1"` succeeds from a developer laptop (with token).
**Rollback:** `az resource delete` the Postgres server. Nothing downstream depends on it yet.

#### Phase 1 prerequisites

Before any Phase 1 tasks begin, verify:

- [ ] **az CLI ≥ 2.86**: run `az version --query '"azure-cli"' -o tsv` and confirm the result is `2.86.0` or later. The `--microsoft-entra-auth` flag on `az postgres flexible-server create` and `update` was not available in 2.84; running against 2.84 produces "unrecognized arguments" and does not expose `--active-directory-auth` either. Source: spike #101, `docs/spike/2026-05-17-postgres-aad-findings.md` § Bonus Finding 3.

- [ ] **Microsoft.Graph provider** (R7): `az provider show -n Microsoft.Graph --query registrationState -o tsv` returns an `InvalidResourceNamespace` error on this subscription — Microsoft.Graph is not a registerable Azure resource provider (verified 2026-05-17 against sub `213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0`). The `administrators` child resource on Postgres Flexible Server does **not** require the Microsoft.Graph provider. R7 is **RESOLVED — not applicable**. No registration step needed.

- [ ] **Confirm no SQLite data exists worth preserving** (moved from Phase 4, Task 4.1): run the verification in Task 1.3 below before spending time on provisioning.

#### Task 1.1: Verify no SQLite data exists worth preserving

**Files:** (read-only verification, no changes)

- [ ] **Step 1: Inspect the AzureFile share**

```powershell
$key = az storage account keys list -g mom-bot --account-name stomombotXXXXXX --query "[0].value" -o tsv
az storage file list `
  --account-name stomombotXXXXXX `
  --account-key $key `
  --share-name mom-bot-data `
  --output table
```
Expected: empty, or a `mom_bot.db` of essentially-zero size (no successful writes ever happened per #91 status).

- [ ] **Step 2: HALT condition — if any file with size > 1 KiB exists**, stop the cutover and convert § Q3 into a real data-migration sub-plan (download, replay rows into Postgres). Do NOT proceed silently.

#### Task 1.2: Author `postgres.bicep`

**Files:**
- Create: `infra/modules/postgres.bicep`

- [ ] **Step 1: Create the Postgres module file**

```bicep
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
// Auth: Microsoft Entra ID only. passwordAuth = 'Disabled'. The user-assigned
//   managed identity mi-mom-bot is set as the Entra admin (it is the runtime
//   principal — bot connects via token). The GHA service principal mom-bot-gha
//   is also added as Entra admin so the deploy workflow can run
//   `alembic upgrade head`. Multiple Entra admins are supported per
//   https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts
//   (fetched 2026-05-16).
//
// Networking: Public access + specific firewall rules. AllowAllAzureServices
//   (0.0.0.0) is NOT used — it admits all Azure tenant IPs (spike #101 §
//   Bonus Finding 4 / docs/spike/2026-05-17-postgres-aad-findings.md).
//   Instead, pin GHA runner CIDR ranges + operator IP(s). See also Charge 4
//   resolution.

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

@description('Principal ID of the GHA service principal to also set as Entra admin (for alembic upgrade from CI).')
param ghaServicePrincipalObjectId string

@description('Display name of the GHA SP.')
param ghaServicePrincipalName string = 'mom-bot-gha'

@description('Operator egress IP address to whitelist in the firewall (single IP; update if the operator\'s IP changes).')
param operatorIpAddress string

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
      backupRetentionDays: 7   // valid range: 7–35 days per az CLI help; B1ms Burstable supports PITR
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

// Firewall: operator IP only (not 0.0.0.0 — see networking decision in Q1).
// GHA runner IPs are added transiently at deploy time (deploy.yml step
// "Add transient firewall rule for runner IP") and removed after migration.
// Update operatorIpAddress in main.bicepparam if the operator's egress changes.
resource fwOperator 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: pg
  name: 'operator-ip'
  properties: {
    startIpAddress: operatorIpAddress
    endIpAddress: operatorIpAddress
  }
}

// Entra admin: mi-mom-bot (runtime).
resource adminUami 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: pg
  name: managedIdentityPrincipalId
  properties: {
    principalType: 'ServicePrincipal'
    principalName: managedIdentityName
    tenantId: tenantId
  }
}

// Entra admin: mom-bot-gha (alembic upgrade from CI).
resource adminGha 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: pg
  name: ghaServicePrincipalObjectId
  properties: {
    principalType: 'ServicePrincipal'
    principalName: ghaServicePrincipalName
    tenantId: tenantId
  }
}

output serverName string = pg.name
output fqdn string = pg.properties.fullyQualifiedDomainName
output databaseName string = db.name
```

- [ ] **Step 2: Local validation**

```powershell
az bicep build --file infra\modules\postgres.bicep
```
Expected: zero errors, zero warnings (lint may flag the `@maxLength(63)` on the name — acceptable; Postgres FQDN component limit is 63).

- [ ] **Step 3: Commit**

```bash
git add infra/modules/postgres.bicep
git commit -m "feat(infra): add postgres.bicep module (Burstable B1ms, AAD-only) (#91)"
```

#### Task 1.3: Wire `postgres` module into `main.bicep` (provision-only, no consumers yet)

**Files:**
- Modify: `infra/main.bicep`
- Modify: `infra/main.bicepparam`

- [ ] **Step 1: Add module instantiation in `main.bicep`** — insert after the `kv` module block, before the `storage` module block:

```bicep
// ---------------------------------------------------------------------------
// PostgreSQL (replaces AzureFile + SQLite stopgap — issue #91)
// ---------------------------------------------------------------------------

@description('Tenant ID — needed for Postgres AAD admin configuration.')
param tenantId string = subscription().tenantId

@description('Operator egress IP for Postgres firewall whitelist. Update if operator IP changes.')
param operatorIpAddress string

module postgres 'modules/postgres.bicep' = {
  name: 'deploy-postgres'
  scope: rg
  params: {
    location: location
    tenantId: tenantId
    managedIdentityPrincipalId: identity.outputs.principalId
    managedIdentityName: managedIdentityName
    ghaServicePrincipalObjectId: ghaServicePrincipalObjectId
    operatorIpAddress: operatorIpAddress
  }
}
```

(The `storage` module and the `containerApp` wiring stay untouched in this phase.)

- [ ] **Step 2: What-if preview**

```powershell
az deployment sub what-if `
  --location eastus2 `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam `
  --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0
```
Expected output: net-new creation of one `flexibleServers`, one `databases`, one `firewallRules`, two `administrators`. Storage, KV, MI, ContainerApp shown as `=` (no change).

- [ ] **Step 3: Apply**

```powershell
az deployment sub create `
  --location eastus2 `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam `
  --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0
```
Expected: deployment succeeds in 5–10 minutes (Postgres provisioning is the long pole).

- [ ] **Step 4: Smoke-test from operator laptop**

```powershell
$token = az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv
$env:PGPASSWORD = $token
$fqdn = az postgres flexible-server show -g mom-bot --name pg-mombot-XXXXXX --query fullyQualifiedDomainName -o tsv
psql "host=$fqdn port=5432 dbname=mom_bot user=<your-aad-upn> sslmode=require" -c "select version();"
```
Expected: prints `PostgreSQL 16.x`. **Note:** if the operator's UPN contains `#EXT#@` (guest account), URL-encode the user component: `urllib.parse.quote(user, safe="")`. Production runtime is not affected (the UAMI `clientId` is a UUID). Source: spike #101, `docs/spike/2026-05-17-postgres-aad-findings.md` § Bonus Finding 2.

**Note:** the operator's AAD account must also be added as an Entra admin for this manual smoke test (one-time `az postgres flexible-server ad-admin create ...`); the Bicep module only adds the UAMI and GHA SP.

**AAD admin propagation:** spike #101 observed <60 s end-to-end latency after `az postgres flexible-server ad-admin set` before the first probe succeeded. If the smoke test returns "pg_hba.conf rejects connection" immediately after provisioning, wait 60 s and retry. Source: `docs/spike/2026-05-17-postgres-aad-findings.md` § Bonus Finding 5.

- [ ] **Step 5: PR and commit**

```bash
git add infra/main.bicep infra/main.bicepparam
git commit -m "feat(infra): provision Postgres Flexible Server (dark — no consumers yet) (#91)"
git push -u origin <branch>
gh pr create --draft --title "feat(infra): provision Postgres (Phase 1 of #91)" --body-file <body>
```

**Acceptance criteria for Phase 1:**
- [ ] `az postgres flexible-server show` returns `state: Ready`.
- [ ] Operator can `psql` with AAD token (proves auth works end-to-end).
- [ ] CAE, KV, MI, Container App, storage unchanged (verified by what-if `=` lines).
- [ ] `az storage file list` on `mom-bot-data` share shows empty / zero-size `.db` file.

---

### Phase 2 — Schema portability (validate Alembic against Postgres)

**Goal:** `alembic upgrade head` runs cleanly against the new Postgres instance from an operator laptop. The strftime CHECK constraint bug in `0002_reminders_schema.py` is fixed **by rewriting 0002 in place** (not by a new 0003 migration). A `tests/test_alembic_postgres.py` test file is added as a required deliverable.

**Entry criteria:** Phase 1 merged. Operator has token-based psql access.
**Exit criteria:** `alembic upgrade head` against Postgres returns success and `\dt` shows `reminders`, `reminder_sent`, `day_role_map`, `member_role_sync_state`, `alembic_version`. `pytest tests/test_alembic.py -v` still passes against SQLite. `pytest tests/test_alembic_postgres.py -v` passes against a containerized Postgres.
**Rollback:** Drop the public schema (`drop schema public cascade; create schema public;`) and re-run.

#### Phase 2 design pivot — rewrite 0002 in place (not a new 0003 migration)

The spike (`docs/spike/2026-05-17-postgres-aad-findings.md` § Charge 5) proved that `0002_reminders_schema.py` fails on Postgres before `0003` can run — `0003` depends on `0002` being in a committed state, but `0002` dies at the CHECK constraint DDL. Two paths exist:

1. **Rewrite inside 0002** (this path): change the CHECK expression in `0002` to use `EXTRACT(SECOND FROM fire_time_utc) = 0` (dialect-portable). Since #91 targets a fresh Postgres database with no data migration, this is the clean path — it eliminates the broken migration from history entirely.
2. **Drop-and-recreate in 0003**: leave `0002` broken as-is and add a `0003` that drops and recreates the constraint. Required only for existing SQLite databases being migrated forward — which #91 explicitly does not require.

**Decision: take path 1.** The existing Phase 2 Task 2.1 that authored `0003_postgres_check_constraint_portability.py` is replaced by the in-place 0002 edit below. The `0003` file should **not** be created.

The `EXTRACT(SECOND FROM fire_time_utc) = 0` expression is dialect-portable: it works on Postgres natively and on SQLite ≥ 3.38 (released 2022-02). If minimum SQLite version in the test matrix is below 3.38, add a dialect-branch fallback in the migration; otherwise the single expression covers both paths.

#### Task 2.1: Edit `0002_reminders_schema.py` in place

**Files:**
- Modify: `migrations/versions/0002_reminders_schema.py`

- [ ] **Step 1: Replace the strftime CHECK**

Locate the `sa.CheckConstraint(...)` call for `ck_fire_time_no_seconds` in `migrations/versions/0002_reminders_schema.py` (currently lines ~65-68 of the upgrade function, reading `"CAST(strftime('%S', fire_time_utc) AS INTEGER) = 0"`). Replace it with:

```python
sa.CheckConstraint(
    "EXTRACT(SECOND FROM fire_time_utc) = 0",
    name="ck_fire_time_no_seconds",
),
```

This change is a destructive edit to a committed migration. It is acceptable because:
- #91 is explicitly a fresh-Postgres-no-data-migration epic.
- There is no production SQLite database with applied migrations (the bot has never written successfully — issue #91 status).
- The `EXTRACT(SECOND FROM ...)` syntax is accepted by SQLite ≥ 3.38 (released 2022-02-22). Confirm minimum SQLite version in the CI test matrix or add a dialect-branch if needed.

**Note on the old 0003 plan:** the prior Task 2.1 in this plan created `migrations/versions/0003_postgres_check_constraint_portability.py` with a dialect-branched drop-and-recreate. Do not create that file. The `tests/test_alembic.py` assertion change at line ~64 (from `ck_fire_time_no_seconds_v2` back to `ck_fire_time_no_seconds`) is no longer needed — the constraint name is unchanged.

- [ ] **Step 2: Run SQLite-side tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_alembic.py -v
```
Expected: PASS — the edited migration runs against SQLite using `EXTRACT()`.

- [ ] **Step 3: Run Postgres-side migration from operator laptop**

```powershell
$token = az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv
$env:PGPASSWORD = $token
$fqdn = az postgres flexible-server show -g mom-bot --name pg-mombot-XXXXXX --query fullyQualifiedDomainName -o tsv
$env:MOM_BOT_DATABASE_URL = "postgresql+psycopg://<your-aad-upn>@${fqdn}:5432/mom_bot?sslmode=require"
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Note the `postgresql+psycopg://` scheme — **not** bare `postgresql://`. SQLAlchemy defaults bare `postgresql://` to the psycopg2 dialect; only psycopg3 (`psycopg`) is installed. Using the wrong scheme raises `ModuleNotFoundError: No module named 'psycopg2'` at engine-creation time. Source: spike #101, `docs/spike/2026-05-17-postgres-aad-findings.md` § Bonus Finding 1.

Expected: each revision prints `Running upgrade ...` and exits 0. **First**, install `psycopg[binary]` — Task 2.2 below adds it to `pyproject.toml`.

- [ ] **Step 4: Verify schema**

```powershell
psql "host=$fqdn port=5432 dbname=mom_bot user=<your-aad-upn> sslmode=require" -c "\dt"
```
Expected: lists `alembic_version`, `day_role_map`, `member_role_sync_state`, `reminder_sent`, `reminders`.

#### Task 2.2: Add `psycopg[binary]` dependency and pin DB deps

**Files:**
- Modify: `pyproject.toml:10-20`

- [ ] **Step 1: Add and pin DB dependencies**

```toml
dependencies = [
    "discord.py>=2.4",
    "aiohttp>=3.9",
    "pydantic>=2",
    "sqlalchemy>=2,<3",
    "alembic>=1.13,<2",
    "azure-identity>=1.17",
    "azure-keyvault-secrets>=4.8",
    "fastapi>=0.111,<1.0",
    "httpx>=0.27,<1.0",
    "psycopg[binary]>=3.2,<4",
]
```

Upper bounds on the three DB deps (`sqlalchemy<3`, `alembic<2`, `psycopg<4`) protect against breaking major-version changes. After the post-SMB incident, explicit pinning is cheap insurance. Source: inquisitor self-review Charge 7.

- [ ] **Step 2: Regenerate the lock file**

```powershell
uv lock
```
This updates `uv.lock` to reflect the new dep set.

- [ ] **Step 3: Reinstall in venv**

```powershell
uv pip install -e ".[dev]"
```

- [ ] **Step 4: Run full test suite to verify no regressions**

```powershell
.\.venv\Scripts\python.exe -m pytest
```
Expected: all existing tests PASS.

#### Task 2.3: Add `tests/test_alembic_postgres.py`

This test fixture is a required Phase 2 deliverable, not deferred. Spike #101 proved that `test_alembic.py` (SQLite-only) is insufficient — the `strftime()` failure went undetected until the spike ran against real Postgres. Without a Postgres-targeted test, every future migration is at risk of the same class of failure.

**Files:**
- Create: `tests/test_alembic_postgres.py`

- [ ] **Step 1: Write the test using testcontainers-python**

```python
"""Alembic upgrade-head test against a real Postgres instance.

Uses testcontainers-python to spin up a Postgres 16 container; verifies
that ``alembic upgrade head`` runs cleanly and all expected tables exist.
This catches dialect-specific DDL failures (SQLite-isms) that the SQLite
test suite in test_alembic.py cannot detect.

Requires the ``testcontainers[postgres]`` extra in dev dependencies.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

pytest.importorskip("testcontainers", reason="testcontainers-python not installed")

from testcontainers.postgres import PostgresContainer  # noqa: E402


@pytest.fixture(scope="module")
def postgres_url() -> str:
    """Spin up a throwaway Postgres 16 container and return its URL."""
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "psycopg")


def test_alembic_upgrade_head_postgres(postgres_url: str) -> None:
    """alembic upgrade head must succeed against Postgres without errors."""
    os.environ["MOM_BOT_DATABASE_URL"] = postgres_url
    cfg = AlembicConfig("alembic.ini")
    alembic_command.upgrade(cfg, "head")

    engine = sa.create_engine(postgres_url)
    with engine.connect() as conn:
        result = conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        )
        tables = {row[0] for row in result}

    expected = {"alembic_version", "day_role_map", "member_role_sync_state", "reminder_sent", "reminders"}
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"
```

- [ ] **Step 2: Add `testcontainers[postgres]` to dev dependencies in `pyproject.toml`**

Add to the `[project.optional-dependencies]` `dev` list:
```toml
"testcontainers[postgres]>=4.7",
```

Alternatively, configure a GitHub Actions `services: postgres:16` block in the CI workflow and have the test read `DATABASE_URL` from the environment. Either approach satisfies the requirement; testcontainers is simpler for local dev.

- [ ] **Step 3: Commit and open Phase 2 draft PR**

```bash
git add migrations/versions/0002_reminders_schema.py tests/test_alembic.py pyproject.toml uv.lock tests/test_alembic_postgres.py
git commit -m "feat(db): rewrite 0002 CHECK constraint for Postgres + add postgres alembic test (#91)"
git push
gh pr create --draft --title "feat(db): schema portability for Postgres (Phase 2 of #91)" --body-file <body>
```

**Acceptance criteria for Phase 2:**
- [ ] `pytest tests/test_alembic.py` passes (SQLite path).
- [ ] `pytest tests/test_alembic_postgres.py` passes (Postgres path via testcontainers or CI service).
- [ ] `alembic upgrade head` runs cleanly against the live Postgres instance (operator laptop run).
- [ ] `\dt` shows all four app tables + `alembic_version`.

---

### Phase 3 — Application wiring (AAD-token engine, remove startup migrations)

**Goal:** The bot's SQLAlchemy engine acquires an AAD token on connect with `pool_recycle=4800`; `run_migrations()` is removed from `setup_hook`; `Dockerfile` drops the `alembic.ini` and `migrations/` COPY lines. The bot does not yet point at Postgres in prod — that's Phase 4.
**Entry criteria:** Phase 2 merged.
**Exit criteria:** Local `pytest` passes; new `tests/test_db_token_injection.py` verifies the token hook fires; `MomBot.setup_hook` no longer calls `run_migrations`.
**Rollback:** Revert the PR. Local dev path (SQLite, no token) must still work — the token hook must be a no-op when the DSN scheme is `sqlite://`.

#### Phase 3 reconciliation — dependency on PR #95

PR #95 (`fix(db): auto-run alembic upgrade head at bot startup`, commit `de9b692`) merged on 2026-05-17, closing issue #94. That PR added `run_migrations()` as a startup-time migration call. Phase 3 of this plan removes what #95 introduced. The dependency chain is:

> spike #101 findings → this plan revision → PR #95 already merged to `main`

Artifacts introduced by PR #95 that Phase 3 will remove from `src/mom_bot/main.py`:
- `run_migrations()` function body: lines 76-105
- Alembic imports: `from alembic.command import upgrade as alembic_upgrade` (line 51), `from alembic.config import Config as AlembicConfig` (line 52)
- `_ALEMBIC_INI` constant: line 73
- Call site in `setup_hook`: line 206 (`run_migrations()`)
- Test fixture `mock_run_migrations` in `tests/test_main_wireup.py` (lines ~87-105): patches `mom_bot.main.run_migrations`. When `run_migrations` is removed from `main.py`, this fixture becomes dead. Remove it and any test that asserts on the mock. Note: the fixture suppresses a `fileConfig` side-effect that disables loggers; once the function is gone, this suppression is no longer needed.

Issue #94 is **already closed** (closed by PR #95 on 2026-05-17). References to "Closes #94" in this plan have been updated to "References #94 (closed by PR #95, 2026-05-17)".

#### Task 3.1: Create `src/mom_bot/db.py` with token-injection engine factory

**Files:**
- Create: `src/mom_bot/db.py`
- Create: `tests/test_db_token_injection.py`

- [ ] **Step 1: Write the failing test first**

```python
# tests/test_db_token_injection.py
"""AAD-token injection for Postgres connections."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event

from mom_bot.db import build_session_factory


def test_sqlite_url_does_not_invoke_token_hook() -> None:
    """Local-dev SQLite path must NOT acquire AAD tokens."""
    with patch("mom_bot.db.ManagedIdentityCredential") as mic:
        factory = build_session_factory("sqlite:///:memory:")
        # Open a session to actually establish a connection.
        with factory() as s:
            s.execute(__import__("sqlalchemy").text("select 1"))
        mic.assert_not_called()


def test_postgres_url_injects_token_as_password() -> None:
    """Postgres path must call ManagedIdentityCredential.get_token and stamp the password."""
    fake_token = MagicMock(token="FAKE-AAD-TOKEN-abc")
    with (
        patch("mom_bot.db.ManagedIdentityCredential") as mic_cls,
        patch("mom_bot.db.create_engine") as ce,
    ):
        mic_cls.return_value.get_token.return_value = fake_token
        engine = MagicMock()
        ce.return_value = engine
        # Capture the do_connect listener.
        listeners: list = []
        engine.dispatch = MagicMock()

        def fake_listen(target, name, fn):
            listeners.append((name, fn))

        with patch("mom_bot.db.event.listens_for") as lf:
            lf.side_effect = lambda *a, **kw: (lambda f: (listeners.append(("do_connect", f)), f)[1])
            build_session_factory(
                "postgresql+psycopg://mi-mom-bot@srv.postgres.database.azure.com/mom_bot?sslmode=require",
                aad_client_id="11111111-2222-3333-4444-555555555555",
            )
        # Invoke the captured do_connect listener with a stub cparams dict.
        do_connect = next(fn for name, fn in listeners if name == "do_connect")
        cparams: dict[str, object] = {}
        do_connect(dialect=None, conn_rec=None, cargs=(), cparams=cparams)
        assert cparams["password"] == "FAKE-AAD-TOKEN-abc"
        mic_cls.return_value.get_token.assert_called_once_with(
            "https://ossrdbms-aad.database.windows.net/.default"
        )
```

- [ ] **Step 2: Run the test, verify it fails**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_db_token_injection.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'mom_bot.db'`.

- [ ] **Step 3: Implement `src/mom_bot/db.py`**

```python
"""SQLAlchemy engine + session factory with AAD-token injection for Postgres.

For Postgres URLs, an AAD access token (audience
``https://ossrdbms-aad.database.windows.net/.default``) is acquired from the
configured user-assigned managed identity on every new physical connection and
stamped as the ``password`` connect parameter.

Token TTL observed in spike #101 is ~86 minutes (5147 s), not the ~24h upper
bound cited in the Entra concepts FAQ (docs/spike/2026-05-17-postgres-aad-findings.md
§ Charge 3). ``pool_recycle=4800`` (80 min) is set to force SQLAlchemy to
close and recreate physical connections before the token expires. QueuePool
does not invoke ``do_connect`` on every session checkout — only on new
physical connections. A connection held past token TTL would receive
``FATAL: token expired`` from the server; pool_recycle prevents that by
closing + recreating physical connections every 80 minutes (under the
86-minute ceiling with a 6-minute safety margin).

For non-Postgres URLs (sqlite, used in unit tests and local dev), the hook is
not registered and pool_recycle is not set.
"""

from __future__ import annotations

import os

from azure.identity import ManagedIdentityCredential
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_OSSDB_AAD_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# pool_recycle ceiling: observed token TTL is 86 min (5160 s). Use 4800 s
# (80 min) for a 6-min safety margin. Citation:
# docs/spike/2026-05-17-postgres-aad-findings.md § Charge 3.
_POOL_RECYCLE_SECONDS = 4800


def build_session_factory(
    db_url: str,
    *,
    aad_client_id: str | None = None,
) -> sessionmaker[Session]:
    """Build a session factory; for Postgres URLs, inject AAD token on connect.

    Args:
        db_url: SQLAlchemy URL. ``postgresql+psycopg://...`` triggers AAD-token
            injection and pool_recycle; anything else (notably ``sqlite://``)
            is opened with no password injection.
        aad_client_id: Client ID of the user-assigned managed identity to use
            for token acquisition. Required when ``db_url`` is Postgres.
            Defaults to ``$AZURE_CLIENT_ID`` when not provided.

    Returns:
        A sessionmaker bound to the configured engine.
    """
    if db_url.startswith(("postgresql://", "postgresql+psycopg://")):
        engine: Engine = create_engine(
            db_url,
            echo=False,
            pool_recycle=_POOL_RECYCLE_SECONDS,
        )
        client_id = aad_client_id or os.environ.get("AZURE_CLIENT_ID")
        if not client_id:
            raise RuntimeError(
                "AZURE_CLIENT_ID must be set (or aad_client_id passed) "
                "when MOM_BOT_DATABASE_URL is a Postgres URL."
            )
        credential = ManagedIdentityCredential(client_id=client_id)

        @event.listens_for(engine, "do_connect")
        def _inject_aad_token(dialect, conn_rec, cargs, cparams):  # type: ignore[no-untyped-def]
            token = credential.get_token(_OSSDB_AAD_SCOPE)
            cparams["password"] = token.token

    else:
        engine = create_engine(db_url, echo=False)

    return sessionmaker(bind=engine)
```

- [ ] **Step 4: Run the test, verify it passes**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_db_token_injection.py -v
```
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mom_bot/db.py tests/test_db_token_injection.py
git commit -m "feat(db): AAD-token engine factory with pool_recycle=4800 for Postgres (#91)"
```

#### Task 3.2: Swap `main.py` to use new factory; remove `run_migrations`

**Files:**
- Modify: `src/mom_bot/main.py` (multiple edits — see Phase 3 reconciliation section above for exact lines)
- Modify: `tests/test_main_wireup.py` (remove `mock_run_migrations` fixture)

- [ ] **Step 1: Replace `_build_session_factory` body with import + delegation**

Edit `main.py:130-149` to:

```python
from mom_bot.db import build_session_factory as _build_session_factory  # noqa: F401

_DEFAULT_DB_URL = "sqlite:///./mom-bot.db"


def _resolve_db_url() -> str:
    return os.environ.get("MOM_BOT_DATABASE_URL", _DEFAULT_DB_URL)
```

Then update every previous caller of the old `_build_session_factory()` to pass `_resolve_db_url()` as the first arg.

- [ ] **Step 2: Remove `run_migrations` and all related artifacts from `main.py`**

Delete the following (added by PR #95, commit `de9b692`):
- Lines 51-52: `from alembic.command import upgrade as alembic_upgrade` and `from alembic.config import Config as AlembicConfig`
- Line 73: `_ALEMBIC_INI: str = os.environ.get("MOM_BOT_ALEMBIC_CONFIG", "alembic.ini")`
- Lines 76-105: the `run_migrations()` function body
- Line 206: the `run_migrations()` call site inside `MomBot.setup_hook`

Also remove the module-level docstring references to `run_migrations` in the "Startup migrations" section at the top of `main.py` (lines ~13-18), as they will be stale.

- [ ] **Step 3: Remove `mock_run_migrations` fixture from `tests/test_main_wireup.py`**

Delete the `mock_run_migrations` fixture (lines ~87-105 of `tests/test_main_wireup.py`) and remove any test assertions that reference it. Without `run_migrations` in `main.py`, the fixture suppresses nothing and its presence would cause an `AttributeError` on `patch("mom_bot.main.run_migrations")`.

- [ ] **Step 4: Edit `Dockerfile` to remove Alembic artifacts**

Alembic runs only in CI after Phase 3. The runtime image does not need `alembic.ini` or `migrations/`. Remove these two lines from `Dockerfile` (currently lines 11-12):

```dockerfile
COPY alembic.ini ./
COPY migrations/ ./migrations/
```

Rationale: if an operator needs to apply migrations against prod from inside the container during incident response, they can bind-mount the migrations dir or trigger the GHA deploy workflow manually. Eliminating ambiguity about who owns migration-apply (CI, exclusively) is more valuable than the rare ad-hoc debug path.

Also switch the install step from bare `pip` to `uv sync --frozen --no-dev` (dep-pinning Option A):

```dockerfile
COPY uv.lock ./
RUN pip install uv --no-cache-dir && uv sync --frozen --no-dev
```

- [ ] **Step 5: Run the full suite**

```powershell
.\.venv\Scripts\python.exe -m pytest
```
Expected: all tests PASS. Notably `tests/test_main_wireup.py` should still work because it patches the env var with a SQLite URL (no AAD path triggered).

- [ ] **Step 6: Commit**

```bash
git add src/mom_bot/main.py tests/test_main_wireup.py Dockerfile
git commit -m "refactor(main): use shared db.build_session_factory; remove run_migrations (refs #94, closed by #95) (#91)"
```

#### Task 3.3: PR

- [ ] **Step 1: Push and open draft PR**

```bash
git push
gh pr create --draft --title "feat(db): AAD-token engine + remove startup migrations (Phase 3 of #91)" --body-file <body>
```

**Acceptance criteria for Phase 3:**
- [ ] `pytest` is green.
- [ ] `grep -r "run_migrations" src/` returns nothing.
- [ ] `grep -r "run_migrations" tests/` is reviewed and any obsolete tests removed.
- [ ] Container image still builds (`docker build .`).
- [ ] `Dockerfile` no longer has `COPY alembic.ini` or `COPY migrations/` lines.
- [ ] `pool_recycle=4800` is present in the SQLAlchemy engine config for Postgres URLs.

---

### Phase 4 — Cutover (deploy workflow runs alembic; KV secret swap; revision restart)

**Goal:** Production runtime swings from broken-SQLite-on-AzureFile to working-Postgres. Done when `ca-mom-bot` is healthy on Postgres for at least one reminder tick.
**Entry criteria:** Phases 1-3 merged.
**Exit criteria:**
- `deploy.yml` runs `alembic upgrade head` successfully against prod Postgres.
- `prod-database-url` KV secret holds the Postgres DSN (`postgresql+psycopg://...`).
- `ca-mom-bot` revision is healthy; logs show reminder scheduler started + at least one DB query.
**Rollback:** Revert the KV secret to the old SQLite-on-SMB DSN and revert the workflow PR. Note: the bot was already broken pre-cutover, so "rollback to broken" is acceptable — the worst case is "still broken, but no worse than the last 12 hours."

#### Task 4.1: Update `deploy.yml` to run `alembic upgrade head`

**Files:**
- Modify: `.github/workflows/deploy.yml`

- [ ] **Step 1: Add migration steps before the image-update step**

Insert the following steps after `Verify image exists in GHCR` (around line 60) and before `Deploy container image to prod`:

```yaml
      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install Alembic + psycopg
        run: |
          pip install --quiet \
            'alembic>=1.13,<2' \
            'sqlalchemy>=2,<3' \
            'psycopg[binary]>=3.2,<4'

      - name: Resolve Postgres FQDN
        id: pg
        run: |
          FQDN=$(az postgres flexible-server list \
            --resource-group mom-bot \
            --query "[?starts_with(name,'pg-mombot-')].fullyQualifiedDomainName | [0]" \
            -o tsv)
          if [ -z "$FQDN" ]; then
            echo "::error::No pg-mombot-* server found in resource group mom-bot."
            exit 1
          fi
          echo "fqdn=$FQDN" >> "$GITHUB_OUTPUT"

      - name: Add transient firewall rule for runner IP
        id: fw
        run: |
          RUNNER_IP=$(curl -sf https://api.ipify.org)
          RULE_NAME="gha-runner-$(date +%s)"
          SERVER=$(echo "${{ steps.pg.outputs.fqdn }}" | cut -d. -f1)
          az postgres flexible-server firewall-rule create \
            --resource-group mom-bot \
            --name "$SERVER" \
            --rule-name "$RULE_NAME" \
            --start-ip-address "$RUNNER_IP" \
            --end-ip-address "$RUNNER_IP"
          echo "rule_name=$RULE_NAME" >> "$GITHUB_OUTPUT"
          echo "server=$SERVER" >> "$GITHUB_OUTPUT"

      - name: Wait for AAD admin propagation
        run: sleep 60
        # Spike #101 observed <60 s end-to-end latency for Entra admin
        # assignment to propagate. Hedge: sleep 60 s before first migration
        # attempt. Source: docs/spike/2026-05-17-postgres-aad-findings.md
        # § Bonus Finding 5.

      - name: Run alembic upgrade head
        env:
          # AAD token injected via PGPASSWORD; psycopg3 reads it from env.
          # Token TTL: ~86 min observed (spike #101 § Charge 3). The deploy
          # job runs end-to-end in well under 86 min so no refresh is needed.
          MOM_BOT_DATABASE_URL: >-
            postgresql+psycopg://mom-bot-gha@${{ steps.pg.outputs.fqdn }}:5432/mom_bot?sslmode=require
        run: |
          PGPASSWORD=$(az account get-access-token \
            --resource-type oss-rdbms \
            --query accessToken -o tsv)
          export PGPASSWORD
          alembic upgrade head

      - name: Remove transient firewall rule
        if: always()
        run: |
          az postgres flexible-server firewall-rule delete \
            --resource-group mom-bot \
            --name "${{ steps.fw.outputs.server }}" \
            --rule-name "${{ steps.fw.outputs.rule_name }}" \
            --yes
```

**Note on `PGPASSWORD` injection:** PGPASSWORD-with-AAD-token through psycopg3 against Flexible Server confirmed working in spike #101. Token format: 2234-char JWT, resource `https://ossrdbms-aad.database.windows.net`. Source: `docs/spike/2026-05-17-postgres-aad-findings.md` § Charge 2 (VERIFIED).

**Note on GHA OIDC federation (Charge 12 — still unverified):** spike #101 minted the token using a user identity (`az account get-access-token` on the operator's machine). Whether the same token resource works via GHA OIDC federation with the `mom-bot-gha` federated SP is **not yet verified**. Before merging Phase 4, run a one-off mini-spike: create a minimal GHA workflow that runs `az account get-access-token --resource https://ossrdbms-aad.database.windows.net` under the federated `mom-bot-gha` SP and verify it returns a token. This is a 5-minute workflow run at approximately $0.

- [ ] **Step 2: Lint the workflow**

```powershell
# actionlint if available; otherwise skip and rely on the PR check.
```

#### Task 4.2: Swap KV secret value

**Files:** none (Azure operation; documented in runbook)

- [ ] **Step 1: Update KV secret to the new passwordless Postgres DSN**

```powershell
$fqdn = az postgres flexible-server list -g mom-bot --query "[0].fullyQualifiedDomainName" -o tsv
$dsn = "postgresql+psycopg://mi-mom-bot@${fqdn}:5432/mom_bot?sslmode=require"
az keyvault secret set `
  --vault-name kv-mombot-eastus2 `
  --name prod-database-url `
  --value $dsn
```

Note: `mi-mom-bot` is the **UAMI display name** (= Postgres role name). Postgres-AAD matches by the role-name + token tenant + object-ID combination; the UAMI display name must equal the Entra admin "principalName" set in `postgres.bicep`. UAMI display names do not contain special characters that require URL-encoding (they are typically a plain slug); if the display name ever changes to contain `@` or `#`, URL-encode the user component. Source: `docs/spike/2026-05-17-postgres-aad-findings.md` § Bonus Finding 2.

- [ ] **Step 2: Verify Container App picks up the new secret**

KV secret references in Container Apps are resolved at revision-create time, not poll-based. A revision update is required (which Step 3 forces via image redeploy).

#### Task 4.3: Trigger deploy and verify

- [ ] **Step 1: Push the workflow change, merge the PR**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci(deploy): add alembic upgrade head step on Postgres (#91)"
git push
gh pr create --title "ci(deploy): Postgres cutover (Phase 4 of #91)" --body-file <body>
# After review, merge.
```

- [ ] **Step 2: Trigger workflow_dispatch**

```bash
gh workflow run deploy.yml
```

- [ ] **Step 3: Wait for completion**

```bash
scripts/wait-for-pr-checks.sh <pr-number-of-the-deploy-PR>
# OR for a workflow_dispatch run, poll runs:
gh run watch
```

- [ ] **Step 4: Verify container revision health**

```powershell
az containerapp revision list -n ca-mom-bot -g mom-bot --query "[?properties.active].{name:name, healthState:properties.healthState, runningState:properties.runningState}" -o table
```
Expected: active revision shows `Healthy` / `Running`.

- [ ] **Step 5: Tail logs for one reminder tick**

```powershell
az containerapp logs show -n ca-mom-bot -g mom-bot --tail 100 --follow
```
Expected: see `reminder scheduler started`, plus periodic activity. No `OperationalError`, no `connection refused`, no `password authentication failed`.

**Acceptance criteria for Phase 4:**
- [ ] `deploy.yml` run completes green end-to-end.
- [ ] `ca-mom-bot` active revision `Healthy`.
- [ ] No DB errors in the last 15 minutes of logs.
- [ ] Reminder scheduler logs at least one tick.

---

### Phase 5 — Cleanup

**Goal:** Remove the AzureFile carcass; close superseded issues; tidy docs.
**Entry criteria:** Phase 4 confirmed stable for ≥ 24h.
**Exit criteria:** Storage account deleted; `infra/modules/storage.bicep` removed; #90 closed; #93 reassessed; runbook updated.
**Rollback:** Not applicable — this is removal of already-defunct infrastructure.

#### Task 5.1: Strip AzureFile wiring from Bicep

**Files:**
- Delete: `infra/modules/storage.bicep`
- Modify: `infra/main.bicep:82-94, 113`
- Modify: `infra/modules/containerapp.bicep:120-131, 166-173, 201-205`

- [ ] **Step 1: Remove the `storage` module from `main.bicep`** (delete lines 82-94, plus the `storageAccountName: storage.outputs.storageAccountName` line at 113).

- [ ] **Step 2: Strip storage binding + volumes from `containerapp.bicep`**:
  - Remove `param storageAccountName string` (and any param it was wired to).
  - Remove the `storages: [{...}]` block at lines 120-131 (the CAE storage binding).
  - Remove the `volumes: [{...}]` block at lines 166-173.
  - Remove the `volumeMounts: [{...}]` block at lines 201-205.

- [ ] **Step 3: Update the `maxReplicas` comment** (lines 82-83 of `containerapp.bicep`) — the SQLite-on-SMB justification is gone; replace with operational-simplicity rationale.

- [ ] **Step 4: What-if**

```powershell
az deployment sub what-if `
  --location eastus2 `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam `
  --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0
```
Expected: deletion of one `storageAccounts`, one `fileServices`, one `shares`; container app volume/volumeMounts removed.

- [ ] **Step 5: Apply**

```powershell
az deployment sub create `
  --location eastus2 `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam `
  --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0 `
  --mode Incremental
```

**Note on Azure Files soft-delete:** new storage accounts have soft-delete enabled by default with a 7-day retention per [Azure Files soft delete](https://learn.microsoft.com/en-us/azure/storage/files/storage-files-prevent-file-share-deletion) (fetched 2026-05-16). Deleting the storage account succeeds immediately; the soft-deleted share remains recoverable for 7 days. **No blocker** to deletion. If you want to fully purge before 7 days (e.g., to free the storage account name — not relevant here, name is auto-generated), you would: undelete share → disable soft-delete → re-delete share → delete account.

- [ ] **Step 6: Delete `storage.bicep`**

```powershell
git rm infra/modules/storage.bicep
```

- [ ] **Step 7: Commit**

```bash
git add infra/main.bicep infra/modules/containerapp.bicep
git commit -m "chore(infra): remove AzureFile/storage wiring (superseded by Postgres) (#91)"
```

#### Task 5.2: Update docs

**Files:**
- Modify: `infra/aad-runbook.md` (lines 278, 318, 414, 506, 526, 593-594 per Explore map)
- Modify: `README.md:19-20, 128-129, 130-172`
- Modify: `docs/secrets-inventory.md:32`

- [ ] **Step 1: `aad-runbook.md`** — replace the "SQLite-on-SMB policy" section with a new "Postgres AAD-admin setup" section. Update the `prod-database-url` example to the new passwordless DSN (`postgresql+psycopg://...`). Add a note about guest-UPN URL-encoding for operator probe commands.

- [ ] **Step 2: `README.md`** — update the Epic 0 / Alembic section: prod uses Postgres; local dev still uses SQLite. Update the Alembic section: migrations are CI-applied in prod, manually run in dev.

- [ ] **Step 3: `docs/secrets-inventory.md:32`** — update the `prod-database-url` description.

- [ ] **Step 4: Commit**

```bash
git add infra/aad-runbook.md README.md docs/secrets-inventory.md
git commit -m "docs: update runbook + README for Postgres cutover (#91)"
```

#### Task 5.3: Close superseded issues

- [ ] **Step 1: Close #90 (snapshot automation)** with comment:

```
Superseded by #91 (Postgres migration). Azure Postgres Flexible Server's
built-in 7-day PITR replaces the bespoke AzureFile snapshot script this
issue tracked. Closing as won't-fix.
```

- [ ] **Step 2: Update #93 (networkAcls)** with comment:

```
The SQL Server / storage networkAcls discussion in this issue is moot
post-#91 — the storage account is deleted. The remaining surface for
network ACL hardening is the Postgres firewall (currently restricted to
operator IPs + GHA runner ranges). Re-scope or close.
```

- [ ] **Step 3: Note on #94 (startup migrations)** — already closed by PR #95 on 2026-05-17. References #94 (closed by PR #95, 2026-05-17). No further action.

- [ ] **Step 4: Update #83 (deploy workflow)** with comment:

```
Partially addressed by #91 Phase 4 — deploy.yml now runs alembic upgrade
head against Postgres. Bicep apply step (full infra deploy) still
outstanding for this issue.
```

- [ ] **Step 5: Update #96 (if open)** — check current state and update relative to Postgres reality.

#### Task 5.4: Close #91

- [ ] **Step 1: Final PR for Phase 5 with `Closes #91` in body**

```bash
git push
gh pr create --title "chore(infra): post-Postgres cleanup (Phase 5 of #91)" --body "$(cat <<'EOF'
Closes #91

Removes AzureFile storage account, strips storage wiring from containerapp.bicep,
updates aad-runbook + README + secrets-inventory.

Per CLAUDE.md, "Closes" keyword in plain text (not in code fences).

🤖 _Generated by Claude Code on behalf of @cbeaulieu-gt_
EOF
)"
```

**Acceptance criteria for Phase 5:**
- [ ] `git ls-tree main -- infra/modules/storage.bicep` returns empty.
- [ ] `az resource list -g mom-bot --resource-type Microsoft.Storage/storageAccounts` returns `[]`.
- [ ] #91, #90 closed; #93 commented; #83 commented. (#94 already closed by PR #95.)
- [ ] This plan file deleted per `CLAUDE.md § Document Files / Lifecycle`.

---

## 5. Risks & Mitigations

| ID | Risk | Likelihood | Impact | Mitigation |
|----|------|-----------|--------|------------|
| R1 | SQLite share has unexpected data | Very low | Medium (silent data loss) | Task 1.1 (Phase 1) mandates inspection of the share before cutover; HALT condition on any file > 1 KiB. (Moved from Phase 4 per Charge 8.) |
| R2 | psycopg3 PGPASSWORD + AAD token auth fails against Flexible Server | VERIFIED Low | VERIFIED — works end-to-end. Token format: 2234-char JWT, resource `https://ossrdbms-aad.database.windows.net`. VERIFIED via spike #101 (closed 2026-05-17). See `docs/spike/2026-05-17-postgres-aad-findings.md` § Charge 2. No "unverified" qualifier applies. |
| R3 | AAD admin assignment race — Postgres provisioned but `administrators` resource fails | Low | Medium (server orphaned, no one can connect) | Bicep resource ordering: `administrators` declares `parent: pg`, so it's implicitly ordered after server creation. If the AAD admin resource fails, redeploy is idempotent. Worst case: manually run `az postgres flexible-server ad-admin create`. Propagation lag: <60 s observed in spike #101 (docs/spike/2026-05-17-postgres-aad-findings.md § Bonus Finding 5). |
| R4 | Burstable B1ms credit exhaustion under unexpected load | Medium | Medium (transient connection failures during credit-empty windows per [concepts-compute](https://learn.microsoft.com/en-us/azure/postgresql/compute-storage/concepts-compute) (fetched 2026-05-16)) | Set up an Azure Monitor alert on `CPU Credits Remaining < 30` post-Phase 4 (track in a follow-up issue, not blocking #91). For a Discord bot's load profile this is very unlikely. |
| R5 | KV secret swap and revision restart out of order — bot starts on old secret | Low | Low (one revision restart fixes it) | KV secret references resolve at revision-create time; the deploy workflow's `az containerapp update` creates a new revision so the swap is automatic. Verify in Task 4.3 Step 4. |
| R6 | Container App egress IP not covered by firewall rules | Low | High (bot can't connect) | With the pivot away from `0.0.0.0`, the Container App's static outbound IP (visible in CAE properties — `staticIp`) must be whitelisted explicitly. Verify at Phase 4 deploy time: if bot cannot connect, add the CAE `staticIp` as a dedicated firewall rule. This is the primary trade-off of pinning operator IPs vs. the broad Azure-services rule. |
| R7 | Microsoft.Graph provider registration required for `administrators` resource | RESOLVED — NOT APPLICABLE | Verified 2026-05-17: `az provider show -n Microsoft.Graph` returns `InvalidResourceNamespace` — Microsoft.Graph is not a registerable Azure resource provider. The `administrators` resource does not require it. No registration step needed. |
| R8 | AAD admin propagation delay blocks first migration run | Low (bounded) | Low | Spike #101 observed <60 s end-to-end (docs/spike/2026-05-17-postgres-aad-findings.md § Bonus Finding 5). Phase 4 deploy workflow includes a `sleep 60` after Entra admin assignment. Risk remains on the register but the stop-loss narrows from "minutes-to-5min" to ≤ 60 s observed ceiling. |
| R9 | Connection pool exhaustion — B1ms supports ~50 max connections | Low | Medium | Configure `pool_size=10, max_overflow=5` in the SQLAlchemy engine config (Phase 3 deliverable — add to `db.py`). These values are empirical for the bot's session-per-tick pattern with one app instance. Well under the B1ms connection ceiling. |
| R10 | UAMI display-name binding rule — Postgres role name must match Entra admin `principalName` | Low (easy to misconfigure) | Medium (auth failure at connect) | Phase 1 documentation item: the `principalName` set in `postgres.bicep` must exactly match the UAMI display name. Verify before Phase 1 deploy. Cite: [Microsoft Entra auth for PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts) (fetched 2026-05-16). |

---

## 6. Cross-Issue Impact

| Issue | Status post-#91 | Action |
|-------|-----------------|--------|
| #90 — AzureFile snapshot automation | Superseded (Postgres PITR replaces it) | Close in Task 5.3 |
| #93 — networkAcls | Re-scoped (storage gone; Postgres firewall is new surface, now bounded to operator IPs + GHA ranges) | Comment + leave open for separate decision |
| #94 — startup-migration topology | ALREADY CLOSED — closed by PR #95 on 2026-05-17. References #94 (closed by PR #95, 2026-05-17). Phase 3 removes what #95 added. | No further action in Task 5.3. |
| #83 — deploy workflow runs Bicep | Partially addressed (alembic step added; Bicep apply step still TODO) | Comment in Task 5.3 |
| #96 — (verify current state in Phase 5) | unverified: needs check at cutover time | Address in Task 5.3 Step 5 |
| #87 / #92 — SQLite-on-AzureFile stopgap PRs | Reverted in effect by #91 (storage module removed) | No action; commit message links suffice |

---

## 7. Definition of Done

- [ ] All five phases shipped as separate merged PRs, each with `(#91)` reference.
- [ ] `closes #91` in the Phase 5 PR body (plain text, not in backticks — per `CLAUDE.md § Pull Requests`).
- [ ] Postgres server `pg-mombot-*` running, ca-mom-bot healthy on it.
- [ ] `git ls-tree main -- infra/modules/storage.bicep` empty.
- [ ] `git grep -n "run_migrations\|mom-bot-data\|stomombot\|AzureFile\|sqlite:///.*/data" -- src/ infra/ migrations/` returns only intentional matches (the test fixtures in `tests/` remain — by design).
- [ ] Bot has run for ≥ 7 days on Postgres without DB-related errors.
- [ ] This plan file deleted per `CLAUDE.md § Document Files / Lifecycle: delete plan files when done`.

---

## 8. Estimated Effort

| Phase | Effort |
|-------|--------|
| Phase 1 — Provision Postgres | Small (Bicep + one deployment) |
| Phase 2 — Schema portability | Small (0002 rewrite + postgres test) |
| Phase 3 — Application wiring | Medium (new module + test + main.py surgery + Dockerfile) |
| Phase 4 — Cutover | Medium (workflow surgery + live verification) |
| Phase 5 — Cleanup | Small (deletions + doc updates + issue triage) |
| **Total** | **Small-Medium** — 1-2 focused days of work for a single contributor, spread across at least two calendar days to allow ≥ 24h soak between Phase 4 and Phase 5. |

---

## 9. Dependency Pinning Decision (Charge 7)

**Decision: Option A — materialize `uv.lock` into the image.**

Add to `Dockerfile`:
```dockerfile
COPY uv.lock ./
RUN pip install uv --no-cache-dir && uv sync --frozen --no-dev
```

Rationale: after the post-SMB incident, reproducible builds are cheap insurance. Floating deps (`sqlalchemy>=2`, `alembic`) let a transitive bump break prod at any deploy. `uv sync --frozen` ensures the running image is bit-for-bit identical to the tested state. Bumps require an explicit `uv lock --upgrade` PR, which surfaces the change in code review.

The three DB deps are also upper-bounded in `pyproject.toml` (see § 3 Modified files) as a belt-and-suspenders measure.

---

## 10. Sources Index

All Microsoft Learn URLs fetched 2026-05-16 unless noted.

- [Compute Options — Azure DB for PostgreSQL Flexible Server](https://learn.microsoft.com/en-us/azure/postgresql/compute-storage/concepts-compute) — Burstable B1ms specs (1 vCore, 2 GiB, 640 IOPS); "for nonproduction" warning; CPU-credit semantics.
- [Microsoft Entra Authentication for PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts) — UAMI as Entra admin supported; token lifetime up to 24h (upper bound for user tokens; observed SP/UAMI TTL is ~86 min per spike #101); multiple admins supported.
- [Networking with Private Access — Azure DB for PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private) — subnet delegation requirement (/28 min), private DNS zone requirements.
- [Networking in Azure Container Apps environment](https://learn.microsoft.com/en-us/azure/container-apps/networking) — environment network type is immutable post-create; workload-profiles /27 subnet minimum.
- [Azure file share soft delete](https://learn.microsoft.com/en-us/azure/storage/files/storage-files-prevent-file-share-deletion) — 7-day default retention; deletion succeeds immediately; purge procedure.
- **Spike #101 findings:** `docs/spike/2026-05-17-postgres-aad-findings.md` — end-to-end PGPASSWORD+AAD-token verification (Charge 2), 86-min observed token TTL (Charge 3), strftime CHECK failure (Charge 5), SQLAlchemy scheme requirement (Bonus 1), guest-UPN encoding (Bonus 2), az CLI ≥ 2.86 requirement (Bonus 3), 0.0.0.0 public-access semantics (Bonus 4), <60 s AAD admin propagation (Bonus 5).
- **verified-cost:** Throwaway B1ms ran ~30 minutes at < $0.20 USD total (spike #101 § Cost, 2026-05-17). Annual estimate for a continuously-running B1ms: ~$13–15/mo at eastus2 rates.
- Repo refs: `infra/modules/storage.bicep:1-78`, `infra/modules/containerapp.bicep:120-131,166-173,201-205`, `infra/main.bicep:82-94,113`, `src/mom_bot/main.py:51-52,73,76-105,206` (run_migrations artifacts added by PR #95), `migrations/versions/0002_reminders_schema.py` (strftime CHECK on lines ~65-68), `migrations/versions/b2_member_role_sync_state.py`, `pyproject.toml:10-20`, `.github/workflows/deploy.yml`, `tests/test_alembic.py`, `tests/test_main_wireup.py:87-105` (mock_run_migrations fixture).
- GitHub refs: #91 (this epic), #90 (snapshot — superseded), #93 (networkAcls — rescoped), #94 (startup migrations — CLOSED by PR #95, 2026-05-17), #83 (deploy workflow — partially addressed), #87 / #92 (SQLite stopgap PRs — reverted in effect), #84 / #86 (UAMI + AZURE_CLIENT_ID pattern — reused), #95 (added run_migrations — removed by Phase 3), #101 (spike — merged, findings at docs/spike/2026-05-17-postgres-aad-findings.md), commit `de9b692` (PR #95 fix).

### Items marked `unverified:`

- Charge 12 / GHA federated identity: spike used a user identity, not the federated `mom-bot-gha` SP. Still unverified that `az account get-access-token --resource https://ossrdbms-aad.database.windows.net` works under GHA OIDC. Phase 4 mini-spike required before cutover.
- #96 current state — needs check at Phase 5 time.

---

## 11. Review Response — 2026-05-17 (Inquisitor Self-Review Pass)

### Spike #101 reconciliation — 2026-05-17

| Charge | Status | Rationale |
|--------|--------|-----------|
| 1 — Reconcile against PR #95 | RESOLVED | #94 already closed by PR #95 on 2026-05-17. Plan updated throughout: "Closes #94" → "References #94 (closed by PR #95, 2026-05-17)". Phase 3 reconciliation section lists exact artifacts to remove (lines 51-52, 73, 76-105, 206 of main.py; mock_run_migrations fixture in test_main_wireup.py:87-105). |
| 2 — R2 risk verified by spike | RESOLVED | R2 updated to VERIFIED. "unverified" prefix removed. Token format documented (2234-char JWT, resource `https://ossrdbms-aad.database.windows.net`). Cited: `docs/spike/2026-05-17-postgres-aad-findings.md` § Charge 2. |
| 3 — Pool token-refresh design | RESOLVED | `pool_recycle=4800` added to Phase 3 `db.py` implementation. Rationale documented in module docstring and in Task 3.1 Step 3. TTL ceiling updated from ~24h to 86 min observed. Cited: `docs/spike/2026-05-17-postgres-aad-findings.md` § Charge 3. |
| 4 — public-access trade-off | RESOLVED | Firewall pivoted from `0.0.0.0` (any Azure tenant) to specific operator IP + transient GHA runner rules. Trade-off paragraph added to Q1. Bicep `postgres.bicep` updated with `operatorIpAddress` param. R6 updated to reflect new surface. Cited: spike § Bonus Finding 4. |
| 5 — Phase 2 Postgres test coverage + 0002 fix | RESOLVED | Phase 2 pivoted: rewrite `ck_fire_time_no_seconds` inside 0002 using `EXTRACT(SECOND FROM fire_time_utc) = 0`. 0003 migration not created. `tests/test_alembic_postgres.py` added as Phase 2 required deliverable (Task 2.3). Cited: `docs/spike/2026-05-17-postgres-aad-findings.md` § Charge 5. |
| 6 — Dockerfile decision | RESOLVED | Phase 3 Task 3.2 Step 4 explicitly removes `COPY alembic.ini` and `COPY migrations/` from Dockerfile. Rationale: CI owns migration-apply exclusively. `uv sync --frozen --no-dev` added (dep-pinning Option A). |
| 7 — dep pinning | RESOLVED | Option A chosen (uv.lock materialized into image). pyproject.toml DB dep upper bounds added. § 9 added to document the decision. |
| 8 — `az storage file list` moved to Phase 1 | RESOLVED | Task 4.1 (was Phase 4) moved to Task 1.1 (Phase 1) as a phase prerequisite. Phase 1 acceptance criteria updated. |
| 9 — R7 Microsoft.Graph provider | RESOLVED — NOT APPLICABLE | Verified 2026-05-17: `az provider show -n Microsoft.Graph` returns `InvalidResourceNamespace`. Provider is not registerable for Postgres `administrators` resource. R7 updated in risk register. |
| 10 — B1ms PITR retention `backupRetentionDays: 7` | RESOLVED | Confirmed valid: `az postgres flexible-server create --help` states range 7–35 days. R10 (UAMI display-name) added to risk register as documentation item. `backupRetentionDays: 7` cited in Bicep comment. |
| 11 — ~$13/mo pricing | RESOLVED | Changed from `unverified:` to `verified-cost:` in Sources Index § 10. Spike #101 observed <$0.20 for 30 min. Extrapolated to ~$13-15/mo; marker changed to `verified-cost:`. |
| 12 — GHA federated identity / oss-rdbms audience | DEFERRED — still unverified | Spike used user identity, not federated SP. Phase 4 mini-spike added to Task 4.1 Step 1. Charge 12 remains in `unverified:` section of § 10. |

### Bonus findings from spike — incorporated

| Finding | Status |
|---------|--------|
| `postgresql+psycopg://` scheme | RESOLVED — all SQLAlchemy URLs in Phase 2, Phase 3, Phase 4 updated to use `postgresql+psycopg://` scheme |
| Guest UPN URL-encoding | RESOLVED — noted in Phase 1 smoke-test step and Task 4.2 KV secret step |
| az CLI ≥ 2.86 | RESOLVED — added as Phase 1 prerequisite |
| `--public-access 0.0.0.0` semantics | RESOLVED — see Charge 4 above |
| AAD admin propagation <60 s | RESOLVED — R8 updated; `sleep 60` hedge added to Phase 4 deploy workflow step; propagation window tightened |
