---
touches:
  - src/mom_bot/db/__init__.py
  - src/mom_bot/main.py
  - src/mom_bot/reminders/scheduler.py
  - migrations/versions/0002_reminders_schema.py
  - migrations/env.py
  - tests/test_alembic.py
  - tests/test_alembic_postgres.py        # planned new file
  - tests/test_db_token_injection.py      # planned new file
  - tests/test_main_wireup.py
  - tests/test_migrations_startup.py
  - Dockerfile
  - pyproject.toml
  - uv.lock
  - infra/main.bicep
  - infra/main.bicepparam
  - infra/modules/postgres.bicep          # planned new file
  - infra/modules/containerapp.bicep
  - infra/modules/storage.bicep           # for removal in Phase 5
  - infra/aad-runbook.md
  - docs/secrets-inventory.md
  - README.md
  - .github/workflows/deploy.yml
skills_relevant:
  - python
  - bicep
  - azure
  - github-actions
  - powershell
---

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

**Trade-off / known sharp edge:** Alembic CLI run from the GHA runner also needs a token. The GHA service principal (`mom-bot-gha`) must also be added as an Entra admin (multiple Entra admins are supported per the same FAQ: "you can set as many Microsoft Entra administrators as you want"). The deploy workflow uses `az account get-access-token --resource https://ossrdbms-aad.database.windows.net` to mint the token and injects it as `PGPASSWORD`. Verified working end-to-end against a real Flexible Server in spike #101 (`docs/spike/2026-05-17-postgres-aad-findings.md` § Charge 2).

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
- `tests/test_db_token_injection.py` — verifies the AAD-token hook is invoked on connect and stamps `connection.password` from the credential.
- `tests/test_alembic_postgres.py` — runs `alembic upgrade head` against a real Postgres instance (via `testcontainers-python` or GitHub Actions `services: postgres:16`) to catch dialect-specific DDL failures before they reach production.
- `.github/workflows/mini-spike-postgres-oidc.yml` — one-off Phase 3 verification that `mom-bot-gha` federated SP can mint an oss-rdbms AAD token via GHA OIDC (Charge 12). Run once; disable/delete after verification.

### Modified files

- `src/mom_bot/db/__init__.py` — **existing package.** `build_session_factory` added alongside existing `Base = DeclarativeBase` export. `Base` import in `migrations/env.py:24` (`from mom_bot.db import Base`) is preserved unchanged. A new `src/mom_bot/db.py` must NOT be created — it would be a name collision with the `db/` package.
- `infra/main.bicep` — instantiate `postgres` module; remove `storage` module instantiation; pass Postgres FQDN to `containerapp.bicep`.
- `infra/main.bicepparam` — add Postgres admin object IDs (UAMI + GHA SP).
- `infra/modules/containerapp.bicep` — strip storage binding (lines 120-131), volumes (166-173), volumeMounts (201-205); update `database-url` secret reference (KV secret already exists; only the value changes); remove `COPY alembic.ini` and `COPY migrations/` from Dockerfile (Alembic runs only in CI — see Phase 3 deliverable).
- `infra/aad-runbook.md` — replace the SQLite-on-SMB policy section with the Postgres Entra-admin runbook step; update `prod-database-url` example. Note guest-UPN URL-encoding requirement for operator probe commands.
- `src/mom_bot/main.py` — replace `_build_session_factory` with import of `build_session_factory` from `mom_bot.db` (which lives in `db/__init__.py` alongside `Base`); remove `run_migrations()` (lines 76-105) and its call site in `setup_hook` (line 206); remove the alembic imports at lines 51-52 (`from alembic.command import upgrade as alembic_upgrade`, `from alembic.config import Config as AlembicConfig`); remove `_ALEMBIC_INI` constant (line 73).
- `pyproject.toml` — add `psycopg[binary]>=3.2,<4`, pin `sqlalchemy>=2,<3`, pin `alembic>=1.13,<2` to `dependencies`; materialize `uv.lock` into the image (see dep-pinning decision below).
- `migrations/versions/0002_reminders_schema.py` — **rewrite the `ck_fire_time_no_seconds` CHECK constraint** to use `EXTRACT(SECOND FROM fire_time_utc) = 0` (dialect-portable: works on both SQLite ≥ 3.38 and Postgres). This is a destructive edit to a committed migration — acceptable because #91 is explicitly fresh-Postgres-no-data-migration. See Phase 2 for rationale.
- `.github/workflows/deploy.yml` — add steps: install Python+`uv`+`psycopg[binary]`+`alembic`, mint AAD token via `az account get-access-token --resource https://ossrdbms-aad.database.windows.net`, add transient firewall rule for runner IP, run `alembic upgrade head`, remove firewall rule. Pin `az` CLI ≥ 2.86 in the runner prereq check.
- `README.md` — update the Epic 0 / Alembic section to reflect Postgres prod + SQLite local-dev.
- `docs/secrets-inventory.md` — update `prod-database-url` description (passwordless DSN, not SQLite path).
- `Dockerfile` — remove `COPY alembic.ini ./` and `COPY migrations/ ./migrations/` lines (lines 11-12); switch `pip install` to `uv sync --frozen --no-dev` after adding `COPY uv.lock` (dep-pinning Option A).

