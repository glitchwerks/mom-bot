# PostgreSQL Migration Epic (#91) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the failed SQLite-on-AzureFile stopgap with Azure Database for PostgreSQL Flexible Server as the durable persistence layer for mom-bot, in shippable increments with independent rollback per phase.

**Architecture:** A new Bicep module provisions a Burstable B1ms Postgres Flexible Server with **public endpoint + firewall (Azure-services rule + GHA runner ephemeral rule)** and **Microsoft Entra ID-only authentication**. The existing `mi-mom-bot` user-assigned managed identity is promoted to Entra admin on the server and used by the Container App to acquire AAD tokens (24h lifetime) as the Postgres password. Schema is applied by an `alembic upgrade head` step in the deploy workflow, not by the bot at startup. AzureFile storage is removed entirely. The single env-var `MOM_BOT_DATABASE_URL` shape is retained.

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

**Decision: Public endpoint + firewall rules.** Specifically: enable the "Allow public access from Azure services" rule (firewall rule `AllowAllAzureServicesAndResourcesWithinAzureIps`, source `0.0.0.0`) plus a transient rule added by the deploy workflow for the GHA runner's egress IP (removed at job end).

**Reasoning:**
- The Container App today runs in the **default (non-VNet) Container Apps Environment** — see `infra/modules/containerapp.bicep:1-253` (no `vnetConfiguration` block). Switching to private Postgres would require:
  - Creating a VNet with at least two `/27` subnets (one for the CAE — workload-profiles minimum per [Container Apps networking](https://learn.microsoft.com/en-us/azure/container-apps/networking#environment-selection) (fetched 2026-05-16), one delegated to `Microsoft.DBforPostgreSQL/flexibleServers` minimum `/28` per [Postgres private networking](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private#virtual-network-concepts) (fetched 2026-05-16)),
  - Migrating the existing CAE to a workload-profiles VNet-injected environment (a destructive recreate — "Once you create an environment with either the default Azure network or an existing VNet, the network type can't be changed" per the same doc),
  - Creating + linking a Private DNS zone ending in `.postgres.database.azure.com`,
  - Adding NSG rules for outbound port 5432 + Microsoft Entra service-tag traffic.
- Cost and complexity: this is a non-trivial infra change touching #93 (network ACLs), well beyond the stated scope of #91.
- Public-endpoint + firewall + AAD-only auth + TLS gives a reasonable security profile: no password to leak, public network blocked except for Azure-internal IPs, no extra cost.
- Issue #93 (networkAcls) can be addressed against the Postgres firewall surface in a separate later epic without blocking this work.

**Citation:** [Postgres private networking concepts](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private) (fetched 2026-05-16) §§ "Virtual network concepts", "Unsupported virtual network scenarios" — confirms the subnet delegation requirement, `/28` minimum, and the irreversible CAE network choice. `infra/modules/containerapp.bicep:1-253` (current state — no `vnetConfiguration`).

### Q2. Auth mode: AAD-token auth vs. password in KV

**Decision: Microsoft Entra ID authentication only (no password in KV).** Concretely:
- Provision the server with `authConfig.passwordAuth = 'Disabled'` and `authConfig.activeDirectoryAuth = 'Enabled'`.
- Assign the user-assigned managed identity `mi-mom-bot` (created in `infra/modules/managed-identity.bicep`) as the **Entra admin** on the server via the `administrators` child resource (`Microsoft.DBforPostgreSQL/flexibleServers/administrators`).
- The bot acquires an AAD token for audience `https://ossrdbms-aad.database.windows.net` via `ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)` (the same pattern already used in `src/mom_bot/secrets.py` per PR #84/#86) and passes the token as the Postgres password on each connection.
- The KV secret `prod-database-url` becomes a **passwordless** DSN of the form `postgresql+psycopg://mi-mom-bot@<server>.postgres.database.azure.com/mom_bot?sslmode=require`. The password is injected at connect-time by a SQLAlchemy `do_connect` event handler that fetches a fresh token (tokens are valid up to 24h for managed identities per [Entra concepts FAQ](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts#frequently-asked-questions) (fetched 2026-05-16); we fetch on each connect to stay well inside the window).

**Reasoning:**
- This is the established pattern in this repo. The same `mi-mom-bot` UAMI already auths to Key Vault via `ManagedIdentityCredential` (PR #84 commit `8b0e10a` — "pass AZURE_CLIENT_ID to ManagedIdentityCredential"). Reusing that identity for Postgres means **zero new secrets to rotate**, **zero new principals**.
- AAD admin can be a user-assigned managed identity directly per [Entra concepts](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts) (fetched 2026-05-16) §§ "Differences between a PostgreSQL administrator and a Microsoft Entra administrator" ("The Microsoft Entra administrator can be a Microsoft Entra user, Microsoft Entra group, service principal, or managed identity").
- Password-in-KV would add a rotation burden, a leak surface, and a secret to manage in `infra/aad-runbook.md` — for no functional gain.

**Trade-off / known sharp edge:** Alembic CLI run from the GHA runner also needs a token. The GHA service principal (`mom-bot-gha`) must also be added as an Entra admin (multiple Entra admins are supported per the same FAQ: "you can set as many Microsoft Entra administrators as you want"). The deploy workflow uses `az account get-access-token --resource-type oss-rdbms` to mint the token and injects it as `PGPASSWORD`.

**Citation:** [Microsoft Entra Authentication for PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts) (fetched 2026-05-16). PR #84, PR #86 (`mi-mom-bot` + `AZURE_CLIENT_ID` pattern).

### Q3. Data migration approach: drain-and-cutover vs. dual-write

**Decision: No migration. Schema-only cutover.**

**Reasoning:** The bot has been failing on first write since the SQLite-on-AzureFile attempt (issue #91 status section confirms "first write hangs indefinitely on fsync over SMB"). There is no production data to preserve. The reminders table is repopulated on bot start by the seed function (`src/mom_bot/reminders/seed.py:225-311` — idempotent on empty DB). The `member_role_sync_state` table accumulates per-member idempotency state that is regenerated naturally as members are re-synced. The `day_role_map` table is seeded by `src/mom_bot/roles/seed.py`.

No dual-write infrastructure, no migration script, no cutover dance. **Skip the question entirely.**

**Verification step before declaring "no data to migrate":** in Phase 4 Task 4.1, the operator (router or human) must run an `az storage file list` against the existing `mom-bot-data` share to confirm there is no `.db` file with non-trivial content. If a populated `.db` file is present, halt and convert this section to a real data-migration plan.

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
- `migrations/versions/0003_postgres_check_constraint_portability.py` — drop SQLite-specific `strftime` CHECK, add Postgres-compatible `EXTRACT` CHECK.
- `src/mom_bot/db.py` — SQLAlchemy engine factory with AAD-token `do_connect` event hook. **New module** — extracts `_build_session_factory` from `main.py` so the token-injection logic lives separately and is unit-testable. (Engine factory is fewer than 60 LOC; this is a focused responsibility split, not bloat.)
- `tests/test_db_token_injection.py` — verifies the AAD-token hook is invoked on connect and stamps `connection.password` from the credential.

### Modified files

- `infra/main.bicep` — instantiate `postgres` module; remove `storage` module instantiation; pass Postgres FQDN to `containerapp.bicep`.
- `infra/main.bicepparam` — add Postgres admin object IDs (UAMI + GHA SP).
- `infra/modules/containerapp.bicep` — strip storage binding (lines 120-131), volumes (166-173), volumeMounts (201-205); update `database-url` secret reference (KV secret already exists; only the value changes).
- `infra/aad-runbook.md` — replace the SQLite-on-SMB policy section with the Postgres Entra-admin runbook step; update `prod-database-url` example.
- `src/mom_bot/main.py` — replace `_build_session_factory` with import from new `db` module; remove `run_migrations()` and its call site in `setup_hook`; remove the alembic Python-API import.
- `pyproject.toml` — add `psycopg[binary]>=3.2` to `dependencies`.
- `migrations/versions/0002_reminders_schema.py` — **leave as-is**. The new 0003 migration supersedes the broken CHECK. Editing 0002 retroactively would break the local-dev SQLite path (`tests/test_alembic.py` validates the SQLite path).
- `tests/test_alembic.py:64` — update the assertion to validate the new constraint name on 0003 (existing line 64 asserts the strftime CHECK name; needs to follow the constraint through both upgrades).
- `.github/workflows/deploy.yml` — add steps: install Python+`uv`+`psycopg[binary]`+`alembic`, mint AAD token via `az account get-access-token --resource-type oss-rdbms`, add transient firewall rule for runner IP, run `alembic upgrade head`, remove firewall rule.
- `README.md` — update the Epic 0 / Alembic section to reflect Postgres prod + SQLite local-dev.
- `docs/secrets-inventory.md` — update `prod-database-url` description (passwordless DSN, not SQLite path).

### Files deleted

- `infra/modules/storage.bicep` — entire file.

---

## 4. Phases & Tasks

Each phase produces a separately-mergeable PR. Each phase has a rollback path that does not require touching the prior phase.

---

### Phase 1 — Provision Postgres (additive, dark)

**Goal:** Postgres Flexible Server exists in the `mom-bot` resource group, with firewall + AAD admin configured. Nothing connects to it yet.
**Entry criteria:** PR for this plan is merged. Branch off `main`.
**Exit criteria:** `az postgres flexible-server show -g mom-bot -n <name>` returns `state: Ready`. `az postgres flexible-server execute -n <name> --admin-user <uami-client-id> --querytext "select 1"` succeeds from a developer laptop (with token).
**Rollback:** `az resource delete` the Postgres server. Nothing downstream depends on it yet.

#### Task 1.1: Author `postgres.bicep`

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
// Networking: Public access + firewall. Rule "AllowAzureServices" (0.0.0.0)
//   permits the Container App (which runs from Azure-internal egress IPs in
//   the default CAE). Deploy workflow adds a transient runner-IP rule for
//   migration runs. See Q1 decision in the plan.

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
      backupRetentionDays: 7
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

// Firewall: allow Azure-internal traffic (covers Container App egress in the
// default-network CAE). 0.0.0.0/0.0.0.0 is Azure's special "AllowAllAzureServices"
// firewall rule shape per the Postgres firewall docs.
resource fwAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: pg
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
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

#### Task 1.2: Wire `postgres` module into `main.bicep` (provision-only, no consumers yet)

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

module postgres 'modules/postgres.bicep' = {
  name: 'deploy-postgres'
  scope: rg
  params: {
    location: location
    tenantId: tenantId
    managedIdentityPrincipalId: identity.outputs.principalId
    managedIdentityName: managedIdentityName
    ghaServicePrincipalObjectId: ghaServicePrincipalObjectId
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
Expected: prints `PostgreSQL 16.x`. **Note:** the operator's AAD account must also be added as an Entra admin for this manual smoke test (one-time `az postgres flexible-server ad-admin create ...`); the Bicep module only adds the UAMI and GHA SP.

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

---

### Phase 2 — Schema portability (validate Alembic against Postgres)

**Goal:** `alembic upgrade head` runs cleanly against the new Postgres instance from an operator laptop. The strftime CHECK constraint bug in `0002_reminders_schema.py` is fixed by a new 0003 migration (no edit-in-place of 0002, to preserve SQLite local-dev compatibility).
**Entry criteria:** Phase 1 merged. Operator has token-based psql access.
**Exit criteria:** `alembic upgrade head` against Postgres returns success and `\dt` shows `reminders`, `reminder_sent`, `day_role_map`, `member_role_sync_state`, `alembic_version`. `pytest tests/test_alembic.py -v` still passes against SQLite.
**Rollback:** Drop the public schema (`drop schema public cascade; create schema public;`) and re-run.

#### Task 2.1: Author 0003 migration — replace strftime CHECK

**Files:**
- Create: `migrations/versions/0003_postgres_check_constraint_portability.py`

- [ ] **Step 1: Write the migration**

```python
"""Replace SQLite-specific strftime() CHECK with portable EXTRACT() form.

The constraint in 0002_reminders_schema enforces "fire_time_utc must be on
the minute" via ``CAST(strftime('%S', fire_time_utc) AS INTEGER) = 0`` —
SQLite-only syntax. Postgres needs EXTRACT(SECOND FROM fire_time_utc) = 0.

We drop and re-add the CHECK so both backends end up with equivalent
semantics. Tests that grep for the constraint name should look for the new
name ``ck_fire_time_no_seconds_v2``.

Revision ID: 0003_pg_check_portability
Revises: b2_member_role_sync_state
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_pg_check_portability"
down_revision: str | Sequence[str] | None = "b2_member_role_sync_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("reminders") as batch:
        # batch_alter_table handles SQLite's "drop constraint" limitation by
        # rebuilding the table; on Postgres it's a no-op wrapper.
        batch.drop_constraint("ck_fire_time_no_seconds", type_="check")
        if dialect == "postgresql":
            batch.create_check_constraint(
                "ck_fire_time_no_seconds_v2",
                "EXTRACT(SECOND FROM fire_time_utc) = 0",
            )
        else:
            # SQLite local-dev path — keep the strftime form, just rename.
            batch.create_check_constraint(
                "ck_fire_time_no_seconds_v2",
                "CAST(strftime('%S', fire_time_utc) AS INTEGER) = 0",
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("reminders") as batch:
        batch.drop_constraint("ck_fire_time_no_seconds_v2", type_="check")
        if dialect == "postgresql":
            batch.create_check_constraint(
                "ck_fire_time_no_seconds",
                "EXTRACT(SECOND FROM fire_time_utc) = 0",
            )
        else:
            batch.create_check_constraint(
                "ck_fire_time_no_seconds",
                "CAST(strftime('%S', fire_time_utc) AS INTEGER) = 0",
            )
```

- [ ] **Step 2: Update `tests/test_alembic.py:64`** to assert the v2 name.

```python
# tests/test_alembic.py — find the existing assertion on line ~64 that looks
# for "ck_fire_time_no_seconds" and update to "ck_fire_time_no_seconds_v2".
# Surrounding context unchanged.
assert "ck_fire_time_no_seconds_v2" in constraint_names
```

- [ ] **Step 3: Run SQLite-side tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_alembic.py -v
```
Expected: PASS — the new migration runs against SQLite via the `batch_alter_table` + strftime branch.

- [ ] **Step 4: Run Postgres-side migration from operator laptop**

```powershell
$token = az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv
$env:PGPASSWORD = $token
$fqdn = az postgres flexible-server show -g mom-bot --name pg-mombot-XXXXXX --query fullyQualifiedDomainName -o tsv
$env:MOM_BOT_DATABASE_URL = "postgresql+psycopg://<your-aad-upn>@${fqdn}:5432/mom_bot?sslmode=require"
.\.venv\Scripts\python.exe -m alembic upgrade head
```
Expected: each revision prints `Running upgrade ...` and exits 0. **First**, you'll need to install `psycopg[binary]` — Task 2.2 below adds it to `pyproject.toml`.

- [ ] **Step 5: Verify schema**

```powershell
psql "host=$fqdn port=5432 dbname=mom_bot user=<your-aad-upn> sslmode=require" -c "\dt"
```
Expected: lists `alembic_version`, `day_role_map`, `member_role_sync_state`, `reminder_sent`, `reminders`.

#### Task 2.2: Add `psycopg[binary]` dependency

**Files:**
- Modify: `pyproject.toml:10-20`

- [ ] **Step 1: Add the dependency**

```toml
dependencies = [
    "discord.py>=2.4",
    "aiohttp>=3.9",
    "pydantic>=2",
    "sqlalchemy>=2",
    "alembic",
    "azure-identity>=1.17",
    "azure-keyvault-secrets>=4.8",
    "fastapi>=0.111,<1.0",
    "httpx>=0.27,<1.0",
    "psycopg[binary]>=3.2",
]
```

- [ ] **Step 2: Reinstall in venv**

```powershell
uv pip install -e ".[dev]"
```

- [ ] **Step 3: Run full test suite to verify no regressions**

```powershell
.\.venv\Scripts\python.exe -m pytest
```
Expected: all existing tests PASS.

- [ ] **Step 4: Commit and open Phase 2 draft PR**

```bash
git add migrations/versions/0003_postgres_check_constraint_portability.py tests/test_alembic.py pyproject.toml uv.lock
git commit -m "feat(db): portable CHECK constraint + psycopg dep for Postgres (#91)"
git push
gh pr create --draft --title "feat(db): schema portability for Postgres (Phase 2 of #91)" --body-file <body>
```

**Acceptance criteria for Phase 2:**
- [ ] `pytest tests/test_alembic.py` passes (SQLite path).
- [ ] `alembic upgrade head` runs cleanly against the live Postgres instance.
- [ ] `\dt` shows all four app tables + `alembic_version`.

---

### Phase 3 — Application wiring (AAD-token engine, remove startup migrations)

**Goal:** The bot's SQLAlchemy engine acquires an AAD token on connect; `run_migrations()` is removed from `setup_hook`. The bot does not yet point at Postgres in prod — that's Phase 4.
**Entry criteria:** Phase 2 merged.
**Exit criteria:** Local `pytest` passes; new `tests/test_db_token_injection.py` verifies the token hook fires; `MomBot.setup_hook` no longer calls `run_migrations`.
**Rollback:** Revert the PR. Local dev path (SQLite, no token) must still work — the token hook must be a no-op when the DSN scheme is `sqlite://`.

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
configured user-assigned managed identity on every connect and stamped as the
``password`` connect parameter. Tokens are valid up to 24h for managed
identities per
https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts
(fetched 2026-05-16); fetching on each connect keeps us well inside the
window without needing token-refresh bookkeeping.

For non-Postgres URLs (sqlite, used in unit tests and local dev), the hook is
not registered.
"""

from __future__ import annotations

import os

from azure.identity import ManagedIdentityCredential
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_OSSDB_AAD_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def build_session_factory(
    db_url: str,
    *,
    aad_client_id: str | None = None,
) -> sessionmaker[Session]:
    """Build a session factory; for Postgres URLs, inject AAD token on connect.

    Args:
        db_url: SQLAlchemy URL. ``postgresql+psycopg://...`` triggers AAD-token
            injection; anything else (notably ``sqlite://``) is opened with no
            password injection.
        aad_client_id: Client ID of the user-assigned managed identity to use
            for token acquisition. Required when ``db_url`` is Postgres.
            Defaults to ``$AZURE_CLIENT_ID`` when not provided.

    Returns:
        A sessionmaker bound to the configured engine.
    """
    engine: Engine = create_engine(db_url, echo=False)

    if db_url.startswith(("postgresql://", "postgresql+psycopg://")):
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
git commit -m "feat(db): AAD-token engine factory for Postgres (#91)"
```

#### Task 3.2: Swap `main.py` to use new factory; remove `run_migrations`

**Files:**
- Modify: `src/mom_bot/main.py` (lines 37, 54, 76-105, 130-149 — multiple edits)

- [ ] **Step 1: Replace `_build_session_factory` body with import + delegation**

Edit `main.py:130-149` to:

```python
from mom_bot.db import build_session_factory as _build_session_factory  # noqa: F401

_DEFAULT_DB_URL = "sqlite:///./mom-bot.db"


def _resolve_db_url() -> str:
    return os.environ.get("MOM_BOT_DATABASE_URL", _DEFAULT_DB_URL)
```

Then update every previous caller of the old `_build_session_factory()` to pass `_resolve_db_url()` as the first arg. (Grep for `_build_session_factory(` to find them — typically one call site at module scope.)

- [ ] **Step 2: Remove `run_migrations` entirely**

Delete lines 76-105 (the `run_migrations` function). Find the call inside `MomBot.setup_hook` (search for `run_migrations()` in `main.py`) and delete that line. Also remove the unused imports:

```python
# Remove these lines from the top-of-file imports:
from alembic import command as alembic_command  # if present
from alembic.config import Config as AlembicConfig  # if present
from alembic.command import upgrade as alembic_upgrade  # if present
```

- [ ] **Step 3: Run the full suite**

```powershell
.\.venv\Scripts\python.exe -m pytest
```
Expected: all tests PASS. Notably `tests/test_main_wireup.py` should still work because it patches the env var with a SQLite URL (no AAD path triggered).

- [ ] **Step 4: Commit**

```bash
git add src/mom_bot/main.py
git commit -m "refactor(main): use shared db.build_session_factory; remove run_migrations (#91)"
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

---

### Phase 4 — Cutover (deploy workflow runs alembic; KV secret swap; revision restart)

**Goal:** Production runtime swings from broken-SQLite-on-AzureFile to working-Postgres. Done when `ca-mom-bot` is healthy on Postgres for at least one reminder tick.
**Entry criteria:** Phases 1-3 merged.
**Exit criteria:**
- `deploy.yml` runs `alembic upgrade head` successfully against prod Postgres.
- `prod-database-url` KV secret holds the Postgres DSN.
- `ca-mom-bot` revision is healthy; logs show reminder scheduler started + at least one DB query.
**Rollback:** Revert the KV secret to the old SQLite-on-SMB DSN and revert the workflow PR. Note: the bot was already broken pre-cutover, so "rollback to broken" is acceptable — the worst case is "still broken, but no worse than the last 12 hours."

#### Task 4.1: Verify no SQLite data exists worth preserving

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

- [ ] **Step 2: HALT condition — if any file with size > 1 KiB exists**, stop the cutover and convert this task into a real data-migration sub-plan (download, replay rows into Postgres). Do NOT proceed silently.

#### Task 4.2: Update `deploy.yml` to run `alembic upgrade head`

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
            'alembic' \
            'sqlalchemy>=2' \
            'psycopg[binary]>=3.2'

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

      - name: Run alembic upgrade head
        env:
          # AAD token for the OSS RDBMS audience — valid ~60 min for SP tokens
          # per Entra concepts FAQ (fetched 2026-05-16).
          MOM_BOT_DATABASE_URL: >-
            postgresql+psycopg://mom-bot-gha@${{ steps.pg.outputs.fqdn }}:5432/mom_bot?sslmode=require
        run: |
          PGPASSWORD=$(az account get-access-token \
            --resource-type oss-rdbms \
            --query accessToken -o tsv)
          export PGPASSWORD
          # Inject token via env var that psycopg picks up; SQLAlchemy URL
          # has no password (avoid leaking it via URL encoding).
          # Alembic's env.py reads MOM_BOT_DATABASE_URL directly.
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

**Note on `PGPASSWORD` injection:** the runtime `db.py` injects the token via the SQLAlchemy `do_connect` event. For the CI alembic run, the equivalent is `PGPASSWORD` env var (psycopg honors it natively). Alembic's `env.py` consumes `MOM_BOT_DATABASE_URL` which carries no password; psycopg picks up `PGPASSWORD` from the env. **Verify before merging:** confirm psycopg3 respects `PGPASSWORD` when the URL has no password component. If not, an alternative is a small Python wrapper script that mints the token and passes it to `sqlalchemy.create_engine(...)` directly. (See § 5 Risk R3.)

- [ ] **Step 2: Lint the workflow**

```powershell
# actionlint if available; otherwise skip and rely on the PR check.
```

#### Task 4.3: Swap KV secret value

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

Note: `mi-mom-bot` is the **UAMI display name** (= Postgres role name). Postgres-AAD matches by the role-name + token tenant + object-ID combination; the UAMI display name must equal the Entra admin "principalName" set in `postgres.bicep`.

- [ ] **Step 2: Verify Container App picks up the new secret**

KV secret references in Container Apps are resolved at revision-create time, not poll-based. A revision update is required (which Step 3 forces via image redeploy).

#### Task 4.4: Trigger deploy and verify

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

- [ ] **Step 1: `aad-runbook.md`** — replace the "SQLite-on-SMB policy" section with a new "Postgres AAD-admin setup" section. Update the `prod-database-url` example to the new passwordless DSN.

- [ ] **Step 2: `README.md`** — update the Epic 0 baseline section: prod uses Postgres; local dev still uses SQLite. Update the Alembic section: migrations are CI-applied in prod, manually run in dev.

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
network ACL hardening is the Postgres firewall (currently
AllowAllAzureServices). Re-scope or close.
```

- [ ] **Step 3: Update #94 (startup migrations)** with comment / close:

```
Resolved by #91 Phase 3 — run_migrations() removed from setup_hook;
migrations are now applied by the deploy workflow per #94's eventual
recommendation. Closing.
```

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
- [ ] #91, #90, #94 closed; #93 commented; #83 commented.
- [ ] This plan file deleted per `CLAUDE.md § Document Files / Lifecycle`.

---

## 5. Risks & Mitigations

| ID | Risk | Likelihood | Impact | Mitigation |
|----|------|-----------|--------|------------|
| R1 | SQLite share has unexpected data | Very low | Medium (silent data loss) | Task 4.1 mandates inspection of the share before cutover; HALT condition on any file > 1 KiB. |
| R2 | psycopg3 ignores `PGPASSWORD` when DSN has no password component | Low | High (deploy fails) | Pre-flight test locally before merging Phase 4: empty-password URL + `PGPASSWORD` env var + `psycopg.connect()`. If it doesn't work, fall back to a small Python wrapper (mint token, build URL with embedded URL-encoded token, call `alembic.command.upgrade` programmatically). |
| R3 | AAD admin assignment race — Postgres provisioned but `administrators` resource fails | Low | Medium (server orphaned, no one can connect) | Bicep resource ordering: `administrators` declares `parent: pg`, so it's implicitly ordered after server creation. If the AAD admin resource fails, redeploy is idempotent. Worst case: manually run `az postgres flexible-server ad-admin create`. |
| R4 | Burstable B1ms credit exhaustion under unexpected load | Medium | Medium (transient connection failures during credit-empty windows per [concepts-compute](https://learn.microsoft.com/en-us/azure/postgresql/compute-storage/concepts-compute) (fetched 2026-05-16)) | Set up an Azure Monitor alert on `CPU Credits Remaining < 30` post-Phase 4 (track in a follow-up issue, not blocking #91). For a Discord bot's load profile this is very unlikely. |
| R5 | KV secret swap and revision restart out of order — bot starts on old secret | Low | Low (one revision restart fixes it) | KV secret references resolve at revision-create time; the deploy workflow's `az containerapp update` creates a new revision so the swap is automatic. Verify in Task 4.4 Step 4. |
| R6 | Container App egress IP not covered by `AllowAllAzureServicesAndResourcesWithinAzureIps` firewall rule | Low | High (bot can't connect) | The `0.0.0.0` firewall rule explicitly covers "Azure services" egress including Container Apps in the default network. If it fails, the fallback is to add `Microsoft.DBforPostgreSQL` Private Link or to explicitly whitelist the Container App's static outbound IP (visible in CAE properties — `staticIp`). |
| R7 | unverified: AAD admin assignment in Bicep requires the `Microsoft.Graph` resource provider registered in the subscription | Low | Medium | If Phase 1 deploy fails on the `administrators` resource with a Graph-related error, register the provider: `az provider register --namespace Microsoft.Graph`. (Could not verify in docs in available time; flagging as `unverified:` per `CLAUDE.md § Cite Sources in Planning Artifacts`.) |

---

## 6. Cross-Issue Impact

| Issue | Status post-#91 | Action |
|-------|-----------------|--------|
| #90 — AzureFile snapshot automation | Superseded (Postgres PITR replaces it) | Close in Task 5.3 |
| #93 — networkAcls | Re-scoped (storage gone; Postgres firewall is new surface) | Comment + leave open for separate decision |
| #94 — startup-migration topology | Resolved (Phase 3 removes `run_migrations` from `setup_hook`) | Close in Task 5.3 |
| #83 — deploy workflow runs Bicep | Partially addressed (alembic step added; Bicep apply still TODO) | Comment in Task 5.3 |
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
| Phase 2 — Schema portability | Small (one migration + test update) |
| Phase 3 — Application wiring | Medium (new module + test + main.py surgery) |
| Phase 4 — Cutover | Medium (workflow surgery + live verification) |
| Phase 5 — Cleanup | Small (deletions + doc updates + issue triage) |
| **Total** | **Small-Medium** — 1-2 focused days of work for a single contributor, spread across at least two calendar days to allow ≥ 24h soak between Phase 4 and Phase 5. |

---

## 9. Sources Index

All Microsoft Learn URLs fetched 2026-05-16.

- [Compute Options — Azure DB for PostgreSQL Flexible Server](https://learn.microsoft.com/en-us/azure/postgresql/compute-storage/concepts-compute) — Burstable B1ms specs (1 vCore, 2 GiB, 640 IOPS); "for nonproduction" warning; CPU-credit semantics.
- [Microsoft Entra Authentication for PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/security/security-entra-concepts) — UAMI as Entra admin supported; token lifetime up to 24h for managed identities; multiple admins supported.
- [Networking with Private Access — Azure DB for PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private) — subnet delegation requirement (/28 min), private DNS zone requirements.
- [Networking in Azure Container Apps environment](https://learn.microsoft.com/en-us/azure/container-apps/networking) — environment network type is immutable post-create; workload-profiles /27 subnet minimum.
- [Azure file share soft delete](https://learn.microsoft.com/en-us/azure/storage/files/storage-files-prevent-file-share-deletion) — 7-day default retention; deletion succeeds immediately; purge procedure.
- Repo refs: `infra/modules/storage.bicep:1-78`, `infra/modules/containerapp.bicep:120-131,166-173,201-205`, `infra/main.bicep:82-94,113`, `src/mom_bot/main.py:76-105,134-149`, `migrations/versions/0002_reminders_schema.py:64`, `migrations/versions/b2_member_role_sync_state.py`, `pyproject.toml:10-20`, `.github/workflows/deploy.yml`, `tests/test_alembic.py`.
- GitHub refs: #91 (this epic), #90 (snapshot — superseded), #93 (networkAcls — rescoped), #94 (startup migrations — resolved by Phase 3), #83 (deploy workflow — partially addressed), #87 / #92 (SQLite stopgap PRs — reverted in effect), #84 / #86 (UAMI + AZURE_CLIENT_ID pattern — reused), #95 (added run_migrations — removed by Phase 3), commit `8b0e10a` (PR #84/#86 fix).

### Items marked `unverified:`

- R7 — Microsoft.Graph provider registration requirement for `administrators` resource on Postgres Flexible Server.
- #96 current state — needs check at Phase 5 time (could not verify open/closed status from this sub-agent context).
