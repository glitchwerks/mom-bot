# AAD Provisioning Runbook — mom-bot-gha

> Commands in this runbook are **PowerShell**. Copy-paste directly into a
> PowerShell 7+ session (`pwsh`). Do not run them in bash or Command Prompt.

Bicep cannot create AAD app registrations (Microsoft Graph operations are not
supported in ARM/Bicep). This runbook covers the one-time manual steps required
before Bicep can deploy the rest of the infrastructure.

Run these commands once, in order, as the subscription owner in tenant
`cmbdevoutlook333.onmicrosoft.com` (`48bca6c3-6d4f-4884-bc1a-648ae2362a32`).

---

## Prerequisites

The `az login --tenant 48bca6c3-...` call below establishes the tenant for the entire session. Resource-management commands (`az deployment ...`, `az role assignment ...`, `az keyvault ...`) inherit the tenant from the active login session and reject `--tenant` as an unrecognized argument.

```powershell
# Log in to the correct tenant. Always pass --tenant to avoid cross-tenant confusion.
az login --tenant 48bca6c3-6d4f-4884-bc1a-648ae2362a32

# Set the target subscription.
az account set --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0

# Confirm you are in the right tenant and subscription.
az account show --query '{tenant:tenantId, sub:id, name:name}' -o table
```

---

## Step 1 — Create the AAD app registration

```powershell
$AppId = az ad app create `
  --display-name mom-bot-gha `
  --query appId `
  --output tsv

Write-Host "AppId=$AppId"
# Save this — you need it in the next step and for the repo variable.
```

---

## Step 2 — Create the service principal

The app registration needs a service principal so Azure RBAC can target it.

```powershell
$SpObjectId = az ad sp create --id $AppId --query id --output tsv

Write-Host "SpObjectId=$SpObjectId"
# Save this — used in Step 4.5 grant commands.
```

---

## Step 3 — Add federated credentials (OIDC trust)

Two federated credentials are required for the A++ dev/prod model.

The JSON body is written to a temp file to avoid PowerShell interpolating
dollar-signs inside the JSON. The closing `'@` must be at column 0 — no
leading whitespace.

