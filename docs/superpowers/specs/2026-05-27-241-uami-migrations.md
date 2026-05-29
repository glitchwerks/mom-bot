---
title: "Spec — Run Postgres migrations as a Container Apps Job under mi-mom-bot"
issue: 241
parent_issue: 103
date: 2026-05-27
status: proposed
touches:
  - .github/workflows/deploy.yml
  - infra/modules/
  - infra/scripts/create-entra-admins.sh
  - infra/aad-runbook.md
skills_relevant:
  - azure
  - bicep
  - github-actions
---

# Spec — Run Postgres migrations as a Container Apps Job under `mi-mom-bot`

**Issue:** [glitchwerks/mom-bot#241](https://github.com/glitchwerks/mom-bot/issues/241)
**Blocks:** [glitchwerks/mom-bot#103](https://github.com/glitchwerks/mom-bot/issues/103)

---

## Recommendation

CONDITIONAL YES (high confidence) — if `replicaTimeout` sizing is confirmed acceptable and `mi-mom-bot` ACR pull authority is verified, running alembic migrations from a `Microsoft.App/jobs` resource bound to `mi-mom-bot` is a supported, WAF-endorsed pattern that collapses the planned two-SP cutover in #103.

---

## 1. Context

Issue #103 proposes splitting `mom-bot-gha` into two service principals — `mom-bot-gha-deploy` (infra/container push) and `mom-bot-gha-migrate` (Postgres DDL) — to reduce blast radius if a workflow token is compromised. That split requires creating a second SP, a second federated identity credential, a separate GHA environment, and additional secret management overhead.

Issue #241 was opened to investigate an alternative surfaced during devops review: instead of a second SP, run `alembic upgrade head` from a `Microsoft.App/jobs` resource authenticated via `mi-mom-bot` (the runtime UAMI). `mi-mom-bot` is already registered as a Postgres Entra admin (`infra/scripts/create-entra-admins.sh:L44`), lives in the same Container Apps Environment as `ca-mom-bot` (sharing outbound IPs), and can be triggered synchronously from GHA via `az containerapp job start`. If viable, this collapses the five-phase cutover in #103: the migrate concern moves to an Azure-native job, no second SP is created, and the `mom-bot-gha` SP's Postgres admin grant can be dropped without a parallel identity infrastructure change.

---

## 2. Current State

**How migrations run today** (`.github/workflows/deploy.yml:L100-L147`): the `migrate` job authenticates as `mom-bot-gha` via OIDC, shells into a Container Apps exec session on the running `ca-mom-bot` instance, and runs `alembic upgrade head` inside the live bot container. The job depends on `deploy` having completed first.

**Current Postgres Entra admins** (`infra/scripts/create-entra-admins.sh:L44`): both `mi-mom-bot` and `mom-bot-gha` are registered as Entra admins on the Postgres Flexible Server. Both hold full DDL/DML authority across all user databases on the server.

**What #103 proposes:** create `mom-bot-gha-migrate` as a second SP with a separate federated credential and GHA environment `prod-migrate`; grant it Postgres Entra admin; strip the Postgres admin grant from `mom-bot-gha`. See the #103 spec (`docs/superpowers/specs/2026-05-27-103-fic-split.md`) for the full five-phase plan.

---

## 3. Research Findings

### Q1 — `az containerapp job start --wait` semantics

The Azure CLI `az containerapp job start` command supports `--no-wait` to skip waiting; by default it waits for the long-running operation to finish, and the CLI process exits non-zero if the underlying job execution fails. The CLI does NOT stream the container's stdout/stderr to the caller — application logs are written to the Log Analytics workspace configured for the Container Apps Environment and must be fetched via `az monitor log-analytics query` after the job completes.

Citations:
- https://learn.microsoft.com/cli/azure/containerapp/job?view=azure-cli-latest (fetched 2026-05-27)
- https://learn.microsoft.com/azure/container-apps/jobs-get-started-cli#query-job-run-logs (fetched 2026-05-27)

Quotes:
- "`--no-wait` — Do not wait for the long-running operation to finish. Default value: False." (implies default behavior IS to wait)
- "Job runs write output logs to the logging provider that you configure for the Container Apps environment. By default, logs are stored in Log Analytics."

Note: Learn documents the `--no-wait` flag but does not explicitly document a `--wait` flag for `az containerapp job start`. In practice the LRO-polling behavior is the default; the GHA workflow should rely on the default (omit `--no-wait`) rather than passing `--wait`.

---

### Q2 — Container App Job + UAMI binding (Bicep)

The `Microsoft.App/jobs` resource accepts a top-level `identity` block with `type` set to `'UserAssigned'` (or `'SystemAssigned,UserAssigned'`) and a `userAssignedIdentities` dictionary keyed by the full ARM resource ID of each UAMI, with empty-object values. Schema is identical to `Microsoft.App/containerApps`.

Citations:
- https://learn.microsoft.com/azure/templates/microsoft.app/2024-03-01/jobs (fetched 2026-05-27) — `2024-03-01` is the current GA api-version
- https://learn.microsoft.com/azure/templates/microsoft.app/2025-10-02-preview/jobs (fetched 2026-05-27) — preview, adds `identitySettings` per-MI config
- https://raw.githubusercontent.com/Azure/bicep-registry-modules/refs/heads/main/avm/res/app/job/README.md (fetched 2026-05-27) — AVM example using `managedIdentities.userAssignedResourceIds` array form

Quote: "userAssignedIdentities … dictionary keys will be ARM resource ids in the form: '/subscriptions/{subscriptionId}/resourceGroups/{resourceGroupName}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{identityName}'. The dictionary values can be empty objects (`{}`) in requests."

---

### Q3 — Postgres Flexible Server Entra admin scope

A Microsoft Entra administrator on Postgres Flexible Server gets the same privileges as the original PostgreSQL administrator (effectively the server admin role; on Flexible Server this means membership in `azure_pg_admin`, which is granted DDL/DML over every non-system database on the server). It is the documented equivalent of the password-based server admin for AAD-mode servers — not literally PG superuser, but functionally equivalent for migrations: full DDL/DML across all user databases plus the ability to manage other Microsoft Entra roles. The admin can be a user, group, service principal, or managed identity.

Citations:
- https://learn.microsoft.com/azure/postgresql/security/security-entra-concepts (fetched 2026-05-27)
- https://learn.microsoft.com/azure/postgresql/security/security-access-control#role-management (fetched 2026-05-27)

Quotes:
- "When you turn on Microsoft Entra authentication for your Microsoft Entra principal as a Microsoft Entra administrator, the account: Gets the same privileges as the original PostgreSQL administrator. Can manage other Microsoft Entra roles on the server."
- "members of the *azure_pg_admin* role can manage roles and access objects owned by any nonrestricted role … providing a seamless and reliable experience without requiring superuser access."

Implication: `mi-mom-bot` (already registered as Entra admin per `infra/scripts/create-entra-admins.sh:L44`) can run `alembic upgrade head` against any database on the server with no additional grants.

---

### Q4 — AAD admin propagation delay

The propagation cost is one-time at admin-registration, not per-migration. Once `mi-mom-bot` is registered as an Entra admin (and the registration has propagated), every subsequent token-acquisition is fast and the only latency is Microsoft Entra access-token issuance for the managed identity. Token lifetime: user tokens up to 1 hour; managed-identity tokens up to 24 hours. The existing 60s sleep in `deploy.yml` is therefore needed only on the run that creates the admin assignment; subsequent migration runs (UAMI already admin) do not need it.

Citations:
- https://learn.microsoft.com/azure/postgresql/security/security-entra-concepts#frequently-asked-questions (fetched 2026-05-27)
- https://learn.microsoft.com/entra/identity-platform/access-tokens#token-lifetime (fetched 2026-05-27)

Quote: "User tokens are valid for up to 1 hour. Tokens for system-assigned managed identities are valid for up to 24 hours."

`unverified:` — Learn does not name a precise propagation interval for new admin assignments. The practical 30-60s wait is operational lore, not a published SLA. Treat any duration claim shorter than "the admin registration LRO has succeeded" as unverified.

---

### Q5 — Container App Job execution time limits

Each job execution runs replicas with a per-replica time cap controlled by `replicaTimeout` (seconds). The official Learn quickstart uses `--replica-timeout 1800` (30 min). The schema and SDK surface it as a 32-bit int (`int (required)`) — Learn does NOT publish a documented hard upper bound for `replicaTimeout` on the public jobs page; the sample value 1800 is the recommended/illustrative figure. Retry behavior: `replicaRetryLimit` controls retries on failure; `replicaTimeout` takes precedence if it expires before retries occur. Parallelism is configurable but defaults to `1`. For a single alembic upgrade, set `parallelism: 1`, `replicaCompletionCount: 1`.

Citations:
- https://learn.microsoft.com/azure/container-apps/jobs#advanced-job-configuration (fetched 2026-05-27)
- https://learn.microsoft.com/azure/container-apps/jobs-get-started-cli#create-a-container-apps-environment (fetched 2026-05-27)

Quote: "Replica retry limit … The maximum number of times to retry a failed replica. To fail a replica without retrying, set the value to `0`. The `replicaTimeout` setting takes precedence if it expires before all retries occur."

`unverified:` — exact upper bound for `replicaTimeout`. The Learn quickstart uses 1800s; community reports indicate the practical ceiling is on the order of hours, but no Learn page states a numeric maximum.

---

### Q6 — Container App Job logs to Log Analytics

Job stdout/stderr is captured in the `ContainerAppConsoleLogs_CL` Log Analytics table, partitioned by `ContainerGroupName_s` which starts with the execution name. The official Learn quickstart shows the exact GHA-friendly pattern: `az containerapp job execution list` to get the execution name, then `az monitor log-analytics query` with a KQL filter on `ContainerGroupName_s startswith '$JOB_RUN_NAME'`. There is ingestion lag — the table can return empty results for a few minutes after job completion; the docs explicitly call this out.

Citations:
- https://learn.microsoft.com/azure/container-apps/jobs-get-started-cli#query-job-run-logs (fetched 2026-05-27)
- https://learn.microsoft.com/azure/container-apps/log-monitoring (fetched 2026-05-27)

Quote: "Until the ContainerAppConsoleLogs_CL table is ready, the command returns no results, or it returns the following error: 'BadArgumentError: The request had some invalid properties.' In either case, wait a few minutes and then run the command again."

---

### Q7 — CAE outbound IPs vs Postgres firewall

All container apps and jobs within a single Container Apps Environment share the same virtual network and the same outbound IP set. The `Microsoft.App/jobs` resource exposes a read-only `outboundIpAddresses` property; by design these are sourced from the environment's managed public IP (or NAT Gateway, if attached). A job in the same CAE as `ca-mom-bot` will egress through the same IPs that are already allowlisted in `infra/modules/postgres.bicep`. No additional firewall rule is required.

Citations:
- https://learn.microsoft.com/azure/container-apps/environment (fetched 2026-05-27)
- https://learn.microsoft.com/azure/cloud-adoption-framework/scenarios/app-platform/container-apps/networking (fetched 2026-05-27)
- https://learn.microsoft.com/javascript/api/@azure/arm-appcontainers/jobproperties (fetched 2026-05-27)

Quote: "When multiple container apps are in the same environment, they share the same virtual network and write logs to the same logging destination."

Caveat: outbound IPs can change over time (per CAF doc). Whatever firewall key the project uses today is already subject to this drift.

---

### Q8 — WAF / Cloud Architect endorsement

The pattern is endorsed by the WAF Reliable Web App guidance (managed identities for all Azure services that support them; user-assigned MI preferred when multiple resources share the same permissions) and is the explicitly documented migration pattern for Heroku-style one-off tasks: "One-off tasks (`heroku run`) → Container Apps job with manual trigger." No Learn page calls "migrations as a Container App Job using runtime MI" an anti-pattern.

Citations:
- https://learn.microsoft.com/azure/architecture/web-apps/guides/enterprise-app-patterns/reliable-web-app/dotnet/guidance (fetched 2026-05-27)
- https://learn.microsoft.com/azure/container-apps/migrate-heroku-overview#cost-comparison (fetched 2026-05-27)
- https://learn.microsoft.com/azure/container-apps/jobs (fetched 2026-05-27) — Manual-job example explicitly cites "A one-time processing task like migrating data from one system to another"

Quotes:
- "Prefer user-assigned managed identities when you have two or more Azure resources that need the same set of permissions."
- "Avoid permanent elevated permissions. Use Microsoft Entra Privileged Identity Management (PIM) to grant just-in-time (JIT) access for privileged operations."

---

### Q9 — Job `triggerType`

`triggerType: 'Manual'`. The `JobConfiguration` schema requires either `manualTriggerConfig`, `scheduleTriggerConfig`, or `eventTriggerConfig` corresponding to the trigger type. For `Manual`, the `manualTriggerConfig` block is required but all of its fields are optional — `parallelism` and `replicaCompletionCount` both default to `1`. Minimal viable shape is `manualTriggerConfig: {}`.

Citations:
- https://learn.microsoft.com/azure/templates/microsoft.app/2024-03-01/jobs (fetched 2026-05-27)
- https://raw.githubusercontent.com/Azure/bicep-registry-modules/refs/heads/main/avm/res/app/job/README.md (fetched 2026-05-27)

Quote: "manualTriggerConfig — Manual trigger configuration for a single execution job. Properties replicaCompletionCount and parallelism would be set to 1 by default."

---

### Q10 — Image reuse with command override

Fully supported and documented. The `containers[]` element in the job template exposes `command: string[]` and `args: string[]` which override the image's ENTRYPOINT/CMD respectively — identical semantics to Docker. The Container Apps jobs documentation has a worked example showing `args: ['/bin/bash', '-c', 'echo "Hello, $MY_NAME!"']` overriding the same image used elsewhere.

Citations:
- https://learn.microsoft.com/azure/container-apps/jobs#start-a-job-execution-on-demand (fetched 2026-05-27)
- https://learn.microsoft.com/azure/container-instances/container-instances-start-command (fetched 2026-05-27)

Quote: "To override the job's configuration, include a template in the request body. The following example overrides the startup command to run a different command."

Pitfalls to verify locally (design judgment):
- mom-bot's image entrypoint must not silently start the bot app before the override gets a chance — if the Dockerfile uses `ENTRYPOINT ["python", "-m", "mom_bot"]`, the override needs to fully replace it via `command:` not just `args:`.
- `config.py` import side effects: if module import triggers Discord token loads or network calls, the migration job will fail at import time. Verify config loading is lazy enough that `uv run alembic upgrade head` does not pull in the Discord client.
- Alembic must read `DATABASE_URL` or equivalent from environment, and that env var (with `password=` substituted by the Entra access token) must be set as an `env:` on the job's container.

---

## 4. Top Risks

1. **`replicaTimeout` upper bound unverified.** `unverified:` — Learn quickstart uses 1800s as the sample value but does not publish a numeric hard ceiling. Propose 1800–3600s as the working range; verify against anticipated schema growth before committing.

2. **Log fetch lag breaks GHA UX.** `--wait` (the default LRO poll) blocks until execution completes but alembic stdout/stderr lands in Log Analytics asynchronously with a documented multi-minute ingestion lag. The GHA step will not show migration output inline; a second KQL-fetch step is required, and it may need a retry loop. This is a real downgrade from today's streaming exec model.

3. **Token acquisition inside the container.** A small `migrate.sh` (or `alembic env.py` extension) must fetch the token via `azure.identity.ManagedIdentityCredential` and set `PGPASSWORD` before invoking alembic. This is additional code that does not currently exist in the image.

4. **ACR pull authorization.** The job must pull the bot's existing image. `mi-mom-bot` UAMI needs `AcrPull` on the ACR — likely already true since `ca-mom-bot` runs under it, but must be verified. The job resource also needs `registries[].identity` set to the UAMI's resource ID.

5. **Token TTL vs migration duration.** Low risk — managed-identity tokens are valid up to 24 hours (Q4); alembic upgrades are expected to complete well within that window.

6. **Outbound IP drift.** Pre-existing risk shared with `ca-mom-bot`. Jobs in the same CAE egress through the same IPs; any drift affects both equally and is not new exposure introduced by this change.

---

## 5. Illustrative Bicep + deploy.yml Shape

These sketches are illustrative only — not reviewed, not tested. Exact param names and API versions must be verified against the live `infra/modules/` conventions before implementation.

### `infra/modules/migrations-job.bicep` (illustrative)

```bicep
param location string = resourceGroup().location
param environmentId string         // CAE resource ID
param imageName string             // e.g. 'acrMombot.azurecr.io/mom-bot:latest'
param uamiId string                // mi-mom-bot resource ID
param uamiClientId string          // mi-mom-bot client ID (for AZURE_CLIENT_ID env)
param acrLoginServer string        // e.g. 'acrMombot.azurecr.io'
param postgresHost string
param postgresDb string

resource migrationsJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-mom-bot-migrate'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uamiId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800          // unverified: no documented hard ceiling; 1800s is the Learn sample value
      replicaRetryLimit: 0
      manualTriggerConfig: {}
      registries: [
        {
          server: acrLoginServer
          identity: uamiId          // AcrPull must be assigned to mi-mom-bot
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: imageName
          command: ['/bin/sh', '/app/migrate.sh']   // overrides ENTRYPOINT
          env: [
            { name: 'AZURE_CLIENT_ID', value: uamiClientId }
            { name: 'PGHOST',          value: postgresHost }
            { name: 'PGDATABASE',      value: postgresDb }
            // PGPASSWORD set at runtime by migrate.sh via ManagedIdentityCredential
          ]
          resources: {
            cpu:    '0.25'
            memory: '0.5Gi'
          }
        }
      ]
    }
  }
}
```

### `.github/workflows/deploy.yml` migrate step (illustrative)

```yaml
- name: Run migrations
  run: |
    # Trigger the job and wait for the LRO to complete (default behavior — no --no-wait)
    az containerapp job start \
      --name job-mom-bot-migrate \
      --resource-group mom-bot

    # Capture the execution name for log retrieval
    JOB_RUN_NAME=$(az containerapp job execution list \
      --name job-mom-bot-migrate \
      --resource-group mom-bot \
      --query '[0].name' -o tsv)

    # Fetch logs from Log Analytics (may require retry loop due to ingestion lag)
    WORKSPACE_ID=$(az monitor log-analytics workspace list \
      --resource-group mom-bot \
      --query '[0].customerId' -o tsv)

    az monitor log-analytics query \
      --workspace "$WORKSPACE_ID" \
      --analytics-query "ContainerAppConsoleLogs_CL
        | where ContainerGroupName_s startswith '${JOB_RUN_NAME}'
        | order by TimeGenerated asc
        | project TimeGenerated, Log_s"
```

Note: the log-fetch step will return empty results if run immediately after job completion. A production implementation should retry with a short delay until the table is populated or a timeout is reached.

---

## 6. Decision Impact on #103

If the user accepts this CONDITIONAL YES recommendation:

- **Phase 2 of #103's cutover dissolves.** No second service principal (`mom-bot-gha-migrate`) is created. No second federated identity credential, no second GHA environment (`prod-migrate`), no second OIDC trust.
- **The `mom-bot-gha` Postgres admin grant can be removed.** That removal is straightforward (a one-line change to `infra/scripts/create-entra-admins.sh` and a corresponding Bicep/runbook update) and can ship as a small, focused PR independent of the broader #103 work.
- **Remaining #103 scope is unaffected.** The deploy-vs-migrate blast-radius concern is resolved differently (UAMI-scoped job instead of a second SP), but all other #103 work — role scoping for `mom-bot-gha`, the custom deployer role, KV Secrets Officer scoping — remains in place.
- **The five-phase cutover collapses to roughly three phases:** (1) add the migrations job Bicep + `migrate.sh`, (2) update `deploy.yml` to call the job instead of exec-ing into the running container, (3) drop the Postgres admin grant from `mom-bot-gha`.

---

## 7. Open Questions for User

### OQ-1: `replicaTimeout` upper bound

The research found no Learn page naming a documented hard ceiling for `replicaTimeout`. The spec uses 1800s as the proposed value (the Learn sample figure). The practical working range is estimated at 1800–3600s based on community evidence, but this is `unverified:` against official documentation.

**Action needed:** Is 1800s acceptable given current alembic upgrade durations? If future migrations are expected to exceed 30 minutes, what ceiling should be used, and do you want to hold the spec until a documented maximum is confirmed?

### OQ-2: ACR pull authority for `mi-mom-bot`

The research notes that `mi-mom-bot` likely already has `AcrPull` on the ACR since `ca-mom-bot` runs under it, but recommends explicit verification before committing the `registries[].identity` configuration in Bicep.

**Action needed:** Verify `mi-mom-bot` has `AcrPull` before implementation begins (an `az role assignment list` check), or accept the assumption and make it a pre-merge gate in the implementing PR.

### OQ-3: Log fetch lag in GHA UX

Today's migrate step streams alembic output directly in the GHA log via container exec. Under the job approach, output lands in Log Analytics with a documented multi-minute ingestion lag and must be fetched with a second KQL step. This is a real UX downgrade.

**Action needed:** Is this acceptable as-is (the blocking signal — job exit code — is still synchronous), or does better log streaming need to be a requirement before accepting this approach?

---

## 8. Acceptance Criteria

Per [glitchwerks/mom-bot#241](https://github.com/glitchwerks/mom-bot/issues/241):

1. Investigation report exists documenting all ten research questions with citations.
2. A decision is recorded: accept or reject the UAMI Container Apps Job pattern for migrations.
3. If accepted: follow-up items identified (OQ-1 through OQ-3 above, plus Bicep/deploy.yml implementation issue).
4. If rejected: rationale documented and #103 two-SP plan confirmed as the path forward.