### Files deleted

- `infra/modules/storage.bicep` — entire file (Phase 5).
- `tests/test_migrations_startup.py` — deleted in Phase 3 Task 3.2 after `run_migrations` is removed from `main.py`; all four tests become dead.
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

- [ ] **Microsoft.DBforPostgreSQL provider registration** (F8): run `az provider register -n Microsoft.DBforPostgreSQL --wait` to ensure the provider is registered in the subscription. This command is idempotent — if already registered it completes immediately. Fresh Azure subscriptions fail with `Microsoft.DBforPostgreSQL is not registered` when creating a Flexible Server without this step.

- [ ] **Confirm no SQLite data exists worth preserving** (moved from Phase 4, Task 4.1): run the verification in Task 1.1 below before spending time on provisioning.

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
$token = az account get-access-token --resource https://ossrdbms-aad.database.windows.net --query accessToken -o tsv
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

#### Task 1.4: Look up CAE static egress IP and emit a named firewall rule

**Files:**
- Modify: `infra/modules/postgres.bicep`
- Modify: `infra/main.bicepparam`

The Container Apps Environment `cae-mom-bot` has a static outbound IP that is knowable before Phase 4 cutover. Discovering it at Phase 4 cutover is the worst possible time — a missing firewall rule blocks the bot immediately on a live production restart. Promote this to Phase 1 so the firewall rule is in place before any connection attempt.

- [ ] **Step 1: Look up the CAE static egress IP**

```powershell
az containerapp env show -n cae-mom-bot -g mom-bot --query 'properties.staticIp' -o tsv
```

Record the output (a dotted-quad IP). This is the `staticIp` that every outbound connection from `ca-mom-bot` will appear to come from.

- [ ] **Step 2: Add `caeEgressIp` parameter to `postgres.bicep`** and emit a named firewall rule:

```bicep
@description('Static egress IP of the Container Apps Environment (cae-mom-bot). '
  + 'Used to allow the bot to connect to Postgres. '
  + 'Retrieve with: az containerapp env show -n cae-mom-bot -g mom-bot --query properties.staticIp -o tsv')
param caeEgressIp string

resource fwCae 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: pg
  name: 'allow-cae-egress'
  properties: {
    startIpAddress: caeEgressIp
    endIpAddress: caeEgressIp
  }
}
```

- [ ] **Step 3: Add `caeEgressIp` to `infra/main.bicepparam`** with the value from Step 1.

- [ ] **Step 4: Re-run the `az deployment sub what-if`** to confirm the new rule appears as a net-new `firewallRules` creation alongside the other Phase 1 resources.

**Acceptance criteria for Phase 1:**
- [ ] `az postgres flexible-server show` returns `state: Ready`.
- [ ] Operator can `psql` with AAD token (proves auth works end-to-end).
- [ ] CAE, KV, MI, Container App, storage unchanged (verified by what-if `=` lines).
- [ ] `az storage file list` on `mom-bot-data` share shows empty / zero-size `.db` file.
- [ ] Postgres firewall rule `allow-cae-egress` exists and its IP matches `az containerapp env show -n cae-mom-bot -g mom-bot --query 'properties.staticIp' -o tsv`.

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

- [ ] **Step 2: Verify SQLite version on the CI runner**

```bash
python -c 'import sqlite3; print(sqlite3.sqlite_version)'
```

`EXTRACT(SECOND FROM ...)` requires SQLite ≥ 3.38 (released 2022-02-22). CI's Ubuntu 22.04 ships SQLite 3.37.x. If the version is below 3.38, SQLite will silently accept the expression without enforcing the CHECK — the constraint becomes a no-op locally and the SQLite test path is not authoritative.

**If SQLite < 3.38 on CI:** the `test_alembic_postgres.py` path (Task 2.3) becomes the authoritative enforcement of the CHECK. The SQLite constraint expression should be wrapped in a dialect branch:

```python
from alembic import op
from sqlalchemy.engine import Engine
from sqlalchemy import text

def _check_expr(conn: Engine) -> str:
    if conn.dialect.name == "sqlite":
        return "CAST(strftime('%S', fire_time_utc) AS INTEGER) = 0"
    return "EXTRACT(SECOND FROM fire_time_utc) = 0"
```

Document the dialect branch decision in the migration file comment if taken.

