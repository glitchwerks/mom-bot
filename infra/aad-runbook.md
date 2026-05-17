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
# Save this — it is exported as $env:GHA_SP_OBJECT_ID in Step 5.
```

---

## Step 3 — Add federated credentials (OIDC trust)

Two federated credentials are required for the A++ dev/prod model.

The JSON body is written to a temp file to avoid PowerShell interpolating
dollar-signs inside the JSON. The closing `'@` must be at column 0 — no
leading whitespace.

### 3a — Trust pushes to `main`

```powershell
$FedCredMain = @'
{
  "name": "mom-bot-main-push",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:glitchwerks/mom-bot:ref:refs/heads/main",
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
  "subject": "repo:glitchwerks/mom-bot:pull_request",
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

## Step 4 — Export the SP Object ID as an environment variable

The SP Object ID is captured in `$SpObjectId` from Step 2. Export it as
`$env:GHA_SP_OBJECT_ID` before running the deploy so that `infra/main.bicepparam`
can read it via `readEnvironmentVariable('GHA_SP_OBJECT_ID')`. This satisfies
Bicep's strict `.bicepparam` contract (every declared param must have an
assignment) while keeping the value out of the repo.

```powershell
$env:GHA_SP_OBJECT_ID = $SpObjectId
```

---

## Step 5 — Deploy Bicep infrastructure

The `$env:GHA_SP_OBJECT_ID` env var (exported in Step 4) is read by
`infra/main.bicepparam` via `readEnvironmentVariable('GHA_SP_OBJECT_ID')`,
supplying the SP object ID captured in Step 2. This satisfies Bicep's strict
bicepparam contract (every declared param must have an assignment) without
committing the value to the repo.

The pre-flight guard catches the most common deploy failure mode: forgetting to re-export
`GHA_SP_OBJECT_ID` after a `git pull` or in a fresh terminal session. Without it,
`readEnvironmentVariable` returns the empty-string default, the KV role assignment receives
an empty `principalId`, and Azure rejects the deploy 90 seconds in with `InvalidPrincipalId`.
Failing at the pre-flight saves a few minutes of wasted deploy time.

```powershell
$env:GHA_SP_OBJECT_ID = $SpObjectId

# Pre-flight: refuse to deploy if the env var didn't survive (e.g. fresh shell)
if (-not $env:GHA_SP_OBJECT_ID) {
  throw "GHA_SP_OBJECT_ID is not set. Export it from `$SpObjectId (see Step 2) before re-running."
}

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
- `mom-bot-gha` SP → **Key Vault Secrets Officer** (deploy-time read+write)
- `mom-bot-gha` SP → **Container Apps Contributor** at RG `mom-bot` scope (granted by Bicep at deploy time; required for `az containerapp update` in `deploy.yml`)

If the Bicep role assignments fail (e.g. `ghaServicePrincipalObjectId` was wrong),
assign manually:

```powershell
$KvId = az keyvault show -g mom-bot -n kv-mombot-eastus2 --query id -o tsv

# Key Vault Secrets Officer for the GHA service principal
az role assignment create `
  --role "Key Vault Secrets Officer" `
  --assignee-object-id $SpObjectId `
  --assignee-principal-type ServicePrincipal `
  --scope $KvId

# Key Vault Secrets User for the managed identity
$MiPrincipalId = az identity show -g mom-bot -n mi-mom-bot --query principalId -o tsv
az role assignment create `
  --role "Key Vault Secrets User" `
  --assignee-object-id $MiPrincipalId `
  --assignee-principal-type ServicePrincipal `
  --scope $KvId
```

---

## Step 6 — Set GitHub repo variables

These are **Variables** (not Secrets — they are non-sensitive OIDC identifiers).
`gh variable set` writes them directly from the values captured in earlier steps.