> **Repo-name discipline (#248):** the federated credential `subject` claim
> uses the repo's **canonical name on GitHub**. The repo was renamed from
> `mom-bot` → `rsl-mom-bot` on 2026-05-29; existing FICs were updated in
> place via `az ad app federated-credential update`. If the repo is renamed
> again, every FIC needs its `subject` updated — GitHub's OIDC token always
> carries the current canonical name, so a stale FIC silently fails with
> `AADSTS700213` on first use after the rename (observed on PR #247 →
> tracked under #248).

### 3a — Trust pushes to `main`

```powershell
$FedCredMain = @'
{
  "name": "mom-bot-main-push",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:glitchwerks/rsl-mom-bot:ref:refs/heads/main",
  "description": "GHA OIDC trust for pushes to main branch",
  "audiences": ["api://AzureADTokenExchange"]
}
'@
$TempFile = New-TemporaryFile
$FedCredMain | Out-File -FilePath $TempFile.FullName -Encoding utf8 -NoNewline
az ad app federated-credential create --id $AppId --parameters "@$($TempFile.FullName)"
Remove-Item $TempFile
```

### 3b — Trust pull requests (for IaC what-if checks on PRs)

```powershell
$FedCredPr = @'
{
  "name": "mom-bot-pr",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:glitchwerks/rsl-mom-bot:pull_request",
  "description": "GHA OIDC trust for pull request workflows",
  "audiences": ["api://AzureADTokenExchange"]
}
'@
$TempFile = New-TemporaryFile
$FedCredPr | Out-File -FilePath $TempFile.FullName -Encoding utf8 -NoNewline
az ad app federated-credential create --id $AppId --parameters "@$($TempFile.FullName)"
Remove-Item $TempFile
```

> **Note:** Do NOT create `:environment:dev` or `:environment:prod` federations.
> The A++ model has no GitHub Environments distinction — one workflow, prod only.

---

## Step 4 — Save the SP Object ID for use in later steps

The SP Object ID is captured in `$SpObjectId` from Step 2. Keep it in scope for
the bootstrap grant commands in Step 4.5.

```powershell
# Keep $SpObjectId in scope — used in Step 4.5 grant commands.
Write-Host "SpObjectId=$SpObjectId"
```

---

## Step 4.5 — Grant SP subscription-scope deploy permission

`mom-bot-gha` needs `az deployment sub create` permission and its own runtime access roles. These are **bootstrap grants** — assigned here (out-of-band), not by Bicep. Bicep does not grant the GHA SP its own roles; it only assigns roles to runtime identities it creates (i.e. `mi-mom-bot`).

`az deployment sub create` requires `Microsoft.Resources/deployments/write` at **subscription** scope — RG-scope is insufficient because the deployment resource itself is created at sub scope. The manual workstation deploys in Step 5 work because the operator's own AAD account holds Owner; the SP cannot bootstrap itself. Granting these roles requires Owner or User Access Administrator and must be operator-run.

### Bootstrap grants for mom-bot-gha SP

Grant the SP the two runtime roles it needs before Bicep has run:

```powershell
$gha = az ad sp list --display-name mom-bot-gha --query "[0].id" -o tsv
$SUB = az account show --query id -o tsv
$KvId = az keyvault show -g mom-bot -n kv-mombot-eastus2 --query id -o tsv

# Container Apps Contributor at RG scope — required for az containerapp update in deploy.yml
az role assignment create `
  --assignee-object-id $gha `
  --assignee-principal-type ServicePrincipal `
  --role "Container Apps Contributor" `
  --scope "/subscriptions/$SUB/resourceGroups/mom-bot"

# Key Vault Secrets Officer at KV scope — required for GHA to write/rotate secrets
az role assignment create `
  --assignee-object-id $gha `
  --assignee-principal-type ServicePrincipal `
  --role "Key Vault Secrets Officer" `
  --scope $KvId
```

> These grants are intentionally out of Bicep. Bicep cannot safely manage SP self-grants: the SP must already hold `roleAssignments/write` before Bicep can run, so putting those grants inside Bicep creates a bootstrap circular dependency. See #174 for the decision thread.

### Role definition JSON

```json
{
  "Name": "Mom-bot GHA Subscription Deployer",
  "IsCustom": true,
  "Description": "Least-privilege role for mom-bot-gha SP to run az deployment sub create. Grants only deployment-resource CRUD and resource-group read at subscription scope; all child-resource writes flow through the separate RG-scoped Container Apps Contributor grant.",
  "Actions": [
    "Microsoft.Resources/deployments/*",
    "Microsoft.Resources/subscriptions/resourceGroups/read"
  ],
  "NotActions": [],
  "DataActions": [],
  "NotDataActions": [],
  "AssignableScopes": [
    "/subscriptions/213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0"
  ]
}
```

> **Important:** the action name is `Microsoft.Resources/subscriptions/resourceGroups/read` — note the `subscriptions/` segment. The shorter form `Microsoft.Resources/resourceGroups/read` is rejected by Azure with `InvalidActionOrNotAction`.

### Procedure

```powershell
# Build the role definition file
$rolePath = Join-Path $env:TEMP 'mom-bot-gha-deployer.json'
# ...paste the JSON above into $rolePath... (operator pastes from this runbook)

# Create the role definition (one-time per subscription)
az role definition create --role-definition $rolePath

# Resolve mom-bot-gha's appId from Entra (more reliable than copying repo vars)
$gha = az ad sp list --display-name mom-bot-gha --query "[0].appId" -o tsv

# Assign at subscription scope
az role assignment create `
    --assignee $gha `
    --role "Mom-bot GHA Subscription Deployer" `
    --scope /subscriptions/213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0

# Verify — should show the new role at sub scope alongside the existing RG-scoped grants
az role assignment list --assignee $gha --all -o table
```

### Expected verify output

After Phase 0.5 lands, `az role assignment list` should show these three assignments:

| Role | Scope |
|---|---|
| Mom-bot GHA Subscription Deployer | `/subscriptions/<sub-id>` |
| Container Apps Contributor | `.../resourceGroups/mom-bot` |
| Key Vault Secrets Officer | `.../resourceGroups/mom-bot` (or narrower KV scope) |

> **Note:** `Container Apps Contributor` and `Key Vault Secrets Officer` are bootstrap grants — assigned out-of-band (not by Bicep). Bicep only manages the `Key Vault Secrets User` assignment for `mi-mom-bot`.

### Fallback

If `az role definition create` fails for tenant-policy or naming-collision reasons, grant built-in `Contributor` at sub scope as an expedient — but file a follow-up issue immediately to swap it for the custom role. Don't leave Contributor silently in place.

See the decision-log extraction on issue #96 ([comment](https://github.com/glitchwerks/mom-bot/issues/96#issuecomment-4569696448)) for design rationale. Full plan content recoverable from git history (deleted in PR #247).

---

## Step 4.6 — Grant SP constrained RBAC Admin at RG scope (issue #167)

`infra-deploy.yml` runs `az deployment sub create`, which causes Bicep to write Key Vault (KV) role assignments via the ARM `Microsoft.Authorization/roleAssignments` resource. For that write to succeed, the SP needs `Microsoft.Authorization/roleAssignments/write` at a scope that covers the KV. This step grants the role at RG scope (not KV scope directly) — see design rationale below. Rather than granting the SP a broad User Access Administrator role (which allows assigning any role), this step grants `Role Based Access Control Administrator` constrained by an ABAC condition that limits the SP to assigning only **Key Vault Secrets User**.

**What this grants:**
- Role: `Role Based Access Control Administrator` (built-in `f58310d9-a9f6-439a-9e8d-f62e7b41a168`)
- Scope: `/subscriptions/<sub-id>/resourceGroups/mom-bot`
- Assignable roles (via ABAC condition):
  - `Key Vault Secrets User` (`4633458b-17de-408a-b874-0445c86b69e6`)

**Design principle:** Bicep only assigns roles to runtime identities it creates (e.g. `mi-mom-bot`). SP self-grants — where `mom-bot-gha` grants itself `Key Vault Secrets Officer` or `Container Apps Contributor` — belong in the bootstrap runbook (Step 4.5 or earlier), not in Bicep. This keeps the ABAC allow-list narrow: the only role Bicep needs to assign is `Key Vault Secrets User` for the managed identity.

The condition covers both `roleAssignments/write` (create) and `roleAssignments/delete` (remove) actions, with the appropriate attribute source for each (`@Request` for write, `@Resource` for delete), per the [Azure ABAC delegation examples](https://learn.microsoft.com/en-us/azure/role-based-access-control/delegate-role-assignments-examples) (fetched 2026-05-21).

**Full condition expression (verbatim):**

```
((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {4633458b-17de-408a-b874-0445c86b69e6})) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {4633458b-17de-408a-b874-0445c86b69e6}))
```

**Condition syntax reference:** [Azure ABAC condition format and syntax](https://learn.microsoft.com/en-us/azure/role-based-access-control/conditions-format) (fetched 2026-05-21) — `ForAnyOfAnyValues:GuidEquals` is the correct operator for GUID set-membership on `RoleDefinitionId`.

### Procedure (replay in a new tenant/subscription)

The `az role assignment create --condition` flag has a bug on some CLI versions where `--scope` with a full resource path triggers `MissingSubscription`. Use `az rest` directly as a workaround:

```powershell
$SUB = az account show --query id -o tsv
$gha = az ad sp list --display-name mom-bot-gha --query "[0].id" -o tsv
$CONDITION = "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {4633458b-17de-408a-b874-0445c86b69e6})) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {4633458b-17de-408a-b874-0445c86b69e6}))"

# Generate a random assignment GUID
$ASSIGNMENT_ID = [guid]::NewGuid().ToString()

$body = @{
  properties = @{
    roleDefinitionId = "/subscriptions/$SUB/providers/Microsoft.Authorization/roleDefinitions/f58310d9-a9f6-439a-9e8d-f62e7b41a168"
    principalId      = $gha
    principalType    = "ServicePrincipal"
    description      = "GHA SP role-assignment authority constrained to KV Secrets User/Officer for infra-deploy.yml"
    condition        = $CONDITION
    conditionVersion = "2.0"
  }
} | ConvertTo-Json -Depth 5

az rest `
  --method PUT `
  --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/mom-bot/providers/Microsoft.Authorization/roleAssignments/${ASSIGNMENT_ID}?api-version=2022-04-01" `
  --body $body `
  -o json
```

If `az role assignment create` works in your environment (future CLI versions may fix the `--scope` bug), the equivalent command is:

```bash
SUB=$(az account show --query id -o tsv)
GHA=$(az ad sp list --display-name mom-bot-gha --query "[0].id" -o tsv)
CONDITION="((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {4633458b-17de-408a-b874-0445c86b69e6})) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {4633458b-17de-408a-b874-0445c86b69e6}))"

az role assignment create \
  --role "Role Based Access Control Administrator" \
  --assignee-object-id "$GHA" \
  --assignee-principal-type ServicePrincipal \
  --scope "/subscriptions/${SUB}/resourceGroups/mom-bot" \
  --condition "${CONDITION}" \
  --condition-version "2.0" \
  --description "GHA SP role-assignment authority constrained to KV Secrets User/Officer for infra-deploy.yml"
```