**If SQLite ≥ 3.38 on CI:** a single `EXTRACT(SECOND FROM fire_time_utc) = 0` expression covers both dialects. Proceed with the single expression.

- [ ] **Step 3: Run SQLite-side tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_alembic.py -v
```
Expected: PASS — the edited migration runs against SQLite using `EXTRACT()` (or dialect branch if SQLite < 3.38).

- [ ] **Step 4: Run Postgres-side migration from operator laptop**

```powershell
$token = az account get-access-token --resource https://ossrdbms-aad.database.windows.net --query accessToken -o tsv
$env:PGPASSWORD = $token
$fqdn = az postgres flexible-server show -g mom-bot --name pg-mombot-XXXXXX --query fullyQualifiedDomainName -o tsv
$env:MOM_BOT_DATABASE_URL = "postgresql+psycopg://<your-aad-upn>@${fqdn}:5432/mom_bot?sslmode=require"
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Note the `postgresql+psycopg://` scheme — **not** bare `postgresql://`. SQLAlchemy defaults bare `postgresql://` to the psycopg2 dialect; only psycopg3 (`psycopg`) is installed. Using the wrong scheme raises `ModuleNotFoundError: No module named 'psycopg2'` at engine-creation time. Source: spike #101, `docs/spike/2026-05-17-postgres-aad-findings.md` § Bonus Finding 1.

Expected: each revision prints `Running upgrade ...` and exits 0. **First**, install `psycopg[binary]` — Task 2.2 below adds it to `pyproject.toml`.

**Note on authoritative enforcement:** `test_alembic_postgres.py` (Task 2.3) is the authoritative enforcement of the `ck_fire_time_no_seconds` CHECK on the Postgres dialect. Even if the SQLite path silently accepts the expression (e.g., due to SQLite < 3.38 on CI), Postgres will correctly fail the constraint if the expression is malformed. The Postgres-targeted test must pass before Phase 2 is considered done.

- [ ] **Step 5: Verify schema**

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
        # Build the URL explicitly rather than using string-replace on
        # get_connection_url() — the replace("psycopg2", "psycopg") pattern
        # is fragile: if testcontainers ever changes its default driver name the
        # replace becomes a no-op and the engine creation silently uses the wrong
        # dialect. Explicit construction is always correct.
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        yield (
            f"postgresql+psycopg://{pg.username}:{pg.password}"
            f"@{host}:{port}/{pg.dbname}"
        )


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

- [ ] **Step 3: Update `tests/test_alembic.py:343-376` docstring**

The `test_fire_time_utc_check_rejects_nonzero_seconds` test at lines 343-376 of `tests/test_alembic.py` still passes after the 0002 rewrite. Update its docstring to note that the constraint expression is now `EXTRACT(SECOND FROM fire_time_utc) = 0` (dialect-portable since the 0002 rewrite in Phase 2), not the old `strftime`-based form.

- [ ] **Step 4: Commit and open Phase 2 draft PR**

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

#### Task 3.1: Add `build_session_factory` to `src/mom_bot/db/__init__.py` with token-injection engine factory

`src/mom_bot/db/__init__.py` already exists and exports `Base = DeclarativeBase`. Phase 3 adds `build_session_factory` to this same file. A new top-level `src/mom_bot/db.py` must NOT be created — Python cannot have both a `db.py` module and a `db/` package at the same level, and `migrations/env.py:24` already imports `from mom_bot.db import Base`. Merging into `db/__init__.py` preserves that import without any change to `env.py`.

**Files:**
- Modify: `src/mom_bot/db/__init__.py`
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
Expected: FAIL with `ImportError: cannot import name 'build_session_factory' from 'mom_bot.db'` — the package exists but `build_session_factory` is not yet defined in it.

- [ ] **Step 3: Add `build_session_factory` to `src/mom_bot/db/__init__.py`**

**Preserve `Base` export.** `migrations/env.py:24` imports `from mom_bot.db import Base` directly. Whether the implementation uses `db/__init__.py` or a new sub-module, `Base = DeclarativeBase` must remain importable as `from mom_bot.db import Base`. The safest approach is to add `build_session_factory` to `db/__init__.py` alongside the existing `Base` declaration.

Append the following to `src/mom_bot/db/__init__.py` (after the existing `Base` class declaration):

