---
title: Bicep deploy automation + ingress-less revision cleanup (closes #83, #96)
touches:
  - .github/workflows/deploy-infra.yml
  - .github/workflows/infra-what-if.yml
  - .github/workflows/deploy.yml
  - infra/aad-runbook.md
  - infra/main.bicepparam
  - scripts/deactivate-old-revisions.sh
skills_relevant:
  - github-actions
  - bicep
  - azure
  - powershell
---

# Bicep deploy automation + ingress-less revision cleanup

Closes [#83](https://github.com/glitchwerks/mom-bot/issues/83) (CI Bicep deploy step) and [#96](https://github.com/glitchwerks/mom-bot/issues/96) (ingress-less Container Apps revision rollover). The two issues are coupled: the proper fix for #96 (automated post-deploy old-revision deactivation) lives inside the new infra-deploy workflow that #83 calls for.

## 1. Background and problem statement

### #83 — Bicep drift between merge and live infra

`.github/workflows/deploy.yml:L61-L66` only runs `az containerapp update --image`. No workflow runs `az deployment sub create` against `infra/main.bicep`. Bicep-only PRs merge green and CI passes, but live infra stays on whatever was last manually deployed from a workstation. This drift caused the PR #82 / #81 recurrence (identity config mismatch between `main.bicep` and the live `ca-mom-bot` resource), since later resolved in PR #86. The pattern will recur until merging Bicep to `main` is itself the act of deployment.

### #96 — Two active revisions on every image deploy

`infra/modules/containerapp.bicep:L152` sets `activeRevisionsMode: 'Single'`, but the Container App is **ingress-less** (`ingress` block disabled at `containerapp.bicep:L150`; see PR #88 which moved the liveness probe to `/healthz` after the ingress-less rejection in PR #81/#82). For ingress-less apps, Azure Container Apps' `Single` mode does **not** auto-deactivate old revisions on image swap — there is no HTTP traffic-routing event to gate the swap on (Azure docs: revision modes describe traffic splitting for ingress-enabled apps; ingress-less apps have no traffic to split). Result: every `az containerapp update --image` leaves the prior revision active. Both replicas mount the same AzureFile share (PR #92), and SQLite WAL locks contend until the old revision is manually deactivated. Verified two active revisions post-deploy via `az containerapp revision list` (#96 body).

#96 lists three fix options:

1. **Automate in deploy workflow** (preferred long-term) — run `az containerapp revision deactivate` against any revision other than the latest after `az containerapp update` returns.
2. **Runbook step** (stopgap) — document the manual command until #1 lands.
3. **Health-aware variant of #1** — wait for the new revision's `/healthz` to return 200 before deactivating the old one.

## 2. Design decisions

### 2.1 Auto-apply on `main` vs manual environment-protection gate

**Decision: auto-apply on push to `main`** (no environment protection rule).

Rationale: parity with the existing image-deploy workflow at `deploy.yml`, which is already auto-apply (`workflow_dispatch` from default branch is one click, no second-stage gate). The user runs a single-developer A++ prod model (`deploy.yml:L3-L5`) — there is no staging environment, and the PR merge to `main` *is* the review event. Adding a manual gate creates latency for no incremental review benefit, and would require a new federated credential of subject form `repo:glitchwerks/mom-bot:environment:prod`. The aad-runbook explicitly notes (per the brief) that no `:environment:` federations are configured today; staying with the existing `refs/heads/main` federation is simpler.

Trade-off accepted: a broken Bicep merge will reach live infra without human review of the apply step. Mitigated by Phase 1 (what-if preview on every PR — see 2.2).

### 2.2 What-if PR preview: blocking vs informational

**Decision: informational-only.**

Rationale: `az deployment sub create --what-if` regularly false-positives on read-only fields — output blocks, system-managed timestamps, role-assignment GUIDs that Azure regenerates on update, identity propagation deltas. A blocking what-if check forces PR authors to chase noise and erodes trust in the signal. The valuable property of what-if is the human-readable diff posted to the PR; the author and reviewer read it and decide. Posting as a sticky PR comment (or workflow check summary) preserves that signal without making merge contingent on it.

If, in practice, what-if proves quiet enough to make blocking worthwhile, the trigger can be flipped later by adding `if: failure() && contains(steps.whatif.outputs.changes, '...')` gates.

### 2.3 Drift exclusions

**Decision: no exclusions today.** The only known drift incident (identity config, PR #82/#86) was reconciled into `main.bicep` and is no longer a divergence. Revisit only if a specific resource surfaces as a chronic drift source. Documenting an empty exclusion list now would invite cargo-cult additions.

### 2.4 New workflow file vs extending `deploy.yml`

**Decision: separate `deploy-infra.yml`.**

Rationale: the two workflows have different triggers (path-filtered push on `infra/**` vs `workflow_dispatch`), different verbs (`az deployment sub create` vs `az containerapp update --image`), different failure modes (Bicep compile/role-assignment vs image pull/start), and different rollback semantics (re-deploy prior Bicep vs `--image` to prior SHA). Conflating them complicates both. Separation also makes the what-if PR workflow (`infra-what-if.yml`) a natural sibling.

The old-revision deactivation logic (#96) lives in a small reusable script (`scripts/deactivate-old-revisions.sh`) called from `deploy.yml` (post `az containerapp update`) — not from `deploy-infra.yml`, because `az deployment sub create` of the current `main.bicep` does **not** create a new revision (it deploys the same `containerImage` parameter unless overridden). Old revisions accumulate from image deploys, not infra deploys, so the deactivation step belongs in the image-deploy workflow. This is a refinement of the brief's "Phase 2" framing — see 3.3.

### 2.5 Health-aware deactivation (#96 option 3)

**Decision: include health gate in Phase 2, gated on `properties.runningState == 'Running'`** (not `properties.healthState`).

Rationale: PR #88 added `/healthz` (httpGet liveness probe) precisely so revision health is externally observable. Deactivating an old revision before the new one is serving means a window where no replica is healthy — minimal risk for a non-user-facing bot, but free to avoid. The gate is a polling loop bounded by `MAX_ITER` per CLAUDE.md "Background Polling".

**Gate-field selection (corrected from prior draft).** `properties.healthState` on the revision resource is only populated for ingress-enabled Container Apps; for ingress-less apps it is typically empty/null and is the wrong field to gate on. The correct revision-level signal is `properties.runningState` — values include `Processing`, `Running`, `RunningAtMaxScale`, `Scaling`, `Stopped`, `Degraded`, `Failed`. We gate on `runningState == 'Running'` (or `RunningAtMaxScale`).

**Verification required before Phase 2 ships — three-branch decision tree.** During Phase 2 implementation, the first step is to run

```bash
az containerapp revision show --name ca-mom-bot --resource-group mom-bot \
  --revision <current-active-revision> --query properties -o json
```

against the live app, capture the populated fields, paste them into the Phase 2 PR body, and pick exactly one of the following branches:

- **Branch (a) — revision-level `runningState` populated.** Use `properties.runningState` on the revision resource as the gate condition. This is the preferred path. Script polls `az containerapp revision show --query properties.runningState -o tsv`.
- **Branch (b) — revision-level empty, replica-level populated.** If `properties.runningState` is null/empty on the revision but `az containerapp replica list --revision <name> --query "[].properties.runningState"` returns non-empty values, gate on **all replicas** reporting `Running` (or `RunningAtMaxScale`). Script polls the replica list and reduces with an AND across replicas.
- **Branch (c) — both empty.** Block Phase 2 implementation. File a follow-up issue documenting the field-availability gap on ingress-less Container Apps. Ship Phase 2 with a stopgap: fixed 90-second post-deploy wait + a `WARNING: gating on fixed timer, not runningState — see issue #<N>` log line. Treat this as a known-bad gate to be replaced once the field-availability question is understood.

**Do not ship Phase 2's deactivation script without explicitly recording which branch was chosen and why** (acceptance criterion on Phase 2's list).

The Container App is ingress-less — `/healthz` is not reachable from the workflow runner externally. The script polls the ARM resource, not the HTTP endpoint.

## 3. Phasing

### Phase 0 — #96 runbook stopgap (doc-only, ship immediately)

**Goal:** documented manual procedure to deactivate stale revisions until Phase 2 automates it.

**Touches:**
- `infra/aad-runbook.md` — insert new Step 10 between current Step 9 (ends `aad-runbook.md:L355`) and the `## Notes` section (`aad-runbook.md:L358`).

**Content:**

New section titled `## Step 10 — Manual old-revision deactivation (#96 stopgap)`, with PowerShell snippet from the #96 body verbatim:

```powershell
$active = az containerapp revision list `
  --name ca-mom-bot --resource-group mom-bot `
  --query "[?properties.active].name" -o tsv
$latest = az containerapp revision list `
  --name ca-mom-bot --resource-group mom-bot `
  --query "sort_by([?properties.active], &properties.createdTime)[-1].name" -o tsv
$active -split "`n" | Where-Object { $_ -and $_ -ne $latest } | ForEach-Object {
  az containerapp revision deactivate --name ca-mom-bot --resource-group mom-bot --revision $_
}
```

Add a one-line note: "Automation tracked in [#83](https://github.com/glitchwerks/mom-bot/issues/83); see `deploy.yml` and `scripts/deactivate-old-revisions.sh` once that work lands." This deliberately avoids naming a specific procedure that Phase 2 will then make stale — Phase 2 rewrites this section as part of its atomic runbook reconcile (see Phase 2 scope).

**Acceptance:**
- New section appears between Step 9 and `## Notes`
- Updates `## Summary checklist` (`aad-runbook.md:L378+`) with a "Step 10 (post-deploy cleanup)" bullet
- PR body references both #96 (partial close: "stopgap landed; full automation tracked in #83") and #83 (cross-reference)

**Branch:** `docs-96-revision-deactivation-stopgap`
**PR scope:** doc-only, ~30 lines

---

### Phase 0.5 — Grant SP subscription-scope deploy permission (operator-run, one-off)

**Goal:** grant `mom-bot-gha` the **least-privilege** role required for `az deployment sub create` at subscription scope. This unblocks Phase 1's what-if (which calls the same ARM what-if API at read+validate level) and Phase 2's apply (which exercises the write permission).

**Why this is a separate phase.** `mom-bot-gha` currently only holds RG-scoped Container Apps Contributor on `mom-bot` (granted by `infra/modules/containerapp.bicep:L238-L248`). `az deployment sub create` requires `Microsoft.Resources/deployments/write` at **subscription** scope — RG-scope is insufficient because the deployment resource itself is created at sub scope (even when all child resources are RG-scoped). The manual workstation deploys documented in `aad-runbook.md` Step 5 work because the operator's own AAD account holds Owner on the subscription; **the SP cannot bootstrap itself with the role it needs.** Granting this role requires Owner or User Access Administrator at sub scope and so must be operator-run, not automated.

**Touches:**
- `infra/aad-runbook.md` — add a new step (after Step 4, before Step 5) titled `## Step 4.5 — Grant SP subscription-scope deploy permission`

**Procedure (operator-run, PowerShell) — least-privilege custom role (primary path):**

The SP needs exactly two permissions at sub scope: `Microsoft.Resources/deployments/*` (validate, what-if, write, read, delete on the deployment resource itself) and `Microsoft.Resources/resourceGroups/read` (so the deployment engine can resolve the target RG). All child-resource writes happen via the existing RG-scoped Container Apps Contributor grant on `mom-bot`. Built-in `Contributor` is over-broad (write access across the entire subscription) and is rejected as the primary path.

Step 1 — write the role definition JSON to a temp file. Use the verbatim body:

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

Note: the action name includes the `subscriptions/` segment — Azure rejects `Microsoft.Resources/resourceGroups/read` with `InvalidActionOrNotAction`.

Step 2 — create the role definition and assign it:

```powershell
# 1. Inspect current role assignments (baseline)
az role assignment list --assignee $env:AZURE_CLIENT_ID `
  --scope /subscriptions/213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0 -o table

# 2. Create the custom role definition (one-time per subscription)
#    Save the JSON above to a temp file first, e.g. $env:TEMP\mom-bot-gha-deployer.json
az role definition create --role-definition $env:TEMP\mom-bot-gha-deployer.json

# 3. Assign the custom role to the SP at subscription scope
az role assignment create `
  --assignee $env:AZURE_CLIENT_ID `
  --role "Mom-bot GHA Subscription Deployer" `
  --scope /subscriptions/213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0
```

**Fallback (dated exception, requires follow-up issue).** If `az role definition create` fails for unrelated reasons (tenant policy denying custom role creation, naming collision, propagation delays not resolvable inside the deploy window) and the operator needs to unblock work, grant built-in `Contributor` at sub scope as an expedient — but file a follow-up issue immediately to replace it with the custom role within the next sprint. Do not leave Contributor in place silently.

**Acceptance:**
- `az role assignment list --assignee $AZURE_CLIENT_ID --scope /subscriptions/213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0 -o table` shows exactly the custom role `Mom-bot GHA Subscription Deployer` at sub scope and Container Apps Contributor at RG-scope `mom-bot`, alongside the existing Key Vault Secrets Officer grant on `kv-mombot-eastus2` (Step 7); no other unexplained assignments (no Owner, no built-in Contributor, no leftover grants)
- The role definition and assignment are documented in `aad-runbook.md` as Step 4.5, including the verbatim JSON body
- If the fallback Contributor path was used, the runbook records the date and links the follow-up issue

**Branch / PR:** `infra-83-sp-rbac-grant` — ships as a doc-only PR (runbook edit only; the operator action itself is out-of-band) separate from Phase 1. Rationale: the role-grant change is auditable independently from the workflow that exercises it, and rolling it into Phase 1's PR would mix doc + workflow + operator-action context. The PR body links the role-assignment evidence (`az role assignment list` output, redacted as needed).

---

### Phase 1 — What-if PR preview workflow (#83 partial)

**Goal:** every PR touching `infra/**` posts a `what-if` diff so the reviewer sees what merging would change in live infra.

**Touches:**
- `.github/workflows/infra-what-if.yml` (new)

**Workflow shape:**

- `on: pull_request: paths: ['infra/**']`
- `permissions: { id-token: write, contents: read, pull-requests: write }` (last needed to post the comment)
- Steps:
  1. `actions/checkout`
  2. `azure/login@v2` with OIDC (reuse `vars.AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` from `deploy.yml:L37-L40`)
  3. **Export `GHA_SP_OBJECT_ID` to subsequent steps via `$GITHUB_ENV`** — *not* bare `export`. Bare `export` only persists for the lifetime of one step's shell; subsequent steps run in fresh shells. `aad-runbook.md` Step 4 uses interactive `$env:GHA_SP_OBJECT_ID = ...` which works for a single operator session but not across CI steps. The required shape is:

     ```yaml
     - name: Resolve SP object ID for Bicep param
       run: |
         # az ad sp show returns the *service principal* object ID (the
         # enterprise-app object in the tenant) — distinct from the *app
         # registration* object ID returned by `az ad app show`. Bicep
         # role assignments require the SP object ID, which is what
         # main.bicepparam expects via GHA_SP_OBJECT_ID.
         SP_OID=$(az ad sp show --id ${{ vars.AZURE_CLIENT_ID }} --query id -o tsv)
         echo "GHA_SP_OBJECT_ID=$SP_OID" >> "$GITHUB_ENV"

     - name: Verify export persisted
       run: |
         test -n "$GHA_SP_OBJECT_ID" || { echo "::error::GHA_SP_OBJECT_ID empty in subsequent step"; exit 1; }
         echo "SP OID present (length=${#GHA_SP_OBJECT_ID})"
     ```

     The verification step proves the persistence mechanism worked and is an acceptance criterion below.

  4. `az deployment sub create --what-if --location eastus2 --template-file infra/main.bicep --parameters infra/main.bicepparam --subscription ${{ vars.AZURE_SUBSCRIPTION_ID }}` — capture stdout to a file. (`--subscription` from `vars` for parity with `deploy.yml:L40`; no hardcoded GUID.)
  5. Post as sticky PR comment via `marocchino/sticky-pull-request-comment@v2` (pinned by SHA per CLAUDE.md GitHub-Actions hygiene)
- Comment body wraps the what-if output in a collapsible `<details>` block with a Claude attribution footer per CLAUDE.md "GitHub Comments"

**`containerImage` parameter handling (resolves reviewer finding on `main.bicepparam:L23`).** The static `mcr.microsoft.com/k8se/quickstart:latest` in `main.bicepparam:L23` is a stable placeholder that diverges from the live image (which is `ghcr.io/glitchwerks/mom-bot:<sha>` set by `az containerapp update`). Left as-is, every what-if would show a phantom `containerImage` diff that trains reviewers to ignore the whole comment.

**Decision: option (a) — read `containerImage` from env var, defaulting to the live image.** Change `main.bicepparam:L23` from a static string to:

```bicep
param containerImage = readEnvironmentVariable('CONTAINER_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
```

The default preserves the quickstart fallback for cold-start scenarios where no image has been deployed yet. Phase 1 and Phase 2 workflows resolve the current live image before the Bicep step:

```yaml
- name: Resolve current live container image
  run: |
    IMG=$(az containerapp show --name ca-mom-bot --resource-group mom-bot \
      --query "properties.template.containers[0].image" -o tsv)
    echo "CONTAINER_IMAGE=$IMG" >> "$GITHUB_ENV"
```

**Implications check (other deploy paths).** The two paths that consume `main.bicepparam` are: (1) operator workstation `az deployment sub create` per `aad-runbook.md` Step 5, and (2) the new Phase 2 `deploy-infra.yml`. Neither relies on the static quickstart value: the operator path is post-bootstrap (an image always exists) and Phase 2 explicitly resolves the live image as above. The image-deploy workflow (`deploy.yml`) does not consume the bicepparam at all — it calls `az containerapp update --image` directly. The `readEnvironmentVariable` fallback preserves the cold-start behavior for the original bootstrap deploy. Conclusion: option (a) is safe; option (b) was rejected because phantom diffs train reviewers to ignore real diffs.

**Acceptance criteria (mapped from #83):**
- [ ] PR-only — does not run on push to `main` (that's Phase 2)
- [ ] OIDC reuses `mom-bot-gha` app registration; no new secrets
- [ ] Workflow exports `GHA_SP_OBJECT_ID` via `$GITHUB_ENV`, verified by a subsequent step echoing the value's length (proves cross-step persistence)
- [ ] First Bicep-only PR after merge has a what-if comment visible to the reviewer
- [ ] What-if output does **not** show a `containerImage` phantom diff (the env-var resolution above is wired correctly)
- [ ] PR description for this workflow includes a sample what-if trace from a no-op PR (proves end-to-end)
- [ ] **Auth reachability check:** Phase 1 proves the SP can authenticate via OIDC and reach the ARM what-if API (`Microsoft.Resources/deployments/validate` + read). If the first PR-trigger run 403s at `az deployment sub create --what-if`, diagnose as missing read/validate RBAC on `mom-bot-gha`. **Note: Phase 1 does NOT verify `Microsoft.Resources/deployments/write`** — `--what-if` is read+validate only. A SP missing the write permission will succeed here and 403 only at Phase 2 apply. Write-permission verification belongs to Phase 0.5's role-assignment check (direct `az role assignment list`) and to Phase 2's first apply.

**Branch:** `infra-what-if-workflow`
**PR scope:** single workflow file + sample-output evidence in PR body

---

### Phase 2 — Main-branch infra deploy + revision deactivation + runbook reconcile (closes #83, #96)

**Goal:** merge-to-`main` triggers Bicep apply; image-deploy workflow automatically cleans up old revisions afterwards; runbook reflects the as-built state — all in one atomic PR.

**Phase 3 folded into Phase 2 (reviewer finding #7, decision: option a).** The runbook reconcile was originally a separate Phase 3 to ship after Phase 2 verified live. That separation creates a stale-doc window: Phase 0 ships a Step 10 procedure that becomes wrong the moment Phase 2 lands, and the runbook describes a manual flow until the operator gets around to the follow-up doc PR. Folding the reconcile into the same PR as the automation makes them atomic — the workflow and the doc describing it land together, the only window of staleness is between PR open and PR merge (closed by review), and there is no "I forgot to ship Phase 3" failure mode. The reconcile is small (~30 lines of edits to `aad-runbook.md`); the PR scope concern (mixed code+doc) is outweighed by the atomicity. Phase 0's deliberate vagueness ("automation tracked in #83") makes this fold cheap because Phase 0 doesn't document a procedure that Phase 2 has to undo.

**Touches:**
- `.github/workflows/deploy-infra.yml` (new)
- `.github/workflows/deploy.yml` (extend — add deactivation step + concurrency block)
- `scripts/deactivate-old-revisions.sh` (new — reusable from any workflow or interactive shell)
- `infra/aad-runbook.md` (reconcile Steps 9 and 10 to describe the as-built workflow surface; **also rewrite `## Notes / Placeholder container image` subsection at `aad-runbook.md:L362-L374`** — see "runbook reconcile" below)
- `infra/main.bicepparam` (`containerImage` switched to `readEnvironmentVariable` per Phase 1 finding)

**`deploy-infra.yml` shape:**

- `on: push: branches: [main], paths: ['infra/**']`
- Same OIDC + `$GITHUB_ENV` export pattern as Phase 1 (`echo "GHA_SP_OBJECT_ID=..." >> "$GITHUB_ENV"`, plus the `CONTAINER_IMAGE` resolution from live state)
- Step: `az deployment sub create --location eastus2 --template-file infra/main.bicep --parameters infra/main.bicepparam --subscription ${{ vars.AZURE_SUBSCRIPTION_ID }}` (no hardcoded GUID — parity with `deploy.yml:L40`)
- Concurrency group: `infra-deploy` with `cancel-in-progress: false` — back-to-back infra merges should serialize, not cancel mid-deploy
- No `--confirm-with-what-if` flag — that re-runs what-if pre-apply and prompts interactively, which doesn't fit headless CI

**Note on `bicep build` and `CONTAINER_IMAGE` absence.** `readEnvironmentVariable('CONTAINER_IMAGE', '<default>')` with a default value **does not fail at `bicep build` / `bicep build-params` time** when the env var is absent — the fallback string is substituted. This is intentional: build-time validation cannot catch a missing `CONTAINER_IMAGE` because the parameter file is syntactically valid regardless. Runtime presence is enforced by the workflow itself via the `az containerapp show ... >> $GITHUB_ENV` step preceding the Bicep call (Phase 1 step in §3 above); if that step fails or returns empty, the deploy uses the quickstart fallback, which is the desired cold-start behavior. See `aad-runbook.md:L151-L154` so operators understand that build-time silence is not a missing-check bug.

**`scripts/deactivate-old-revisions.sh` shape:**

POSIX shell (sub-agents run Bash per CLAUDE.md). Inputs: `--app ca-mom-bot --rg mom-bot --new-revision <name>` (or auto-detect newest). Logic:

1. **First, verify the gate field is observable on this app** (see §2.5). The implementer runs `az containerapp revision show ... --query properties -o json` against the current live revision, pastes the populated fields into the PR body, and confirms either `properties.runningState` is populated *or* falls back to replica-level `runningState` via `az containerapp replica list --revision <name>`. The script's polling query is finalized only after this verification.
2. Poll the **new** revision's `properties.runningState` via `az containerapp revision show --query properties.runningState -o tsv` until value matches `Running` or `RunningAtMaxScale`, or `MAX_ITER` (cap ~60 iterations × 5s = 5min, per CLAUDE.md "Generic polling" pattern). **Do not use `properties.healthState`** — it is only populated for ingress-enabled apps and this app is ingress-less.
3. List active revisions: `az containerapp revision list --name <app> --resource-group <rg> --query "[?properties.active].name" -o tsv`
4. For each name != new-revision: `az containerapp revision deactivate --name <app> --resource-group <rg> --revision <name>`
5. Exit nonzero with a loud message if the new revision never reaches the running state — do not deactivate old revisions in that case

**`deploy.yml` extension:**

Add a top-level `concurrency` block (no existing block on this workflow — verified against `deploy.yml:L1-L67`):

```yaml
concurrency:
  group: image-deploy
  cancel-in-progress: false
```

**Rationale for `cancel-in-progress: false` on `deploy.yml`.** `deploy.yml` is `workflow_dispatch`-triggered with an explicit `commit_sha` input — the operator chose that SHA at dispatch time. A second dispatch queued behind a running one represents a deliberate second decision, not a noisy re-trigger, so cancelling the in-flight deploy would defeat the operator's intent. We accept the stale-queued-deploy failure mode (if the queued deploy lands after a manual rollback, it will reapply the now-undesired image) as the cost of honoring explicit dispatch inputs. Contrast with `deploy-infra.yml`, where `cancel-in-progress: false` is purely about serialization of apply ordering on `main` push — there is no per-trigger input the operator authored.

After existing `Deploy container image to prod` step (`deploy.yml:L61-L66`):

```yaml
      - name: Deploy container image to prod
        id: image_deploy
        run: |
          # --output json so we can extract latestRevisionName from the
          # update response directly. Using sort_by JMESPath on
          # `revision list` to find the "newest" revision has a race
          # window where a concurrent revision creation could steer the
          # deactivation script at the wrong target. The update response
          # is the authoritative source.
          UPDATE_JSON=$(az containerapp update \
            --name ca-mom-bot \
            --resource-group mom-bot \
            --image ghcr.io/glitchwerks/mom-bot:${{ steps.resolve_sha.outputs.sha }} \
            --output json)
          NEW_REV=$(echo "$UPDATE_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["properties"]["latestRevisionName"])')
          echo "new_revision=$NEW_REV" >> "$GITHUB_OUTPUT"

      - name: Deactivate prior revisions (#96)
        run: scripts/deactivate-old-revisions.sh --app ca-mom-bot --rg mom-bot --new-revision "${{ steps.image_deploy.outputs.new_revision }}"
```

Note: `properties.latestRevisionName` from the `az containerapp update` response is the authoritative identifier for the revision that the update just created. The prior plan draft used `sort_by([?properties.active], &properties.createdTime)[-1]` on `revision list`, which has a small race window if any other revision-creating operation runs concurrently. The `update` response closes that window.

**`infra/aad-runbook.md` reconcile (folded from former Phase 3):**

- **Step 9** (`aad-runbook.md:L333-L355`) — rewrite to describe two workflows (`deploy.yml` for image; `deploy-infra.yml` for Bicep, auto-fired on `infra/**` push to `main`). The manual "GitHub repo → Actions → Deploy → Run workflow" instruction stays as the image-rollout path but a new sub-section explains that Bicep changes deploy automatically on merge.
- **Step 10** (added in Phase 0) — convert from "manual stopgap" to "fallback if automation fails": keep the snippet, prefix with "Automated by `scripts/deactivate-old-revisions.sh` invoked from `deploy.yml`. Run manually only if a deploy fails partway through revision cleanup."
- **Summary checklist** — update Step 9/10 bullets
- **`## Notes / Placeholder container image` subsection** (`aad-runbook.md:L362-L374`) — rewrite to describe the new `containerImage` resolution model: (a) `main.bicepparam:L23` now uses `readEnvironmentVariable('CONTAINER_IMAGE', '<quickstart>')` as a fallback, so absence of the env var no longer fails the build; (b) both workflows resolve the live image via `az containerapp show ... --query properties.template.containers[0].image -o tsv` and export it as `CONTAINER_IMAGE` for the Bicep step; (c) on the operator path, inline `--parameters containerImage=...` still works and takes precedence over the env var, which is the documented escape hatch for forcing a specific image during recovery
- Covers #83 acceptance item 4: "`infra/aad-runbook.md` updated to reflect new workflow's role + permissions"

**Acceptance criteria (mapped from #83 + #96):**
- [ ] `az deployment sub create` runs on push to `main` when `infra/**` changes (#83)
- [ ] OIDC reuses `mom-bot-gha` (#83)
- [ ] `deploy.yml` has top-level `concurrency: { group: image-deploy, cancel-in-progress: false }` (added in this PR — no prior block exists per `deploy.yml:L1-L67`)
- [ ] `deploy-infra.yml` has `concurrency: { group: infra-deploy, cancel-in-progress: false }`
- [ ] Both new/updated workflows export `GHA_SP_OBJECT_ID` via `$GITHUB_ENV` (not bare `export`) and verify cross-step persistence
- [ ] No hardcoded subscription GUID — both workflows use `${{ vars.AZURE_SUBSCRIPTION_ID }}`
- [ ] Deactivation script resolves the new revision via `az containerapp update --output json | ... latestRevisionName`, **not** via JMESPath sort on `revision list`
- [ ] A Bicep-only PR verifiable end-to-end without a manual workstation `az` invocation (#83 — Phase 1 covers PR-preview, Phase 2 covers apply)
- [ ] After image deploy, only one revision is active on `ca-mom-bot` (#96)
- [ ] Deactivation script gates on `properties.runningState == 'Running'` (or `RunningAtMaxScale`) — **not** `healthState`. Gate-field verification evidence (`az containerapp revision show ... --query properties -o json` against a live revision) is pasted into the PR body before merge (#96 option 3).
- [ ] `infra/aad-runbook.md` Steps 9 and 10 reflect the new automated surface (folded Phase 3)
- [ ] PR description includes a what-if trace from a no-op change (#83) — produced from the Phase 1 workflow on this PR itself
- [ ] PR description shows the post-deploy revision list (one active) for the first run that exercises both workflows
- [ ] **Write-permission verification:** First Phase 2 run against `main` succeeds without 403 at `az deployment sub create` (no `--what-if` flag). If it 403s, the Phase 0.5 custom role is misconfigured or missing the `Microsoft.Resources/deployments/write` action — diagnose via `az role assignment list --assignee $AZURE_CLIENT_ID --scope /subscriptions/...` and inspect the role definition's `Actions` array before retry. Do not retry blindly.
- [ ] **Runbook `## Notes / Placeholder container image` subsection updated** to describe (a) the `readEnvironmentVariable('CONTAINER_IMAGE', ...)` fallback behavior introduced in this PR, (b) the `CONTAINER_IMAGE` env-var override option used by the workflows, and (c) that inline `--parameters containerImage=...` on the operator path still works and takes precedence over the env var
- [ ] **`runningState` field-availability decision recorded:** verification command output (`az containerapp revision show ... --query properties -o json`, redacted as needed) pasted into the Phase 2 PR body; one of the three branches (a/b/c) from §2.5 explicitly chosen with rationale

**Branch:** `infra-deploy-and-revision-cleanup` (one PR — workflow, script, `deploy.yml` extension, and runbook reconcile are interdependent and should land atomically)
**PR scope:** 2 new files + 1 extended workflow + 1 new script + runbook edits + `main.bicepparam` env-var switch

## 4. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `az deployment sub create` on `main` deploys a broken Bicep change without human review of the apply | Low (Phase 1 what-if visible on PR) | High (prod broken) | Phase 1 lands first and is required reading for every Bicep PR |
| What-if false positives create alert fatigue, reviewers stop reading | Medium | Medium | Informational-only by design; revisit if signal is consistently noisy by tuning what-if exclusions (Bicep `@metadata`) |
| `az containerapp revision deactivate` deactivates the **new** revision because revision-name detection is wrong | Low | High (app down) | Script asserts new-revision is `Healthy` before deactivating; refuses to deactivate the name passed as `--new-revision`; loud failure if list is empty |
| Federated credential subject mismatch for PR runs (Phase 1 needs `pull_request` federation) | Low — already exists per brief | — | Reuses existing `repo:glitchwerks/mom-bot:pull_request` federation; verify via `az ad app federated-credential list` in PR description |
| Concurrent infra + image deploys race on the same revision | Low | Medium | `concurrency` group `infra-deploy` will be added in Phase 2's new `deploy-infra.yml`; `concurrency` group `image-deploy` will be **added** to `deploy.yml` in Phase 2 (no existing block — verified at `deploy.yml:L1-L67`); separate groups so the two workflows don't block each other |
| `az deployment sub create` requires subscription-scope `Microsoft.Resources/deployments/write`, which `mom-bot-gha` does not have today | **High** — SP only has RG-scoped Container Apps Contributor per `infra/modules/containerapp.bicep:L238-L248`; manual deploys work because operator's AAD account holds Owner | High — Phase 2 403s without it (Phase 1 does NOT surface this — `--what-if` is read+validate, not write) | **Phase 0.5** grants a least-privilege custom role (`Microsoft.Resources/deployments/*` + `resourceGroups/read`) at sub scope. Operator-run, one-off. Verified directly via `az role assignment list`, **not** via Phase 1 what-if. |

## 5. Dependencies

- Existing federated credentials on `mom-bot-gha` for both `refs/heads/main` and `pull_request` (per brief; verify with `az ad app federated-credential list --id ${{ vars.AZURE_CLIENT_ID }}` if any phase fails auth)
- Existing repo variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (referenced by `deploy.yml:L38-L40`)
- `az` CLI on `ubuntu-latest` runners (default-installed)
- `marocchino/sticky-pull-request-comment` action for Phase 1 (or equivalent; pin by SHA)

## 6. Definition of Done

- #83 closed via PR that merges Phase 2 (PR body: `Closes #83`)
- #96 closed via same PR (PR body: `Closes #96`)
- Both acceptance-criteria lists from the issue bodies fully checked
- One real Bicep PR has been merged through the new pipeline and the post-merge `az containerapp revision list` shows exactly one active revision
- `infra/aad-runbook.md` reflects the as-built workflow surface (folded into Phase 2)

## 7. Open questions deferred to user / reviewer

None blocking. The five decisions in §2 are stated with rationale; the reviewer or user may overturn any of them. Specifically worth a second look:

- §2.1: auto-apply vs gate — if the user wants a one-click human gate before infra apply, swap Phase 2's `on: push` for `on: workflow_dispatch` and add a "infra deploy on demand" runbook note. No other changes required.
- §2.5: health-gate timeout — current proposal is 5 minutes. `mom-bot` startup is fast (<30s typical) so 5min is generous; tighten if desired.

## 8. Review gate

Per CLAUDE.md, this plan should be reviewed by `project-reviewer` before any phase ships. Phase 0 is doc-only and lowest-risk; ship sequence is 0 → 0.5 → 1 → 2 with the reviewer's blessing between Phase 1 and Phase 2 (the boundary where headless production writes begin). Former Phase 3 (runbook reconcile) is folded into Phase 2 per reviewer finding #7.

## Citations

- `.github/workflows/deploy.yml:L1-L67` — current image-only deploy workflow
- `infra/main.bicep:L1-L117` — Bicep entry point; sub-scoped
- `infra/main.bicepparam:L29` — `readEnvironmentVariable('GHA_SP_OBJECT_ID', '')`
- `infra/modules/containerapp.bicep:L150-L152` — ingress disabled; `activeRevisionsMode: 'Single'`
- `infra/aad-runbook.md:L333-L388` — Step 9, Notes, Summary checklist (insertion site for Phase 0/3)
- Issues: [#83](https://github.com/glitchwerks/mom-bot/issues/83), [#96](https://github.com/glitchwerks/mom-bot/issues/96)
- PRs: [#82](https://github.com/glitchwerks/mom-bot/pull/82), [#86](https://github.com/glitchwerks/mom-bot/pull/86), [#88](https://github.com/glitchwerks/mom-bot/pull/88), [#92](https://github.com/glitchwerks/mom-bot/pull/92), [#95](https://github.com/glitchwerks/mom-bot/pull/95) (per `git log` and brief)
- CLAUDE.md sections: "Background Polling", "GitHub Comments", "Cite Sources in Planning Artifacts"

## Review Response — 2026-05-17

Addressing the project-reviewer findings from the 2026-05-17 review:

1. **`$GITHUB_ENV` persistence (BLOCKING).** Phase 1 workflow shape now bakes in `echo "GHA_SP_OBJECT_ID=$SP_OID" >> "$GITHUB_ENV"` plus a verification step; acceptance criterion added. Phase 2 inherits the same pattern. (See Phase 1 § Workflow shape step 3; Phase 1 acceptance list; Phase 2 acceptance list.)
2. **Subscription-scope RBAC pre-flight (BLOCKING).** New **Phase 0.5** added (operator-run, one-off): inspect role assignments, grant Contributor at sub scope, update runbook. Phase 1 articulated as the RBAC canary in its acceptance list. Risk-table row corrected to flag this as High likelihood, not Low. (See Phase 0.5; Phase 1 acceptance "RBAC canary" bullet; §4 risk table row 6.)
3. **`runningState` not `healthState` (BLOCKING).** §2.5 rewritten to call out that `healthState` is only populated for ingress-enabled apps; gate now `runningState == 'Running'` with a verification step required before Phase 2 ships (paste live `properties` JSON into PR body; fall back to replica-level if needed). (See §2.5; Phase 2 script shape steps 1-2; Phase 2 acceptance list.)
4. **`main.bicepparam:L23` quickstart phantom diff (CONCERN).** Resolved as **option (a)**: switch to `readEnvironmentVariable('CONTAINER_IMAGE', '<quickstart>')` and have both workflows resolve the live image before Bicep. Implications check covered: no other deploy path depends on the static value. (See Phase 1 § `containerImage` parameter handling; `infra/main.bicepparam` added to Phase 2 touches.)
5. **Race window on revision-name resolution (CONCERN).** Replaced JMESPath `sort_by([?properties.active], &properties.createdTime)[-1]` with `properties.latestRevisionName` extracted from the `az containerapp update --output json` response (the authoritative source). (See Phase 2 § `deploy.yml` extension; Phase 2 acceptance list.)
6. **`deploy.yml` has no existing concurrency block (CONCERN).** Risk-table row corrected ("will be added in Phase 2"). Phase 2 touches now includes adding `concurrency: { group: image-deploy, cancel-in-progress: false }` to `deploy.yml`, with matching acceptance criterion. (See §4 risk row 5; Phase 2 § `deploy.yml` extension.)
7. **Phase 0 → Phase 2 runbook staleness (CONCERN).** Resolved as **option (a)**: Phase 3 folded into Phase 2 so runbook reconcile lands atomically with the automation. Phase 0's Step 10 reworded to a deliberately vague pointer ("automation tracked in #83") so it does not go stale on Phase 2 merge. (See Phase 0 content note; Phase 2 header + § runbook reconcile; §8 review gate.)
8. **`az ad sp show` SP-vs-app comment (NIT).** Inline comment added to the Phase 1 workflow YAML clarifying SP object ID vs app object ID. (See Phase 1 step 3 code block.)
9. **Hardcoded subscription GUID (NIT).** Replaced with `${{ vars.AZURE_SUBSCRIPTION_ID }}` in Phase 1 and Phase 2 workflow shapes, with matching acceptance criterion. (See Phase 1 step 4; Phase 2 § `deploy-infra.yml` shape; Phase 2 acceptance list.)

## Review Response — 2026-05-17 (Pass 2)

Addressing the project-reviewer's second-pass findings:

10. **Phase 0.5 over-grants Contributor (BLOCKING).** Phase 0.5 rewritten to make the least-privilege custom role (`Microsoft.Resources/deployments/*` + `resourceGroups/read`) the primary path with verbatim JSON body inline; Contributor demoted to a dated-exception fallback requiring a follow-up issue. Acceptance criterion now uses direct `az role assignment list` verification. (See Phase 0.5 § Procedure; Phase 0.5 § Acceptance.)
11. **Phase 1 is not an RBAC canary for `deployments/write` (BLOCKING).** All "canary" framing stripped from Phase 1 acceptance and §4 risk table. Phase 1 acceptance bullet renamed to "Auth reachability check" and explicitly notes `--what-if` is read+validate, not write. Write-permission verification moved to a new Phase 2 acceptance bullet (first apply against `main`) with `az role assignment list` diagnosis pathway. (See Phase 1 § acceptance "Auth reachability check"; §4 risk row 6; Phase 2 § acceptance "Write-permission verification".)
12. **Runbook `## Notes / Placeholder container image` rewrite (CONCERN).** `aad-runbook.md:L362-L374` explicitly added to Phase 2 touches; new bullet under Phase 2's runbook reconcile describes the rewrite covering (a) `readEnvironmentVariable` fallback, (b) `CONTAINER_IMAGE` env-var override, (c) inline `--parameters` precedence. Matching acceptance criterion added to Phase 2 list. (See Phase 2 touches; Phase 2 § runbook reconcile; Phase 2 § acceptance.)
13. **`cancel-in-progress: false` on `deploy.yml` rationale (CONCERN).** Three-sentence rationale added directly to the `deploy.yml` extension section explaining that `workflow_dispatch` deploys carry an explicit operator-chosen `commit_sha`, so queued deploys honor operator intent and cancelling would defeat it; contrasted with the infra-deploy serialization rationale. (See Phase 2 § `deploy.yml` extension.)
14. **`runningState` three-branch decision tree (CONCERN).** §2.5 rewritten to spell out branch (a) revision-level, (b) replica-level, (c) both empty → block + stopgap with 90s wait + warning log + follow-up issue. New Phase 2 acceptance bullet requires the chosen branch to be recorded with redacted command output pasted into the PR body. (See §2.5; Phase 2 § acceptance "runningState field-availability decision recorded".)
15. **Phase 0.5 branch/PR target (NIT).** Branch declared: `infra-83-sp-rbac-grant`, doc-only PR separate from Phase 1, with rationale (auditability of role-grant independent of workflow that exercises it). (See Phase 0.5 § Branch / PR.)
16. **`bicep build` won't catch missing `CONTAINER_IMAGE` (NIT).** Note added to Phase 2 `deploy-infra.yml` shape section confirming this is intentional: `readEnvironmentVariable` with a default is build-time silent by design; runtime presence is enforced by the workflow's `az containerapp show` step, with the fallback preserving cold-start behavior. Cross-references `aad-runbook.md:L151-L154`. (See Phase 2 § `deploy-infra.yml` shape, "Note on `bicep build` and `CONTAINER_IMAGE` absence".)