### Verify

```powershell
az role assignment list `
  --assignee $gha `
  --all `
  --query "[?roleDefinitionName=='Role Based Access Control Administrator'].{role:roleDefinitionName, scope:scope, condition:condition, conditionVersion:conditionVersion}" `
  -o json
```

```bash
az role assignment list \
  --assignee "$GHA" \
  --all \
  --query "[?roleDefinitionName=='Role Based Access Control Administrator'].{role:roleDefinitionName, scope:scope, condition:condition, conditionVersion:conditionVersion}" \
  -o json
```

Expected output confirms: role is `Role Based Access Control Administrator`, scope ends in `.../resourceGroups/mom-bot`, and the condition string contains only the `Key Vault Secrets User` GUID (`4633458b-17de-408a-b874-0445c86b69e6`).

### Expected full role listing after this step

| Role | Scope | How assigned |
|---|---|---|
| Mom-bot GHA Subscription Deployer | `/subscriptions/<sub-id>` | Step 4.5 (manual bootstrap) |
| Container Apps Contributor | `.../resourceGroups/mom-bot` | Step 4.5 (manual bootstrap) |
| Key Vault Secrets Officer | `.../resourceGroups/mom-bot/.../kv-mombot-eastus2` | Step 4.5 (manual bootstrap) |
| Contributor | `.../resourceGroups/mom-bot` | Step 4.5 fallback expedient (see note) |
| Role Based Access Control Administrator | `.../resourceGroups/mom-bot` | Step 4.6 (this step) |

> **Note on Contributor:** A broader `Contributor` grant was added as a fallback expedient (see Step 4.5 fallback note). It is a superset of Container Apps Contributor; schedule cleanup in a follow-up issue.
>
> **Note on Bicep vs bootstrap:** `Container Apps Contributor` and `Key Vault Secrets Officer` were previously granted by Bicep (`containerapp.bicep` and `keyvault.bicep`). As of #174 those Bicep resources are removed — the grants are out-of-band bootstrap only. The ABAC condition on `Role Based Access Control Administrator` is narrowed accordingly: it now covers only `Key Vault Secrets User` (the one role Bicep still needs to assign, to `mi-mom-bot`).