```python
# ---------------------------------------------------------------------------
# Engine factory — added in Phase 3 (#91). The Base class above remains
# unchanged; migrations/env.py imports it as `from mom_bot.db import Base`.
# ---------------------------------------------------------------------------

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
physical connections — so ``pool_recycle`` is the primary guard.

IMPORTANT: ``pool_recycle`` only invalidates connections on checkout, not on
active connections mid-query. This design depends on the bot's session-per-tick
pattern: sessions are opened and closed per scheduler tick, never held open
across an 80-minute boundary. If a future change holds a session open longer
(e.g. a long-running background task), the cached physical connection's token
could be stale and the server would return ``FATAL: token expired`` on the next
query. Document any session-lifetime change as a re-evaluation trigger for this
design. Source: R9 in the risk register; Phase 3 decision.

Connection-pool sizing: ``pool_size=5, max_overflow=5`` (10 connections max).
B1ms user-accessible ceiling is 35 connections (Azure reserves 15 of the ~50
hard ceiling — see MS Learn [Postgres limits](https://learn.microsoft.com/azure/postgresql/configure-maintain/concepts-limits#maximum-connections)
(fetched 2026-05-17)). Deploy-window worst-case: old revision pool (10) + new
revision pool (10) + CI alembic conn (1) + operator psql (1) = 22/35. These
values are empirical for the bot's session-per-tick pattern with one app
instance (R9); conservative ceiling chosen to leave headroom during deploy
windows on the 35-cap B1ms tier.

``pool_size=5`` is empirically generous for the bot's session-per-tick pattern
— tick cadence is minutes-to-hours, sessions are returned to the pool well
before the next tick. Conservative ceiling chosen to leave headroom during
deploy windows on the 35-cap B1ms tier.

``pool_pre_ping=True`` is the pessimistic-disconnect-handling pattern:
SQLAlchemy issues a cheap ``SELECT 1`` on every checkout and transparently
reconnects if the connection is dead. This catches token expiry, server
failover, and network flaps — strictly more robust than ``pool_recycle``
alone, which only fires on the timer. Cost: one round-trip per checkout
(negligible for session-per-tick). Reference: SQLAlchemy docs
`Disconnect Handling - Pessimistic <https://docs.sqlalchemy.org/en/20/core/pooling.html#disconnect-handling-pessimistic>`_.

