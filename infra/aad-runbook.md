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
# Save this — it is passed as --parameters ghaServicePrincipalObjectId=$SpObjectId in Step 6.
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

## Step 5 — (No file edit needed — passed via CLI in Step 6)

The SP Object ID is captured in `$SpObjectId` from Step 2. We pass it directly
to `az deployment sub create` in the next step rather than committing it to
`infra/main.bicepparam`. This keeps the parameter file free of values that
vary by provisioning run.

---

## Step 6 — Deploy Bicep infrastructure

The `ghaServicePrincipalObjectId=$SpObjectId` override supplies the SP object ID captured in Step 2, so it never lives in the parameter file.

```powershell
az deployment sub create `
  --location eastus2 `
  --template-file infra/main.bicep `
  --parameters infra/main.bicepparam `
  --parameters ghaServicePrincipalObjectId=$SpObjectId `
  --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0 `
  --tenant 48bca6c3-6d4f-4884-bc1a-648ae2362a32
```

Bicep handles the Key Vault RBAC role assignments:

- `mi-mom-bot` → **Key Vault Secrets User** (runtime read-only)
- `mom-bot-gha` SP → **Key Vault Secrets Officer** (deploy-time read+write)

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

## Step 7 — Set GitHub repo variables

These are **Variables** (not Secrets — they are non-sensitive OIDC identifiers).
`gh variable set` writes them directly from the values captured in earlier steps.

```powershell
gh variable set AZURE_CLIENT_ID --body $AppId --repo glitchwerks/mom-bot
gh variable set AZURE_TENANT_ID --body 48bca6c3-6d4f-4884-bc1a-648ae2362a32 --repo glitchwerks/mom-bot
gh variable set AZURE_SUBSCRIPTION_ID --body 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0 --repo glitchwerks/mom-bot
```

---

## Step 8 — Grant yourself Key Vault access for local dev

Your own user account needs `Key Vault Secrets User` on the KV to read
`dev-*` secrets locally via `az login` + `DefaultAzureCredential`.

```powershell
$MyObjectId = az ad signed-in-user show --query id -o tsv

# Capture the KV resource ID if you did not run the manual fallback in Step 6.
$KvScope = az keyvault show --name kv-mombot-eastus2 --resource-group mom-bot --query id -o tsv

az role assignment create `
  --role "Key Vault Secrets User" `
  --assignee-object-id $MyObjectId `
  --assignee-principal-type User `
  --scope $KvScope
```

---

## Step 9 — Seed initial secrets

For each secret, paste the real value when prompted or supply it inline. Do not
commit actual token values. If you prefer to avoid the value appearing in your
terminal history, use `Read-Host -AsSecureString` and convert — but for a
one-time setup, plain inline paste is fine.

See `docs/secrets-inventory.md` for the full list of secret names and their
expected formats.

```powershell
# Discord bot token (prod)
az keyvault secret set `
  --vault-name kv-mombot-eastus2 `
  --name prod-discord-token `
  --value "<paste prod token>"

# Discord bot token (dev — your test bot or same token for local dev)
az keyvault secret set `
  --vault-name kv-mombot-eastus2 `
  --name dev-discord-token `
  --value "<paste dev token>"

# Database URL (prod — SQLite path on Container Apps volume, or Postgres later)
az keyvault secret set `
  --vault-name kv-mombot-eastus2 `
  --name prod-database-url `
  --value "sqlite:////data/mom_bot.db"

# Database URL (dev — local SQLite)
az keyvault secret set `
  --vault-name kv-mombot-eastus2 `
  --name dev-database-url `
  --value "sqlite:///./mom_bot_dev.db"

# App Insights connection string (placeholder — provisioning is Epic 1+)
# Leave these empty or set placeholder until App Insights is provisioned.
az keyvault secret set `
  --vault-name kv-mombot-eastus2 `
  --name prod-app-insights-conn-string `
  --value "PLACEHOLDER"

az keyvault secret set `
  --vault-name kv-mombot-eastus2 `
  --name dev-app-insights-conn-string `
  --value "PLACEHOLDER"
```

---

## Step 10 — Run the deploy workflow

Trigger the first deploy via GitHub Actions:

```
GitHub repo → Actions → Deploy (mom-bot) → Run workflow → Run workflow
```

The workflow (`deploy.yml`) runs `az containerapp update` to push the container
image to `ca-mom-bot`. First run succeeds if:

1. AAD federated credentials are in place (Steps 1–3)
2. Repo variables are set (Step 7)
3. A container image exists at `ghcr.io/glitchwerks/mom-bot:<sha>`

> Image build+push to GHCR is Epic 1 work. For v0 smoke testing, push a
> placeholder image manually and rerun.

---

## Summary checklist

- [ ] Step 1 — AAD app created; `$AppId` saved
- [ ] Step 2 — Service principal created; `$SpObjectId` saved
- [ ] Step 3a — Federated credential for `main` push created
- [ ] Step 3b — Federated credential for pull requests created
- [ ] Step 5 — (acknowledged — no file edit needed)
- [ ] Step 6 — Bicep deployed successfully (with `ghaServicePrincipalObjectId=$SpObjectId` override)
- [ ] Step 7 — Repo variables set in GitHub
- [ ] Step 8 — Your user account has `Key Vault Secrets User`
- [ ] Step 9 — Initial secrets seeded
- [ ] Step 10 — First `workflow_dispatch` run of `deploy.yml` succeeds