**Design rationale:** Granting at RG scope (not KV scope) gives the SP the minimum scope necessary for the Bicep KV role-assignment resource while avoiding subscription-wide RBAC authority. The ABAC condition is the safety layer — without it, RBAC Admin would allow the SP to assign any role to any principal within the RG. See [#167](https://github.com/glitchwerks/mom-bot/issues/167) for the full decision thread.

---

## Step 5 — Deploy Bicep infrastructure

With the bootstrap grants from Step 4.5 in place, deploy the Bicep infrastructure:

```powershell
az deployment sub create `
  --location eastus2 `
  --template-file infra/main.bicep `
  --parameters infra/main.bicepparam `
  --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0
```

> **Tip**: before running the deploy, validate the parameter file with
> `az bicep build-params --file infra/main.bicepparam`. CI's
> `az bicep build --file main.bicep` does NOT validate the bicepparam
> against the template — `build-params` does.

Bicep handles the following RBAC role assignments:

- `mi-mom-bot` → **Key Vault Secrets User** (runtime read-only)

If the Bicep role assignment fails (e.g. `managedIdentityPrincipalId` was wrong),
assign manually:

```powershell
$KvId = az keyvault show -g mom-bot -n kv-mombot-eastus2 --query id -o tsv

# Key Vault Secrets User for the managed identity
$MiPrincipalId = az identity show -g mom-bot -n mi-mom-bot --query principalId -o tsv
az role assignment create `
  --role "Key Vault Secrets User" `
  --assignee-object-id $MiPrincipalId `
  --assignee-principal-type ServicePrincipal `
  --scope $KvId
```

GHA SP bootstrap roles (`Key Vault Secrets Officer`, `Container Apps Contributor`) are granted out-of-band in Step 4.5 — they are not managed by Bicep.

---

## Step 5.5 — Create Entra admins on Postgres (post-deploy)

After `az deployment sub create` completes successfully against `infra/main.bicep`,
run the following to register `mi-mom-bot` as the Entra admin on the Postgres server.
This step replaces the `administrators` resources that used to live in
`infra/modules/postgres.bicep`; they were moved here because the ARM resource
races against the server's post-provision Updating window (issue #106).

> **Issue #255 (Phase 3):** `mom-bot-gha` is no longer registered as a Postgres
> Entra admin. Migrations now run via the Container Apps Job `job-mom-bot-migrate`
> under `mi-mom-bot` UAMI (issue #255). `mom-bot-gha` cannot connect to Postgres
> directly from GHA runners — the Postgres firewall allows only CAE outbound IPs.
> `GHA_SP_OBJECT_ID` and `GHA_SP_DISPLAY_NAME` are no longer required by the
> script.

```bash
RESOURCE_GROUP=mom-bot \
POSTGRES_SERVER_NAME=<from main.bicepparam — e.g. pg-mombot-flkrgslirk53q> \
UAMI_OBJECT_ID=$(az identity show -g mom-bot -n mi-mom-bot --query principalId -o tsv) \
UAMI_DISPLAY_NAME=mi-mom-bot \
bash infra/scripts/create-entra-admins.sh
```

The script is idempotent — safe to re-run.

### Troubleshooting: First-run Entra admin propagation delay

**Symptom:** The first `az containerapp job start` (or manual run of the
migration job) fails with an authentication error from Postgres — typically
`password authentication failed for user "mi-mom-bot"` or a similar Entra
token rejection. The infra is otherwise healthy.

**Cause:** After `create-entra-admins.sh` completes, the Postgres Flexible
Server takes approximately 60–90 seconds to propagate the new Entra admin
grant internally. A job triggered immediately after the script will hit the
server before propagation is complete and receive an auth failure. This is
a one-time cost per spec §3 Q4 — it does not recur on subsequent runs.

**How to distinguish from a real config problem:**
- If `UAMI_OBJECT_ID` in the script output matches `mi-mom-bot`'s principal ID
  (verifiable with `az identity show -g mom-bot -n mi-mom-bot --query principalId -o tsv`),
  and the error appears within 2 minutes of running the script, it is almost
  certainly a propagation delay.
- If the error persists beyond 3 minutes, or `UAMI_OBJECT_ID` was wrong,
  re-run the script to confirm idempotency and check the Postgres admin list:
  `az postgres flexible-server microsoft-entra-admin list -g mom-bot --server-name <PG_SERVER> -o table`.

**Recovery:** Wait 60–90 seconds and re-trigger the job:

```bash
az containerapp job start \
  --name job-mom-bot-migrate \
  --resource-group mom-bot
```

**If `mom-bot-gha` was previously registered as Entra admin** and you want to revoke
that grant (optional cleanup), run:

```bash
PG_SERVER=<postgres-server-name>
GHA_OID=$(az ad sp show --id <mom-bot-gha-app-id> --query id -o tsv)
az postgres flexible-server microsoft-entra-admin delete \
  -g mom-bot --server-name "$PG_SERVER" --object-id "$GHA_OID" --yes
```

This is a clean-up step only — the `job-mom-bot-migrate` Container Apps Job does
not depend on `mom-bot-gha` being removed; it depends only on `mi-mom-bot` being
present.

### Cutover completion: reassign ownership before revoking mom-bot-gha

> **When to run this.** The simple `delete` snippet above will fail with
> `AadAuthPrincipalDropFailed` / `2BP01: role "mom-bot-gha" cannot be dropped
> because some objects depend on it` if historical migrations created objects
> owned by `mom-bot-gha`. Run the sequence below **before** retrying the delete.
> Full design rationale and privilege-model analysis are in
> [`docs/superpowers/plans/2026-05-29-261-postgres-role-cutover.md`](../docs/superpowers/plans/2026-05-29-261-postgres-role-cutover.md)
> (refs #261).

Set shell variables (bash):

```bash
RG=mom-bot
PG_SERVER=pg-mombot-flkrgslirk53q
PG_FQDN="${PG_SERVER}.postgres.database.azure.com"
PG_DB=mom_bot
ADMIN_USER='cmb_dev@outlook.com'   # the human Entra admin display/login name

# mom-bot-gha object-id — verify before the destructive step (step 4 below):
GHA_OID=6fcf4d62-e6da-4819-9667-234a55018fa2
```

Open a firewall rule for your workstation IP (see "Dev-laptop ad-hoc Postgres
access" above for the full procedure):

```bash
MYIP=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create \
  --resource-group "$RG" --name "$PG_SERVER" \
  --rule-name "dev-cmb-261-cutover" \
  --start-ip-address "$MYIP" --end-ip-address "$MYIP"
```

Acquire an Entra token and connect as the admin. The `azure_pg_admin`
enhancement on Flexible Server lets any Entra admin run `REASSIGN OWNED`
against nonrestricted roles without needing superuser or explicit
`GRANT "mom-bot-gha" TO current_user` first:

```bash
export PGPASSWORD=$(az account get-access-token \
  --resource-type oss-rdbms \
  --query accessToken -o tsv)

psql "host=${PG_FQDN} port=5432 dbname=${PG_DB} user=${ADMIN_USER} sslmode=require"
```

**Step 1 — Enumerate objects owned by `mom-bot-gha` (capture output as before-state):**

```sql
-- Tables, sequences, views, matviews owned by mom-bot-gha
SELECT n.nspname AS schema, c.relname AS object, c.relkind AS kind
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_roles r     ON r.oid = c.relowner
WHERE r.rolname = 'mom-bot-gha'
ORDER BY 1, 3, 2;

-- Schemas owned by mom-bot-gha (expected: none — public is owned by azure_pg_admin)
SELECT nspname FROM pg_namespace n
JOIN pg_roles r ON r.oid = n.nspowner
WHERE r.rolname = 'mom-bot-gha';

-- Functions / procedures owned by mom-bot-gha
SELECT n.nspname AS schema, p.proname AS function
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_roles r     ON r.oid = p.proowner
WHERE r.rolname = 'mom-bot-gha'
ORDER BY 1, 2;

-- Types owned by mom-bot-gha
SELECT n.nspname AS schema, t.typname AS type
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
JOIN pg_roles r     ON r.oid = t.typowner
WHERE r.rolname = 'mom-bot-gha'
ORDER BY 1, 2;

-- Single-number guard: total owned-object count
SELECT
  (SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE r.rolname='mom-bot-gha')
  +
  (SELECT count(*) FROM pg_proc  p JOIN pg_roles r ON r.oid=p.proowner WHERE r.rolname='mom-bot-gha')
  +
  (SELECT count(*) FROM pg_type  t JOIN pg_roles r ON r.oid=t.typowner WHERE r.rolname='mom-bot-gha' AND t.typtype != 'c')
  -- typtype != 'c' excludes composite types already counted in pg_class above
  AS gha_owned_count;
```

If `gha_owned_count` is non-zero (expected), continue. If it is zero, the
plain `delete` above should already succeed — stop here and retry it.

**Step 2 — Reassign ownership to `mi-mom-bot`:**

```sql
BEGIN;
REASSIGN OWNED BY "mom-bot-gha" TO "mi-mom-bot";
-- Do NOT add DROP OWNED — that would drop privileges/grants, not just ownership.
COMMIT;
```

> If this returns `42501 permission denied`, the `azure_pg_admin` enhancement is
> not active on this server image. Run the following fallback in the **same
> session**, then re-run the `BEGIN…COMMIT` block:
>
> ```sql
> GRANT "mom-bot-gha" TO current_user;
> GRANT "mi-mom-bot"  TO current_user;
> ```

**Step 3 — Verify ownership transferred (count must be zero):**

```sql
SELECT
  (SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE r.rolname='mom-bot-gha')
  +
  (SELECT count(*) FROM pg_proc  p JOIN pg_roles r ON r.oid=p.proowner WHERE r.rolname='mom-bot-gha')
  +
  (SELECT count(*) FROM pg_type  t JOIN pg_roles r ON r.oid=t.typowner WHERE r.rolname='mom-bot-gha' AND t.typtype != 'c')
  -- typtype != 'c' excludes composite types already counted in pg_class above
  AS gha_owned_count_after;   -- MUST be 0

-- Spot-check: alembic version table + a real table now owned by mi-mom-bot
-- adjust table names to match what step 1 enumerated for this database
SELECT n.nspname, c.relname, pg_get_userbyid(c.relowner) AS owner
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relname IN ('alembic_version','reminders')
ORDER BY 1,2;
```

`gha_owned_count_after` must be 0 before proceeding. If it is not zero,
re-run step 1 to see what remains (likely a different database context) and
repeat step 2 there. Exit psql (`\q`).

**Step 4 — Confirm the object-id, then retry the Entra-admin revoke:**

```bash
# Resolve mom-bot-gha's object-id dynamically from Entra
GHA_OID=$(az ad sp list --display-name mom-bot-gha --query "[0].id" -o tsv)
echo "Resolved mom-bot-gha object-id: $GHA_OID"
# Operator: confirm this equals 6fcf4d62-e6da-4819-9667-234a55018fa2 before proceeding

# List current admins (before)
az postgres flexible-server microsoft-entra-admin list \
  -g "$RG" --server-name "$PG_SERVER" -o table

# Revoke
az postgres flexible-server microsoft-entra-admin delete \
  -g "$RG" --server-name "$PG_SERVER" \
  --object-id "$GHA_OID" --yes
```

**Step 5 — Verify the final admin list:**

```bash
az postgres flexible-server microsoft-entra-admin list \
  -g "$RG" --server-name "$PG_SERVER" -o table
# Expected remaining admins: mi-mom-bot (SP), cmb_dev (User). mom-bot-gha GONE.
```

Close the firewall rule:

```bash
az postgres flexible-server firewall-rule delete \
  --resource-group "$RG" --name "$PG_SERVER" \
  --rule-name "dev-cmb-261-cutover" --yes
```

---

## Step 6 — Set GitHub repo variables

These are **Variables** (not Secrets — they are non-sensitive OIDC identifiers).
`gh variable set` writes them directly from the values captured in earlier steps.

```powershell
gh variable set AZURE_CLIENT_ID --body $AppId --repo glitchwerks/rsl-mom-bot
gh variable set AZURE_TENANT_ID --body 48bca6c3-6d4f-4884-bc1a-648ae2362a32 --repo glitchwerks/rsl-mom-bot
gh variable set AZURE_SUBSCRIPTION_ID --body 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0 --repo glitchwerks/rsl-mom-bot
```

---

## Step 7 — Grant yourself Key Vault Secrets Officer for seeding

Your own user account needs `Key Vault Secrets Officer` on the KV to both
read and write secrets — required for the secret-seeding calls in Step 8.

```powershell
$MyObjectId = az ad signed-in-user show --query id -o tsv

# Capture the KV resource ID if you did not run the manual fallback in Step 5.
$KvScope = az keyvault show --name kv-mombot-eastus2 --resource-group mom-bot --query id -o tsv

az role assignment create `
  --role "Key Vault Secrets Officer" `
  --assignee-object-id $MyObjectId `
  --assignee-principal-type User `
  --scope $KvScope
```

> **Note**: `Key Vault Secrets Officer` includes write access (needed for Step 8's secret seeding). `Key Vault Secrets User` is read-only and would fail the secret-set call. If you only need to read secrets locally for dev (not seed/rotate), `User` is the lesser-privilege choice.
>
> RBAC propagation can take 5–10 minutes. If Step 8 fails with a permission error immediately after this step, wait 30–120 seconds and retry.

---

## Step 8 — Seed initial secrets

The bot reads secrets from Key Vault at runtime via `mom_bot.config.load_secret`,
prefixing each name with `<MOM_BOT_ENV>-`. So `dev-` secrets are used in local dev
(laptop), and `prod-` secrets in the deployed Container App.

Token-class secrets in this section are read interactively via
`Read-Host -AsSecureString` rather than passed inline as `--value "..."`. This
avoids the trailing-whitespace and copy-paste-newline pitfalls that cause
Discord/Azure to silently reject "valid-looking" tokens.

See `docs/secrets-inventory.md` for the full secret catalog, expected formats,
and a column indicating which secrets share the same value across environments.

### Discord bot token — same value in both env slots

If you have **one Discord application** (one bot, invited to multiple guilds),
the same token authenticates in every guild. Both `dev-discord-token` and
`prod-discord-token` hold the same value — you paste it once and write it twice.

(If you ever create a separate dev-only bot application, the values will diverge.
See "When to split into two bot applications" at the bottom of this section.)

```powershell
# Paste once — won't echo to terminal:
$secureToken = Read-Host "Paste your Discord bot token" -AsSecureString
$token = [System.Net.NetworkCredential]::new("", $secureToken).Password

az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-discord-token --value $token | Out-Null
az keyvault secret set --vault-name kv-mombot-eastus2 --name prod-discord-token --value $token | Out-Null
```

### Guild IDs — different per environment

> **Bicep-managed since PR #237 (issues #121, #236) — `prod-guild-id` is now
> provisioned by `infra/main.bicep`. Do not run the `prod-guild-id` command
> below by hand; edit `infra/main.bicepparam` (`param guildId`) instead and
> redeploy. The `dev-guild-id` command remains manual (dev env is not
> provisioned by this Bicep template).**

Each Discord server has its own ID. Right-click your server name in Discord
(with Developer Mode on: User Settings → Advanced → Developer Mode) → "Copy
Server ID".

Guild IDs are public identifiers — they are safe to echo to the terminal.

```powershell
# DEV only — prod-guild-id is Bicep-managed (see note above).
$devGuildId  = Read-Host "Paste your DEV guild ID (test server)"

az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-guild-id  --value $devGuildId  | Out-Null
```

### Database URL — different per environment

```powershell
# Local dev: SQLite file in the working directory
az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-database-url  --value "sqlite:///./mom_bot_dev.db" | Out-Null

# Prod: SQLite on Container Apps volume (placeholder for PostgreSQL in Epic 1+)
az keyvault secret set --vault-name kv-mombot-eastus2 --name prod-database-url --value "sqlite:////data/mom_bot.db" | Out-Null
```

### App Insights connection string — Bicep-managed since PR #182

> **Bicep-managed since issue #182 — the Application Insights resource and its
> connection string are now provisioned by `infra/modules/observability.bicep`.
> `APPLICATIONINSIGHTS_CONNECTION_STRING` is injected into the Container App
> as a container secret sourced directly from the Bicep output (not from KV),
> avoiding the PLACEHOLDER drift documented in #199. Do NOT set
> `prod-app-insights-conn-string` in Key Vault by hand; the secret is no longer
> referenced. The `dev-*` command below remains manual (dev env is not
> provisioned by this Bicep template).**

```powershell
# Dev only — prod is Bicep-managed (see above).
az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-app-insights-conn-string  --value "PLACEHOLDER" | Out-Null
```

### Reminder scheduler secrets — channel name and mention role (no Developer Mode required)

> **Bicep-managed since PR #237 (issues #121, #236) — `prod-reminder-channel-name`
> and `prod-reminder-mention-role-name` are now provisioned by
> `infra/main.bicep`. Do not run the `prod-*` commands below by hand; edit
> `infra/main.bicepparam` (`param reminderChannelName` / `param
> reminderMentionRoleName`) instead and redeploy. The `dev-*` commands remain
> manual (dev env is not provisioned by this Bicep template).**

Both the Hydra and Chimera reminders fire to the **same channel** in each
environment (collapsed from two per-reminder secrets in #43). Reminders post
without pinging any role (#45). The secret value is the **channel name** as
a plain string — no Developer Mode, no right-click, no snowflake copy (#47).
The mention role name (restored in #51) is similarly a plain string.

```powershell
# DEV only — prod-reminder-channel-name is Bicep-managed (see note above).
$devReminderChannelName  = Read-Host "Paste your DEV reminder channel NAME (e.g. reminders)"

az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-reminder-channel-name  --value $devReminderChannelName  | Out-Null
```

```powershell
# DEV only — prod-reminder-mention-role-name is Bicep-managed (see note above).
$devReminderMentionRoleName = Read-Host "Paste your DEV reminder mention role NAME (e.g. Member)"

az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-reminder-mention-role-name --value $devReminderMentionRoleName | Out-Null
```

> **Note**: If you previously seeded `*-reminder-{hydra,chimera}-channel-id`,
> `*-reminder-channel-id` (snowflake), or `*-reminder-mention-role-id`
> secrets, see the migration history in `docs/secrets-inventory.md` before
> running these commands.

### Verify all expected secrets exist

```powershell
az keyvault secret list --vault-name kv-mombot-eastus2 --query "[].name" -o tsv
```

You should see, at minimum:

- `dev-discord-token`, `prod-discord-token`
- `dev-guild-id`, `prod-guild-id` ← `prod-guild-id` provisioned by Bicep (#121, #236)
- `dev-database-url`, `prod-database-url`
- `dev-app-insights-conn-string`, `prod-app-insights-conn-string`
- `dev-reminder-channel-name`, `prod-reminder-channel-name` ← `prod-*` provisioned by Bicep (#121)
- `dev-reminder-mention-role-name`, `prod-reminder-mention-role-name` ← `prod-*` provisioned by Bicep (#121)

### When to split into two bot applications

For now, **one mom-bot application** invited to both your dev guild and your prod
guild is sufficient. If you later want full prod isolation — slash-command changes
in dev that don't affect prod, a separate avatar or status, independent token
rotation — create a second Discord application named `mom-bot-dev`, invite it to
your dev guild only, and store its token in `dev-discord-token` (overwriting the
shared value). At that point the two slots legitimately diverge.

---

## Step 9 — Run the deploy workflow (post-merge)

This step runs **after PR #21 merges to `main`**. The `workflow_dispatch`
trigger on `deploy.yml` fires from the repo's default branch only, so the
deploy workflow file must already be on `main` before you can invoke it.
Treat Step 9 as the first post-merge smoke test, not a pre-merge gate.

Trigger the first deploy via GitHub Actions:

```
GitHub repo → Actions → Deploy (mom-bot) → Run workflow → Run workflow
```

The workflow (`deploy.yml`) runs `az containerapp update` to push the container
image to `ca-mom-bot`. First run succeeds if:

1. AAD federated credentials are in place (Steps 1–3)
2. Repo variables are set (Step 6)
3. A container image exists at `ghcr.io/glitchwerks/mom-bot:<sha>`

> Image build+push to GHCR is Epic 1 work. For v0 smoke testing, push a
> placeholder image manually and rerun.

---

## Step 9.5 — Run the infra deploy workflow (post-merge)

`infra-deploy.yml` is the CI/CD counterpart to the manual `az deployment sub create` in Step 5. It runs the same Bicep apply (`infra/main.bicep` + `infra/main.bicepparam`) against the production subscription via OIDC, removing the need for a local `az login` session for infrastructure changes.

**Trigger:** `workflow_dispatch` only (v1 — auto-trigger on infra/** push is deferred, see #83).

**SP role relied on:** `Mom-bot GHA Subscription Deployer` (custom role, subscription scope — granted in Step 4.5). Same OIDC credentials as the existing what-if workflow.

**Inputs:**
- `commit_sha` (optional) — pin to a specific commit; defaults to HEAD of `main`.

**To invoke:**
```
GitHub repo → Actions → Deploy infra (Bicep apply) → Run workflow → Run workflow
```

The workflow:
1. Checks out the repo (at `commit_sha` if supplied, else `main` HEAD).
2. Logs in via OIDC (`mom-bot-gha` app registration — same as `deploy.yml` and `infra-what-if.yml`).
3. Resolves the live container image → `CONTAINER_IMAGE` env var (prevents phantom diffs).
4. Runs `az deployment sub create` — deployment named `infra-<run_id>` for portal traceability.
5. Writes a summary to the Actions run page (deployment name + subscription + portal navigation hint).

After the workflow completes, continue to Step 5.5 (Entra admin creation on Postgres) if this is a cold-start or the Postgres server was recreated.

---

## Step 10 — Manual old-revision deactivation (#96 stopgap)

`activeRevisionsMode: 'Single'` deactivates prior revisions automatically only when
the Container App has ingress — the mode governs traffic routing, not revision lifecycle
in the absence of HTTP traffic. Because `ca-mom-bot` is ingress-less, deploying a new
image does not deactivate the old revision; stale revisions accumulate and consume
quota. Until Phase 2 of [#83](https://github.com/glitchwerks/mom-bot/issues/83)
automates this inside the deploy workflow, run the following snippet manually after
each `az containerapp update` (i.e., after each successful Step 9 run). Tracked in
[#96](https://github.com/glitchwerks/mom-bot/issues/96).

```powershell
# Collect names of all currently active revisions on the Container App
$active = az containerapp revision list `
  --name ca-mom-bot --resource-group mom-bot `
  --query "[?properties.active].name" -o tsv

# Resolve the newest active revision by createdTime — this is the one we keep
$latest = az containerapp revision list `
  --name ca-mom-bot --resource-group mom-bot `
  --query "sort_by([?properties.active], &properties.createdTime)[-1].name" -o tsv

# Deactivate every active revision except the newest
$active -split "`n" | Where-Object { $_ -and $_ -ne $latest } | ForEach-Object {
  az containerapp revision deactivate --name ca-mom-bot --resource-group mom-bot --revision $_
}
```

Automation tracked in [#83](https://github.com/glitchwerks/mom-bot/issues/83); see `deploy.yml` and `scripts/deactivate-old-revisions.sh` once that work lands.

---

## Dev-laptop ad-hoc Postgres access

> **Why this is a runbook step, not Bicep:** The `operatorIpAddress` param was
> removed from Bicep in [#166](https://github.com/glitchwerks/mom-bot/issues/166).
> Operator IPs change frequently (VPNs, ISP DHCP rotation, travel) — managing
> them in Bicep means every IP change triggers a Bicep deploy. Instead, open a
> rule when you need it and delete it when you are done. The Postgres server
> and the `allow-cae-egress` firewall rule are still Bicep-managed; only
> operator ad-hoc rules are manual.

### Open a firewall rule (when you need DB access)

```powershell
# 1. Find your current public egress IP.
$MyIp = (Invoke-WebRequest -Uri 'https://api.ipify.org').Content
Write-Host "My IP: $MyIp"

# 2. Find the Postgres server name (deterministic hash of the RG resource ID).
$PgServer = az postgres flexible-server list `
  --resource-group mom-bot `
  --query "[0].name" -o tsv
Write-Host "PG server: $PgServer"

# 3. Open a firewall rule scoped to your IP only.
#    Replace <your-handle> with your GitHub handle or initials (e.g. cbeaulieu).
az postgres flexible-server firewall-rule create `
  --resource-group mom-bot `
  --name $PgServer `
  --rule-name "dev-<your-handle>" `
  --start-ip-address $MyIp `
  --end-ip-address $MyIp
```

The rule is effective within ~30 seconds.

### Remove the firewall rule (when you are done)

Always delete the rule when you finish your session. Do not leave developer IP
rules open indefinitely — they widen the attack surface on a password-auth-disabled
(Entra-only) server that is otherwise narrowly scoped.

```powershell
az postgres flexible-server firewall-rule delete `
  --resource-group mom-bot `
  --name $PgServer `
  --rule-name "dev-<your-handle>" `
  --yes
```

### Verify the rule is gone

```powershell
az postgres flexible-server firewall-rule list `
  --resource-group mom-bot `
  --name $PgServer `
  --output table
```

Expected output shows only `allow-cae-egress` (the CAE static IP rule, which is
Bicep-managed). Any `dev-*` rules indicate a session was not cleaned up.

### If your IP changes mid-session

Delete the old rule and create a new one with the updated IP:

```powershell
$MyIp = (Invoke-WebRequest -Uri 'https://api.ipify.org').Content
az postgres flexible-server firewall-rule delete `
  --resource-group mom-bot --name $PgServer `
  --rule-name "dev-<your-handle>" --yes
az postgres flexible-server firewall-rule create `
  --resource-group mom-bot --name $PgServer `
  --rule-name "dev-<your-handle>" `
  --start-ip-address $MyIp --end-ip-address $MyIp
```

---

## Notes

### Placeholder container image

The `containerImage` parameter defaults to `mcr.microsoft.com/k8se/quickstart:latest` —
Microsoft's public Container Apps hello-world image. The Container App provisions and serves
a static page until Epic 1 wires up image build+push to GHCR. To deploy a real mom-bot image
at any time, override at the CLI:

```powershell
az deployment sub create `
  --location eastus2 `
  --template-file infra/main.bicep `
  --parameters infra/main.bicepparam `
  --parameters containerImage="ghcr.io/glitchwerks/mom-bot:<sha>" `
  --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0
```

---

## Summary checklist

**Pre-merge (one-time provisioning):**
- [ ] Step 1 — AAD app created; `$AppId` saved
- [ ] Step 2 — Service principal created; `$SpObjectId` saved
- [ ] Step 3a — Federated credential for `main` push created
- [ ] Step 3b — Federated credential for pull requests created
- [ ] Step 4 — `$SpObjectId` in scope for Step 4.5 grant commands
- [ ] Step 4.5 (one-time) — Custom role `Mom-bot GHA Subscription Deployer` created and assigned to `mom-bot-gha` at subscription scope
- [ ] Step 4.6 (one-time) — `Role Based Access Control Administrator` granted to `mom-bot-gha` at RG scope with ABAC condition constraining assignable roles to KV Secrets User/Officer (see #167)
- [ ] Step 5 — Bicep deployed successfully (parameter file validated with `az bicep build-params` first)
- [ ] Step 5.5 — Entra admin (`mi-mom-bot` only) created on Postgres via `infra/scripts/create-entra-admins.sh` (issue #255: `mom-bot-gha` grant removed)
- [ ] Step 6 — Repo variables set in GitHub
- [ ] Step 7 — Grant yourself Key Vault Secrets Officer for seeding
- [ ] Step 8 — Initial secrets seeded

**Post-merge (validation):**
- [ ] Step 9 — Run the deploy workflow (`workflow_dispatch` on `deploy.yml` from `main`)
- [ ] Step 10 — Manually deactivate old revisions (stopgap until #83 automates it)

---

## AzureFile-backed SQLite (Policy decisions from #87) — DEPRECATED

> **⚠️ DEPRECATED — removed in #240.** This section documents the
> SQLite-on-AzureFile stopgap that ran in production from Epic 0 until the
> Postgres migration (#92) superseded it. The storage account, file share,
> volume mount, and all associated policies below were removed from
> infrastructure in #240. **Do not follow these procedures** — they operate on
> resources that no longer exist. Retained as historical record only.

This section documents the operator policies that govern the SQLite-on-AzureFile
stopgap introduced in PR #87. These policies are load-bearing — violating them
risks database corruption.

---

### Backing store choice

**Storage type:** Azure File Share, Standard LRS
**Cost:** ~$0.25–1.10/month for a ≤ 1 GiB share

EmptyDir (the default Container Apps ephemeral volume) loses all state on every
revision swap — unacceptable for a database. PostgreSQL is the correct long-term
answer (see Epic #91, closed — decision-log extraction at [#91 comment](https://github.com/glitchwerks/mom-bot/issues/91#issuecomment-4569696957)), but standing up a managed Postgres
instance is out of scope for the initial bot bringup. AzureFile Standard LRS
gives a persistent, SMB-mountable file share for under $2/month with no managed
database overhead. It is explicitly a **stopgap** — the `prod-database-url` KV
secret already points to the `/data/mom_bot.db` path that will be replaced when
the PostgreSQL migration lands.

---

### Risk acknowledgement (Policy 3)

See the verbatim risk acknowledgement block at the top of
`infra/modules/containerapp.bicep`. Summary of accepted risks:

- SMB does not honour the fsync/lock semantics SQLite assumes; a dropped
  connection mid-write opens a corruption window.
- File-level locking over SMB is advisory only. **Concurrent writers will
  corrupt the database file.** Mitigated by Policy 1 (see below).
- No point-in-time recovery below 1-day granularity. Daily share snapshots
  (Policy 2, see below) are the recovery SLA.

Do not extend the SQLite-on-SMB pattern to additional services.

---

### `maxReplicas` lock — Policy 1

**Rule:** The Container App must never run more than one replica simultaneously.

**Why:** SQLite's WAL mode assumes a single writer. Multiple replicas would
each open the same `/data/mom_bot.db` over SMB, competing for the write lock.
SMB advisory locking provides no crash-safe mutual exclusion — simultaneous
writers will corrupt the file.

**Enforcement:** The `maxReplicas` parameter in `infra/modules/containerapp.bicep`
carries an `@allowed([1])` decorator. Supplying any value other than `1` is a
**hard Bicep build error** (BCP036) — the template will not compile, so the
constraint cannot be silently bypassed via a parameter file change. Verified by
the negative test in PR #87 (error output: `BCP036: The property "maxReplicas"
expected a value of type "1 | null" but the provided value is of type "2"`).

**Operator escape-hatch:** If the decorator must be removed (e.g. for a
PostgreSQL migration that makes multi-replica safe), the change requires editing
the decorator in `containerapp.bicep` and redeploying. This is intentionally
painful — a quiet parameter bump should never silently lift the limit.

---

### Snapshot schedule — Policy 2

Daily share snapshots are the recovery mechanism. Granularity is 1 day; retention
is 7 days. Automation is deferred to a follow-up issue (see "Automate AzureFile
snapshot schedule for prod") pending OIDC/federated auth in CI (#83).

Until automation lands, run this command manually each day (or via a local
Task Scheduler / cron job on the operator's workstation):

```powershell
# Take a snapshot. Replace <storage-account-name> with the value from the
# storage.bicep Bicep output or: az storage account list -g mom-bot --query "[].name" -o tsv
az storage share snapshot create `
  --account-name <storage-account-name> `
  --name mom-bot-data
```

Prune snapshots older than 7 days (run after taking the new snapshot):

```powershell
# List and delete snapshots older than 7 days.
$cutoff = (Get-Date).ToUniversalTime().AddDays(-7).ToString('yyyy-MM-ddTHH:mm:ssZ')
az storage share list --account-name <storage-account-name> --include-snapshots `
  --query "[?snapshot && snapshot < '$cutoff'].snapshot" -o tsv |
  ForEach-Object { az storage share delete --account-name <storage-account-name> --name mom-bot-data --snapshot $_ }
```

The storage account name is emitted by `storage.bicep` as output `storageAccountName`
and visible in the Azure portal under the `mom-bot` resource group, or via:

```powershell
az storage account list -g mom-bot --query "[].name" -o tsv
```

---

### Persistence verification drill

Run after every Bicep redeploy or revision swap to confirm the volume survives:

1. **Write a test row:**
   ```powershell
   # Exec into a running replica (requires Azure CLI extension + container name)
   az containerapp exec -g mom-bot -n ca-mom-bot --command "/bin/sh"
   # Inside the container:
   python -c "
   import sqlalchemy, os
   engine = sqlalchemy.create_engine(os.environ['MOM_BOT_DATABASE_URL'])
   with engine.connect() as c:
       c.execute(sqlalchemy.text('CREATE TABLE IF NOT EXISTS _drain_test (v TEXT)'))
       c.execute(sqlalchemy.text(\"INSERT INTO _drain_test VALUES ('persist-check')\"))
       c.commit()
   print('written')
   "
   ```

2. **Force a revision swap** (no-op env-var update triggers a new revision):
   ```powershell
   az containerapp update -g mom-bot -n ca-mom-bot --set-env-vars DRAIN_CHECK=$(Get-Date -Format o)
   ```

3. **Verify the row persists** (exec into the new revision):
   ```powershell
   az containerapp exec -g mom-bot -n ca-mom-bot --command "/bin/sh"
   # Inside the container:
   python -c "
   import sqlalchemy, os
   engine = sqlalchemy.create_engine(os.environ['MOM_BOT_DATABASE_URL'])
   with engine.connect() as c:
       rows = c.execute(sqlalchemy.text('SELECT v FROM _drain_test')).fetchall()
   print(rows)
   "
   ```
   Expected output: `[('persist-check',)]`

4. **Clean up:**
   ```python
   c.execute(sqlalchemy.text('DROP TABLE _drain_test'))
   c.commit()
   ```

---

### Snapshot restore drill

Run periodically to validate the recovery path is functional:

1. **Take a snapshot** (note the returned timestamp):
   ```powershell
   $snap = az storage share snapshot create `
     --account-name <storage-account-name> `
     --name mom-bot-data `
     --query snapshot -o tsv
   Write-Host "Snapshot: $snap"
   ```

2. **Modify the database** (simulate data loss/corruption — write a known-bad row
   as in step 1 of the persistence drill).

3. **Restore from snapshot** — copy the DB file out of the snapshot mount:
   ```powershell
   # List snapshot contents to confirm the file is present
   az storage file list --account-name <storage-account-name> `
     --share-name mom-bot-data --snapshot $snap --query "[].name" -o tsv

   # Copy the DB back from the snapshot (overwrites the live file)
   az storage file copy start `
     --account-name <storage-account-name> `
     --destination-share mom-bot-data `
     --destination-path mom_bot.db `
     --source-account-name <storage-account-name> `
     --source-share mom-bot-data `
     --source-path mom_bot.db `
     --source-snapshot $snap
   ```

4. **Force a revision restart** to reload the restored file:
   ```powershell
   az containerapp revision restart -g mom-bot -n ca-mom-bot --revision $(
     az containerapp revision list -g mom-bot -n ca-mom-bot --query "[0].name" -o tsv
   )
   ```

5. **Verify state** matches the pre-modification snapshot (re-run the read from
   step 3 of the persistence drill).

---

### Forward pointer — PostgreSQL migration

The SQLite-on-AzureFile setup is a **temporary stopgap**. The production target
is a managed PostgreSQL instance (Azure Database for PostgreSQL Flexible Server or
equivalent). Migration was tracked under Epic #91 (closed) — the full plan
was extracted to [#91 comment](https://github.com/glitchwerks/mom-bot/issues/91#issuecomment-4569696957)
and the plan file was removed in PR #247. The `prod-database-url` KV secret and
the `MOM_BOT_DATABASE_URL` env var are already wired to accept a PostgreSQL
connection string — no application code change is required for the migration,
only the secret value and the removal of the AzureFile volume wiring.