For non-Postgres URLs (sqlite, used in unit tests and local dev), the hook is
not registered and pool_recycle / pool_size are not set.
"""

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
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
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
git add src/mom_bot/db/__init__.py tests/test_db_token_injection.py
git commit -m "feat(db): AAD-token engine factory with pool_recycle=4800, pool_size=5, pool_pre_ping for Postgres (#91)"
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

- [ ] **Step 3: Remove `run_migrations` test artifacts from `tests/test_main_wireup.py` and delete `tests/test_migrations_startup.py`**

Delete the `mock_run_migrations` fixture (lines ~87-105 of `tests/test_main_wireup.py`) and remove any test assertions that reference it. Without `run_migrations` in `main.py`, the fixture suppresses nothing and its presence would cause an `AttributeError` on `patch("mom_bot.main.run_migrations")`.

Additionally, **delete `tests/test_migrations_startup.py` entirely.** This file (tests A through D) patches `mom_bot.main.run_migrations` at multiple points and imports `mom_bot.main.run_migrations` directly. Once Phase 3 removes `run_migrations` from `main.py`, every test in this file becomes dead (they will raise `AttributeError: module 'mom_bot.main' has no attribute 'run_migrations'`). Remove the file rather than converting it — the behaviour it verified no longer exists by design.

```bash
git rm tests/test_migrations_startup.py
```

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
git rm tests/test_migrations_startup.py
git commit -m "refactor(main): use shared db.build_session_factory; remove run_migrations (refs #94, closed by #95) (#91)"
```

#### Task 3.3: GHA OIDC mini-spike — verify `mom-bot-gha` can mint an oss-rdbms token (Charge 12)

This mini-spike workflow must run and pass before Phase 4 begins. It is a Phase 3 deliverable — not a Phase 4 in-flight step — because if GHA OIDC federation cannot mint a token for the `https://ossrdbms-aad.database.windows.net` audience, Phase 4 has no fallback and the deploy workflow step becomes a blocker at the worst possible moment.

**Files:**
- Create: `.github/workflows/mini-spike-postgres-oidc.yml`

- [ ] **Step 1: Create the mini-spike workflow**

```yaml
# mini-spike-postgres-oidc.yml — one-off verification that the mom-bot-gha
# federated SP can mint an AAD token for the oss-rdbms audience via OIDC.
# Run once, verify it succeeds, then delete or disable this workflow.
# Charge 12 of spike #101 (docs/spike/2026-05-17-postgres-aad-findings.md):
# the spike minted tokens using a user identity, not the federated SP.
name: "Mini-spike: Postgres OIDC token"

on:
  workflow_dispatch:

permissions:
  id-token: write
  contents: read

jobs:
  verify-oidc-token:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Azure login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID_GHA }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}

      - name: Mint oss-rdbms AAD token
        run: |
          TOKEN=$(az account get-access-token \
            --resource https://ossrdbms-aad.database.windows.net \
            --query accessToken -o tsv)
          if [ -z "$TOKEN" ]; then
            echo "::error::Token is empty — oss-rdbms audience not supported for this SP"
            exit 1
          fi
          # Print first 20 chars to confirm non-empty without leaking full token.
          echo "Token prefix: ${TOKEN:0:20}..."
          echo "Token length: ${#TOKEN}"
```

- [ ] **Step 2: Trigger the workflow via `workflow_dispatch`**

```bash
gh workflow run mini-spike-postgres-oidc.yml
gh run watch
```

Expected: the "Mint oss-rdbms AAD token" step exits 0 and prints a token prefix + length.

- [ ] **Step 3: Record the result**

Update the `unverified:` Charge 12 entry in § 10 to either:
- `VERIFIED — GHA OIDC federation mints oss-rdbms token for mom-bot-gha SP` (if it passed), or
- `BLOCKED — Charge 12 failed; see [run link]; Phase 4 cannot proceed until resolved` (if it failed).

Phase 4 **must not begin** until this item is resolved.

#### Task 3.4: PR

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
- [ ] `pool_pre_ping=True` is set on the engine for Postgres URLs.
- [ ] `pool_size=5, max_overflow=5` are set on the engine for Postgres URLs.

---

### Phase 4 — Cutover (deploy workflow runs alembic; KV secret swap; revision restart)

**Goal:** Production runtime swings from broken-SQLite-on-AzureFile to working-Postgres. Done when `ca-mom-bot` is healthy on Postgres for at least one reminder tick.
**Entry criteria:** Phases 1-3 merged, AND:
- **Charge 12 mini-spike has passed** — GHA OIDC federated identity (`mom-bot-gha` SP) has verified that `az account get-access-token --resource https://ossrdbms-aad.database.windows.net` mints a token under the federated identity (Task 3.3 in Phase 3). The § 10 `unverified:` entry for Charge 12 must read `VERIFIED` before Phase 4 may begin. If Charge 12 failed, stop and resolve the federation configuration before proceeding.

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

**Dependency note (F10):** `alembic.ini` and `migrations/` are sourced from the GHA runner's `actions/checkout` of this repo, not from the deployed image (Phase 3 removed them from the image). The `actions/checkout` step must remain in the workflow and must be pinned to the same commit SHA being deployed so that migrations and image are in lockstep.

```yaml
      - name: Install uv
        uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b  # v8.1.0

      - name: Install Python deps from uv.lock
        run: uv sync --frozen --no-dev
        # uv.lock is materialized in the image per § 9 dep-pinning Option A;
        # the deploy workflow also uses it for the migration step.
        # This installs alembic, sqlalchemy, psycopg[binary] from the locked
        # versions — same as what the container image runs.

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
            --resource https://ossrdbms-aad.database.windows.net \
            --query accessToken -o tsv)
          export PGPASSWORD
          uv run alembic upgrade head

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

**Note on GHA OIDC federation (Charge 12):** The mini-spike workflow (`mini-spike-postgres-oidc.yml`) was run as a Phase 3 deliverable (Task 3.3). If it passed, Charge 12 is verified and this note is informational only. If Phase 4 is being executed and Charge 12 is still marked `unverified:` in § 10, **stop and run the mini-spike first** — merging Phase 4 without verifying OIDC token acquisition means the `alembic upgrade head` step will fail on the first deploy run with no fallback path.

**Note on alternative migration patterns (reach option):** Azure Container Apps Jobs is a viable alternative pattern if migrations grow. Container Apps Jobs is the platform's Kubernetes-Job equivalent; a separate Bicep resource + its own UAMI grant could host the migration step without coupling it to the CI pipeline. Adds two moving parts (resource + identity); rejected for #91 in favor of the simpler CI-side approach. Re-evaluate if migration frequency or duration outgrows a single CI step.

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

**Ordering note:** The `git ls-tree main -- infra/modules/storage.bicep` empty criterion in § 7 (Definition of Done) only becomes true after this step lands on `main`. Not before — any earlier phase's `main` state still contains `storage.bicep` because it was not yet removed. The Definition of Done check must be run post-merge of the Phase 5 PR.

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
| R6 | Container App egress IP not covered by firewall rules | Low → MITIGATED (Phase 1) | High (bot can't connect) | Mitigated proactively in Phase 1 Task 1.4: the CAE static egress IP is looked up via `az containerapp env show -n cae-mom-bot -g mom-bot --query 'properties.staticIp' -o tsv` and emitted as a named firewall rule `allow-cae-egress` in `postgres.bicep`. Phase 4 verification: confirm `allow-cae-egress` rule still matches the current `properties.staticIp` value (the IP is static but worth a sanity check at cutover time). |
| R7 | Microsoft.Graph provider registration required for `administrators` resource | RESOLVED — NOT APPLICABLE | Verified 2026-05-17: `az provider show -n Microsoft.Graph` returns `InvalidResourceNamespace` — Microsoft.Graph is not a registerable Azure resource provider. The `administrators` resource does not require it. No registration step needed. |
| R8 | AAD admin propagation delay blocks first migration run | Low (bounded) | Low | Spike #101 observed <60 s end-to-end (docs/spike/2026-05-17-postgres-aad-findings.md § Bonus Finding 5). Phase 4 deploy workflow includes a `sleep 60` after Entra admin assignment. Risk remains on the register but the stop-loss narrows from "minutes-to-5min" to ≤ 60 s observed ceiling. |
| R9 | Connection pool exhaustion — B1ms user-accessible ceiling is 35 connections (Azure reserves 15) | Low | Medium | Configure `pool_size=5, max_overflow=5` (10 total connections) in the SQLAlchemy engine config (Phase 3 deliverable — implemented in `src/mom_bot/db/__init__.py` `build_session_factory`). B1ms user-accessible ceiling is 35 (Azure reserves 15 — see MS Learn [Postgres limits](https://learn.microsoft.com/azure/postgresql/configure-maintain/concepts-limits#maximum-connections), fetched 2026-05-17). Deploy-window worst-case: old revision pool (10) + new revision pool (10) + CI alembic conn (1) + operator psql (1) = 22/35. Halved vs the prior `pool_size=10` design. |
| R10 | UAMI display-name binding rule — Postgres role name must match Entra admin `principalName` | Low (easy to misconfigure) | Medium (auth failure at connect) | Phase 1 documentation item: the `principalName` set in `postgres.bicep` must exactly match the UAMI display name. Verify before Phase 1 deploy. Cite: [Microsoft Entra auth for PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts) (fetched 2026-05-16). |
| R11 | CAE `staticIp` is not contractually static | Low (Azure rotates rarely) | High (bot stops connecting; symptoms = `pg_hba.conf` reject on every tick) | **Detection:** alert on bot logs containing `pg_hba.conf reject` or `no pg_hba.conf entry` (Phase 4 Task 4.x — wire into existing log monitor). **Remediation:** re-run `az containerapp env show -n cae-mom-bot -g mom-bot --query 'properties.staticIp' -o tsv`; update `caeEgressIp` bicepparam; `az deployment sub create` to re-apply. **Long-term:** workload-profiles CAE + NAT gateway is the only contractually-static-egress path per MS Learn ([Container Apps networking — outbound IP addresses](https://learn.microsoft.com/azure/container-apps/networking#http-edge-proxy-behavior), fetched 2026-05-17). Out of scope here — CAE network type is immutable, same constraint that rejected private endpoint in § 2 Q1. **Source:** devops review of plan-revision-2 (2026-05-17, CONCERN-1). |
| R12 | `mom-bot-gha` holds both Subscription Contributor AND Postgres Entra admin on one federated credential | Low (requires workflow-yaml compromise on `main`) | Critical (full subscription mutations + server-wide DDL/DML on Postgres) | **Accepted-known for #91** — splitting now adds two SPs, two FICs, two GHA environments, two `azure/login` blocks; orthogonal to the migration epic. **Tracked separately:** issue #103 (glitchwerks/mom-bot#103) — split into `mom-bot-gha-deploy` + `mom-bot-gha-migrate` gated by GHA environments (`prod-deploy` / `prod-migrate`). **Interim hardening:** protect `main` branch with required reviews; require all workflow edits to flow through PRs (existing). No direct pushes from non-author identities. **Source:** devops review of plan-revision-2 (2026-05-17, CONCERN-3). |

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
- [ ] `git grep -n "run_migrations\|mom-bot-data\|stomombot\|AzureFile\|sqlite:///.*/data" -- src/ infra/ migrations/` returns only intentional matches (the `run_migrations` test files in `tests/` are deleted in Phase 3 Task 3.2 — zero matches in `tests/` is expected after Phase 3).
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
- Repo refs: `infra/modules/storage.bicep:1-78`, `infra/modules/containerapp.bicep:120-131,166-173,201-205`, `infra/main.bicep:82-94,113`, `src/mom_bot/main.py:51-52,73,76-105,206` (run_migrations artifacts added by PR #95), `src/mom_bot/db/__init__.py` (existing package — `Base` export; Phase 3 adds `build_session_factory` here), `migrations/versions/0002_reminders_schema.py` (strftime CHECK on lines ~65-68), `migrations/versions/b2_member_role_sync_state.py`, `migrations/env.py:24` (`from mom_bot.db import Base` — must remain valid after Phase 3), `pyproject.toml:10-20`, `.github/workflows/deploy.yml`, `tests/test_alembic.py:343-376` (fire_time_utc CHECK test), `tests/test_main_wireup.py:87-105` (mock_run_migrations fixture), `tests/test_migrations_startup.py` (all tests dead after Phase 3 removes run_migrations).
- GitHub refs: #91 (this epic), #90 (snapshot — superseded), #93 (networkAcls — rescoped), #94 (startup migrations — CLOSED by PR #95, 2026-05-17), #83 (deploy workflow — partially addressed), #87 / #92 (SQLite stopgap PRs — reverted in effect), #84 / #86 (UAMI + AZURE_CLIENT_ID pattern — reused), #95 (added run_migrations — removed by Phase 3), #101 (spike — merged, findings at docs/spike/2026-05-17-postgres-aad-findings.md), commit `de9b692` (PR #95 fix).

### Items marked `unverified:`

- Charge 12 / GHA federated identity: spike used a user identity, not the federated `mom-bot-gha` SP. Still unverified that `az account get-access-token --resource https://ossrdbms-aad.database.windows.net` works under GHA OIDC. **Phase 3 Task 3.3 is the verification gate** — the mini-spike workflow must pass and this item updated to `VERIFIED` before Phase 4 may begin. This is now a hard Phase 4 entry criterion.
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

---

## 12. Review Response — 2026-05-17 (Project-Reviewer Pass, 13 + 2 findings)

### Findings reconciliation

| Finding | Severity | Status | Resolution |
|---------|----------|--------|------------|
| F1 — `db.py` vs `db/` package conflict | BLOCKING | RESOLVED | Task 3.1 changed from "Create: `src/mom_bot/db.py`" to "Modify: `src/mom_bot/db/__init__.py`". Failing-test expectation updated from `ModuleNotFoundError` to `ImportError: cannot import name 'build_session_factory'`. Patch paths (`mom_bot.db.*`) remain valid — `db/__init__.py` is the `mom_bot.db` module. |
| F2 — `pool_size` / `max_overflow` absent from code block | BLOCKING | RESOLVED | `pool_size=10, max_overflow=5` added to the `create_engine(...)` call in Task 3.1 Step 3. Phase 3 acceptance criteria checklist now includes both values. |
| F3 — CAE egress IP discovered too late (Phase 4) | BLOCKING | RESOLVED | Task 1.4 added to Phase 1: looks up `cae-mom-bot` `properties.staticIp`, adds `caeEgressIp` param to `postgres.bicep`, emits a named `allow-cae-egress` firewall rule. Phase 1 acceptance criteria updated. R6 updated to "MITIGATED (Phase 1)". Phase 4 step downgraded to a sanity-check verification. |
| F4 — Charge 12 mini-spike inside Phase 4 (no fallback) | BLOCKING | RESOLVED | Mini-spike workflow (`mini-spike-postgres-oidc.yml`) moved to Phase 3 Task 3.3 as a required deliverable. Phase 4 entry criteria now include "Charge 12 mini-spike has passed." Phase 4 Task 4.1 note updated to reflect pre-condition status. § 10 `unverified:` entry updated. |
| F5 — SQLite 3.38 constraint | CONCERN | RESOLVED | Task 2.1 Step 2 added: verify `sqlite3.sqlite_version ≥ 3.38` on CI runner; if below, use dialect-branch fallback; note that `test_alembic_postgres.py` is the authoritative enforcement regardless. |
| F6 — `Base` export preservation | CONCERN | RESOLVED | Explicit preservation note added to Task 3.1 Step 3 header. Task 3.1 restructured around `db/__init__.py` with `Base` co-located. |
| F7 — bare `pip install` in deploy workflow | CONCERN | RESOLVED | Phase 4 Task 4.1 Step 1 replaces `pip install --quiet '...'` block with `astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b` (v8.1.0 — commit SHA, not tag object) + `uv sync --frozen --no-dev` + `uv run alembic upgrade head`. SHA verified via `gh api repos/astral-sh/setup-uv/git/ref/tags/v8.1.0` (commit type, not annotated tag). |
| F8 — `Microsoft.DBforPostgreSQL` provider registration absent | CONCERN | RESOLVED | Added to Phase 1 prerequisites as an idempotent `az provider register -n Microsoft.DBforPostgreSQL --wait` step. |
| F9 — `pool_recycle` mid-query assumption undocumented | CONCERN | RESOLVED | Task 3.1 Step 3 module docstring (in `db/__init__.py`) now documents that `pool_recycle` only invalidates connections on checkout; depends on session-per-tick pattern; any session-lifetime change is a re-evaluation trigger. |
| F10 — `alembic.ini` / `migrations/` sourced from `actions/checkout` | CONCERN | RESOLVED | Phase 4 Task 4.1 Step 1 now includes a dependency note: `alembic.ini` and `migrations/` come from the `actions/checkout` step; removing it would break the migration; workflow checkout must be pinned to the same commit SHA being deployed. |
| F11 — `test_alembic.py:343-376` docstring stale | NIT | RESOLVED | Task 2.3 Step 3 added: update docstring of `test_fire_time_utc_check_rejects_nonzero_seconds` to note constraint expression is now `EXTRACT(SECOND FROM fire_time_utc) = 0`. |
| F12 — Phase 5 `git ls-tree` criterion ordering | NIT | RESOLVED | Ordering note added after Phase 5 Task 5.1 Step 7: the `git ls-tree main -- infra/modules/storage.bicep` empty criterion is only true after the Phase 5 PR merges. |
| F13 — testcontainers URL string-replace fragility | NIT | RESOLVED | Task 2.3 Step 1 fixture `postgres_url` rewritten to build URL explicitly from `pg.get_container_host_ip()`, `pg.get_exposed_port(5432)`, `pg.username`, `pg.password`, `pg.dbname`. |
| Implicit-1 — missing `touches:` frontmatter | IMPLICIT | RESOLVED | YAML frontmatter block added at top of file with `touches:` (all planned files verified in repo or marked as planned new) and `skills_relevant:`. |
| Implicit-2 — `test_migrations_startup.py` omitted from Phase 3 cleanup | IMPLICIT | RESOLVED | Task 3.2 Step 3 updated to delete `tests/test_migrations_startup.py` (all four tests patch `mom_bot.main.run_migrations` and become dead after Phase 3). `git rm` added to Task 3.2 Step 6 commit command. |

### Devops review reconciliation — 2026-05-17

Devops verdict: **0 BLOCKING, 3 CONCERN, 3 NIT — recommend proceeding to implementation.** This is the intended final plan revision before merge.

| Finding | Severity | Status | Resolution |
|---------|----------|--------|------------|
| C-1 — CAE `staticIp` drift risk | CONCERN | RESOLVED | R11 added to risk register: likelihood Low / impact High; detection via `pg_hba.conf reject` log alerts; remediation via `az containerapp env show` + bicepparam update + redeploy; long-term fix is workload-profiles + NAT gateway (out of scope — CAE network type immutable). MS Learn citation: [Container Apps networking — outbound IP addresses](https://learn.microsoft.com/azure/container-apps/networking#http-edge-proxy-behavior) (fetched 2026-05-17). |
| C-2 — drop `pool_size` to 5 | CONCERN | RESOLVED | `pool_size=10` → `pool_size=5` in Task 3.1 Step 3 `create_engine(...)` code block. R9 mitigation text updated: B1ms user-accessible ceiling is 35 (Azure reserves 15); deploy-window worst-case = 22/35 (halved vs prior design). Phase 3 acceptance criteria updated. Rationale paragraph added near `create_engine` block. MS Learn citation: [Postgres limits](https://learn.microsoft.com/azure/postgresql/configure-maintain/concepts-limits#maximum-connections) (fetched 2026-05-17). |
| C-3 — single-SP blast-radius risk | CONCERN | RESOLVED | R12 added to risk register: `mom-bot-gha` holds Subscription Contributor + Postgres Entra admin on one FIC; likelihood Low / impact Critical. Accepted-known for #91; two-FIC split tracked as issue #103 (`mom-bot-gha-deploy` + `mom-bot-gha-migrate`). Interim hardening: `main` branch protection + PR-required workflow edits. |
| N-1 — `pool_pre_ping=True` | NIT | RESOLVED | `pool_pre_ping=True` added to `create_engine(...)` kwargs alongside `pool_recycle=4800` in Task 3.1 Step 3. Rationale added: pessimistic-disconnect-handling catches token expiry, server failover, and network flaps — strictly more robust than `pool_recycle` alone. Phase 3 acceptance criteria updated. SQLAlchemy docs [Disconnect Handling - Pessimistic](https://docs.sqlalchemy.org/en/20/core/pooling.html#disconnect-handling-pessimistic) cited in module docstring. |
| N-2 — Container Apps Jobs as future option | NIT | RESOLVED | Note added to Phase 4 Task 4.1 (after the GHA OIDC note, before Step 2): Container Apps Jobs is a viable reach pattern if migrations grow; adds two moving parts; rejected for #91 in favor of simpler CI-side approach. |
| N-3 — `--resource` / `--resource-type` consistency | NIT | RESOLVED | All occurrences of `az account get-access-token --resource-type oss-rdbms` normalized to `--resource https://ossrdbms-aad.database.windows.net` (more explicit; matches spike's actual command). Updated: Q2 trade-off text, Modified files list, Phase 1 Task 1.3 smoke-test, Phase 2 Task 2.1 Step 4, Phase 4 Task 4.1 deploy.yml block. The mini-spike (Task 3.3) and Phase 4 entry criteria were already correct. |