```powershell
gh variable set AZURE_CLIENT_ID --body $AppId --repo glitchwerks/mom-bot
gh variable set AZURE_TENANT_ID --body 48bca6c3-6d4f-4884-bc1a-648ae2362a32 --repo glitchwerks/mom-bot
gh variable set AZURE_SUBSCRIPTION_ID --body 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0 --repo glitchwerks/mom-bot
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

Each Discord server has its own ID. Right-click your server name in Discord
(with Developer Mode on: User Settings → Advanced → Developer Mode) → "Copy
Server ID".

Guild IDs are public identifiers — they are safe to echo to the terminal.

```powershell
$devGuildId  = Read-Host "Paste your DEV guild ID (test server)"
$prodGuildId = Read-Host "Paste your PROD guild ID (real server)"

az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-guild-id  --value $devGuildId  | Out-Null
az keyvault secret set --vault-name kv-mombot-eastus2 --name prod-guild-id --value $prodGuildId | Out-Null
```

### Database URL — different per environment

```powershell
# Local dev: SQLite file in the working directory
az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-database-url  --value "sqlite:///./mom_bot_dev.db" | Out-Null

# Prod: SQLite on Container Apps volume (placeholder for PostgreSQL in Epic 1+)
az keyvault secret set --vault-name kv-mombot-eastus2 --name prod-database-url --value "sqlite:////data/mom_bot.db" | Out-Null
```

### App Insights connection string — placeholder until Epic 1

```powershell
az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-app-insights-conn-string  --value "PLACEHOLDER" | Out-Null
az keyvault secret set --vault-name kv-mombot-eastus2 --name prod-app-insights-conn-string --value "PLACEHOLDER" | Out-Null
```

### Reminder scheduler secrets — channel name (no Developer Mode required)

Both the Hydra and Chimera reminders fire to the **same channel** in each
environment (collapsed from two per-reminder secrets in #43). Reminders post
without pinging any role (#45). The secret value is the **channel name** as
a plain string — no Developer Mode, no right-click, no snowflake copy (#47).

```powershell
$devReminderChannelName  = Read-Host "Paste your DEV reminder channel NAME (e.g. reminders)"
$prodReminderChannelName = Read-Host "Paste your PROD reminder channel NAME (e.g. reminders)"

az keyvault secret set --vault-name kv-mombot-eastus2 --name dev-reminder-channel-name  --value $devReminderChannelName  | Out-Null
az keyvault secret set --vault-name kv-mombot-eastus2 --name prod-reminder-channel-name --value $prodReminderChannelName | Out-Null
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
- `dev-guild-id`, `prod-guild-id`
- `dev-database-url`, `prod-database-url`
- `dev-app-insights-conn-string`, `prod-app-insights-conn-string`
- `dev-reminder-channel-name`, `prod-reminder-channel-name`

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
- [ ] Step 4 — `$env:GHA_SP_OBJECT_ID` exported in session
- [ ] Step 5 — Bicep deployed successfully (parameter file validated with `az bicep build-params` first)
- [ ] Step 6 — Repo variables set in GitHub
- [ ] Step 7 — Grant yourself Key Vault Secrets Officer for seeding
- [ ] Step 8 — Initial secrets seeded

**Post-merge (validation):**
- [ ] Step 9 — Run the deploy workflow (`workflow_dispatch` on `deploy.yml` from `main`)
- [ ] Step 10 — Manually deactivate old revisions (stopgap until #83 automates it)

---

## AzureFile-backed SQLite (Policy decisions from #87)

This section documents the operator policies that govern the SQLite-on-AzureFile
stopgap introduced in PR #87. These policies are load-bearing — violating them
risks database corruption.

---

### Backing store choice

**Storage type:** Azure File Share, Standard LRS
**Cost:** ~$0.25–1.10/month for a ≤ 1 GiB share

EmptyDir (the default Container Apps ephemeral volume) loses all state on every
revision swap — unacceptable for a database. PostgreSQL is the correct long-term
answer (see Epic 1+, tracking issue TBD), but standing up a managed Postgres
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
equivalent). Migration is tracked under Epic 1+; the specific tracking issue is
TBD (see follow-up issue "Track PostgreSQL migration epic (replaces
SQLite-on-SMB stopgap)" tracked by issue #87, implemented in PR #92). The `prod-database-url` KV secret and
the `MOM_BOT_DATABASE_URL` env var are already wired to accept a PostgreSQL
connection string — no application code change is required for the migration,
only the secret value and the removal of the AzureFile volume wiring.
