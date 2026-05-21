---
title: mom-bot epic #128 Phase 1 — scoping & cross-repo coordination
touches:
  - src/mom_bot/sidecar/app.py
  - src/mom_bot/sidecar/
  - tests/integration/sidecar/
  - infra/
  - docs/operations/epic-128-cutover.md
  - docs/secrets-inventory.md
  - siege-web/bot/INTERFACE.md
  - siege-web/backend/app/services/bot_client.py
  - siege-web/infra/
skills_relevant:
  - python
  - azure
  - bicep
  - github-actions
---

# mom-bot Epic #128 — Phase 1 Scoping Plan

**Parent epic:** `glitchwerks/mom-bot#128` — "epic: mom-bot replaces siege-web's bundled bot+sidecar (full sidecar mode)"
**Milestone:** mom-bot v1.0 (#1)
**Sibling repo:** `glitchwerks/rsl-siege-manager` (siege-web)
**Authoritative contract:** `siege-web/bot/INTERFACE.md` + `siege-web/backend/tests/integration/sidecar/` (tests win on disagreement)
**Master framework plan:** `docs/superpowers/plans/2026-05-08-mom-bot-framework.md`
**Created:** 2026-05-20
**Status:** APPROVED (Rev 4, 2026-05-21) — all decisions signed off; B-1 pre-flight complete; PR #157 review feedback addressed; ready for sub-issue filing.

## Revision log

- **2026-05-21 (Rev 4, post-PR-157-second-review).** Second `claude-action-runner` review on PR #157 raised 7 items (2 BLOCK on meta-consistency, 5 medium on scope clarification). Review at https://github.com/glitchwerks/mom-bot/pull/157#issuecomment-4509173704. All addressed in this revision:
  - **Status line + this entry's date** updated to Rev 4 (was stale at Rev 3, blocking item).
  - **Provenance link** added to the Rev 3 entry pointing at the round-1 review comment (audit-trail item).
  - **B-8 observability tightened**: App Insights named explicitly, alert ownership assigned to the epic #128 driver, thresholds explicitly deferred to implementer-time against B-10 baseline (justified inline).
  - **B-8 outbound-IP detection simplified**: weekly cron dropped (over-engineered per review pushback); on-403-spike detection retained as the timely signal.
  - **§ 7 fallback communication owner named**: epic #128 driver posts the cross-repo-delay comment.
  - **B-11 final-503 disposition softened** from "any blocks" to graduated investigate/rollback thresholds (≥1 investigate, ≥3 rollback). Pushback rationale captured inline.
  - **Framework plan correction** now references issue **#158** rather than living only as a plan checklist.

- **2026-05-21 (Rev 3, post-PR-157-review).** `claude-action-runner` review on PR #157 raised 9 items (2 BLOCK, 4 medium, 3 nit) (review at https://github.com/glitchwerks/mom-bot/pull/157#issuecomment-4509173704). All addressed in this revision:
  - **B-8 monitoring scope added** to acceptance criteria (4xx/5xx rate alerts, p95 latency alert, cert-expiry monitoring, outbound-IP-change detection, App Insights wiring). Folded into B-8 rather than filed as a separate sub-issue.
  - **Cross-repo delay fallback strategy** consolidated into a new § 7 subsection with explicit triggers (C-1/C-4 ownership unresolved, C-4 not in prod, siege-web v1.2 slip) and per-trigger actions.
  - **B-1 → B-7 fake-mode dispatch dependency** promoted to a § 5 callout so the implicit implementation-shape coupling is visible on a skim of the dependency graph.
  - **B-8 IP allowlist runbook** added — if 4xx spike fires, re-query siege-web env static IP and update the Bicep allowlist.
  - **Framework plan region-lock correction** added to § 7 as a tracked follow-up item; not a blocker for this epic's sub-issues.
  - **B-11 cutover downtime budget** added to acceptance criteria (10-30s expected; verify via App Insights against C-4 retry budget; final-503 blocks declaring cutover complete).
  - **Dependency graph fixes**: added missing B-7 → B-10 edge; removed resolved C-2 reference.
  - **Terminology normalized** to "bundled bot" (open compound) throughout the prose.

- **2026-05-21 (Rev 2, post-sign-off).** User signed off on all seven Decisions D-1..D-7 in a focused session. § 6 cross-blocking pre-flight executed (none of #103, #120, #121, #124 block B-1). Three significant findings reshaped the plan:
  - **Env topology verified.** mom-bot in East US 2, siege-web's bot env in West US — cross-region, contradicting the framework plan's "both in eastus2 (locked 2026-05-08)" claim. Framework plan needs amendment. See § 2 for verified region/IP details.
  - **D-2 rewritten.** Cross-region topology makes internal DNS infeasible. Chosen path: Mode 2 — public HTTPS ingress on mom-bot's sidecar Container App with IP allowlist for `20.245.166.6/32` (siege-web env static outbound IP), HSTS, min TLS 1.2, auto-managed env-default-domain cert. Env consolidation deferred to post-v1.0.
  - **C-1 substantially de-scoped.** siege-web's `useExternalSidecar` + `externalBotApiUrl` Bicep parameters already exist (`infra/modules/container-apps.bicep:209`); C-1 collapses to verify-and-document.
  - **C-2 closed in-session** (no PR needed — siege-web's bundled bot is a separate Container App in its env, confirmed via Bicep paste).
  - **D-6 wording refined** to "example of working conforming impl, not plan-of-record."
  - **D-7 rationale rewritten** to be about this cutover's specific risks (gateway + sidecar HTTP, post-reminder-cycle, off-primetime, weekday operator) rather than mechanically inheriting Epic 4.
  - **B-8 sub-issue spec updated** to match the new D-2 (public HTTPS + allowlist + HSTS + TLS 1.2).
  - Sign-off and findings captured at https://github.com/glitchwerks/mom-bot/issues/128#issuecomment-4509085682.

- **2026-05-20 (Rev 1, post-review).** `project-reviewer` pass returned 2 BLOCKING / 4 CONCERN / 3 NIT findings (full report at `.tmp/2026-05-20-project-reviewer-epic-128-phase-1.md`). User accepted all BLOCKING findings and authorized fixes. Changes in this revision:
  - **D-3 rewritten.** Removed the false `INTERFACE.md:180-194` retry citation (that range is the error-semantics table, not a retry spec). Verified `siege-web/backend/app/services/bot_client.py:43-99` — the four sidecar methods (`notify`, `post_message`, `post_image`, `get_members`) have no retry loop; they swallow `httpx.HTTPError` and return `False`/`None`/`[]`. The only retry path is `sync_day_role` (one 5xx retry, separate webhook). Mitigation: added new cross-repo sub-issue **C-4** (add 503-retry-with-backoff to BotClient sidecar methods, hard prerequisite of B-11) AND tightened B-11's runbook to require a verified-quiet window before stopping the bundled bot. Defense in depth.
  - **B-1 / B-2 auth contract.** Verified `siege-web/backend/tests/integration/sidecar/test_auth.py:29-134` asserts `response.status_code == 403` (not `in {401, 403}`) on missing `Authorization` header across 4 endpoints. mom-bot's current `_require_bearer` (`src/mom_bot/sidecar/app.py:237-254`) returns 401 on absent header — this WILL fail the ported conformance suite. B-1 now mandates switching the auth scaffold to `fastapi.security.HTTPBearer(auto_error=True)` so missing-header returns 403, matching the bundled bot.
  - **D-2 reclassified.** Header changed from "Decision" to "Conditional decision — confirmed by C-2." Added explicit fallback paragraph for the co-located case.
  - **D-5 / C-1 critical path.** Cross-repo checklist § 7 now names C-1 as the critical-path gate for B-11 with an explicit timeline-confirmation step.
  - **D-1 escape-hatch caveat.** Replaced "Option (c) is the cheapest path" with the Discord-application-scope conditional.
  - **D-6 / D-3 citations corrected** (NIT — `INTERFACE.md:122-134` was over-cited).
  - **B-5 / B-6 deps** updated to include B-2 (auth scaffolding) explicitly (NIT).
  - **§ 4 preface added** explaining the plan re-sequences epic #128's phasing by logical dependency rather than the epic body's order (NIT).
  - **B-7 technical notes** updated to require `build_app`'s fake-mode dispatch signature lands in B-1 (CONCERN).
  - **§ 6 hardened** — recommend user scan #103/#120/#121/#124 titles before filing B-1 (CONCERN).
  - **Frontmatter `touches:`** rewritten — was self-referential; now lists the implementation paths sub-issues actually touch.
  - **CONCERN deferred:** none. All four were cheap to address inline.

> **Auto Mode disclaimer.** This document was drafted under router Auto Mode. Every architectural decision below is the assistant's "reasonable call" per the brief, **not** a confirmation from the user or from the siege-web owner. Sections marked **CROSS-REPO CONFIRM** require explicit ack from the siege-web maintainer before the corresponding sub-issues land. The user should redirect any decision that diverges from intent before filing.

---

## 1. Purpose

Resolve the architectural and cross-repo open questions named in #128 so the remaining 8 phases of the epic can be decomposed into independently mergeable sub-issues. This document does **not** restate the framework — it answers what #128 explicitly defers and turns the answers into sub-issue specs.

## 2. Verified context (do not re-derive)

- **mom-bot's inbound sidecar surface today is a single endpoint** — `POST /api/internal/role-sync` for Epic 2.6 (`src/mom_bot/sidecar/app.py:260-264`). The seven public-contract endpoints in INTERFACE.md (`/api/version`, `/api/health`, `/api/notify`, `/api/post-message`, `/api/post-image`, `/api/members`, `/api/members/{discord_user_id}`) are **not yet implemented** in mom-bot.
- **siege-web's bundled bot is the reference implementation** of the seven endpoints (`siege-web/bot/app/http_api.py`, `siege-web/bot/app/discord_client.py`, `siege-web/bot/app/main.py`). The fake-mode integration harness at `siege-web/backend/tests/integration/sidecar/conftest.py:91-140` launches it as a subprocess on port 8001 and asserts the full conformance table from `INTERFACE.md`.
- **Sidecar contract is single-guild by design.** `DISCORD_GUILD_ID` is an environment variable scoped to the process (`INTERFACE.md` § Discord coupling, lines 122-134). The contract returns the guild member list with no `guild_id` discriminator anywhere in the request or response shapes.
- **mom-bot today serves a single guild** in prod — verified by inspecting `src/mom_bot/sidecar/app.py`, which receives `guild: discord.Guild` (singular) at app-build time. The framework plan's reference to "two guilds" (epic body's multi-guild open question) is forward-looking, not current state. The decision below treats single-guild as the floor and documents the path forward only if a second guild is added.
- **Container Apps environment topology (verified 2026-05-21 via `az containerapp env show`).** mom-bot runs in `cae-mom-bot-eastus2` (East US 2, static outbound `57.162.41.33`, `vnetConfig: null`). siege-web's bot Container Apps env is `siege-web-cae-prod-yf3fl2t3yxmtk` (West US, default domain `salmondesert-a9a40ca1.westus.azurecontainerapps.io`, static outbound `20.245.166.6`, `vnetConfig: null`). The two envs are in different regions and neither is VNet-integrated. Note: the framework plan's claim that "both apps live in `eastus2` (locked 2026-05-08)" is incorrect — siege-web's bot env is in West US. The framework plan needs a corresponding amendment.
- **`useExternalSidecar` is referenced in `INTERFACE.md` § Discord coupling** as the operator-side mechanism for excluding the bundled bot. The contract document treats it as a Bicep parameter that exists; siege-web's actual Bicep state must be verified before the cutover sub-issue assumes it.
- **mom-bot KV `kv-mombot-eastus2` already holds `siege-web-url`, `siege-web-bot-token`, `mom-bot-api-key`, `prod-discord-token`** — listed in the epic body. New secret for this epic: `prod-discord-bot-api-key` (the Bearer token siege-web sends to mom-bot's inbound sidecar; semantically equivalent to siege-web's existing `BOT_API_KEY`).

---

## 3. Decisions log

Each decision lists the chosen option, the rationale, and any cross-repo follow-ups it generates.

### D-1. Multi-guild — **scope to single guild (Option a)**

**Decision.** Mom-bot's sidecar surface is single-guild. `DISCORD_GUILD_ID` is set once via environment / KV; the seven endpoints resolve names against that one guild's caches. If mom-bot grows to serve a second guild post-v1.0, that work is a v2.x epic — likely Option (c) (two mom-bot instances) rather than a contract change.

**Rationale.**
1. The contract is structurally single-guild — no `guild_id` field on any request or response (`INTERFACE.md` lines 122-134). Option (b) (per-request `guild_id`) is a breaking contract change that requires the table-row classification "Adding a required request field — Breaking" (`INTERFACE.md` lines 838-848). That cannot land inside this epic without dragging siege-web along.
2. Option (c) (two instances) defers the multi-guild question entirely. **Caveat:** Option (c) is viable as-is _only if_ both guilds are invited to the same Discord application (one shared bot token, one app). If the second guild requires a separate Discord application — new token, new invite flow, different permission scope — the scope is no longer "copy the Bicep, set a different `DISCORD_GUILD_ID`"; it becomes a new Epic that touches Discord developer-portal state, KV secret topology, and potentially the cutover machinery. The framework plan's "runtime is coupled to siege-web by design" framing supports treating two-instance as conditional, not free.
3. Current prod state is single-guild; framing the v1.0 cutover around a hypothetical second guild adds scope mom-bot does not need today.

**Cross-repo follow-up.** None. This decision is internal to mom-bot.

**Source.** `siege-web/bot/INTERFACE.md:122-134` (Discord coupling); `src/mom_bot/sidecar/app.py:167-191` (current single-guild build_app signature).

### D-2. Container Apps ingress — **Mode 2: public HTTPS, cross-region**

**Decision.** mom-bot's sidecar Container App uses public HTTPS ingress on port 8001 (`external: true`). siege-web's `externalBotApiUrl` is set to `https://<mom-bot-app-fqdn>.eastus2.azurecontainerapps.io` (the Container Apps environment's auto-managed default-domain cert; no custom domain required). IP allowlist scoped to `20.245.166.6/32` (siege-web env's static outbound IP).

**Why Mode 2 — invalidation of the prior internal-DNS framing.**

Verified via `az containerapp env show` (2026-05-21):

- mom-bot's env `cae-mom-bot-eastus2` is in **East US 2**.
- siege-web's bot env `siege-web-cae-prod-yf3fl2t3yxmtk` is in **West US**.

Internal DNS within a Container Apps environment only resolves within a single env. The two envs are in different regions — internal DNS is structurally infeasible for cross-env name resolution.

**Modes considered and eliminated.**

- **Mode 1 (internal DNS, shared env):** Requires both apps in the same env. They are not. Infeasible without consolidation.
- **Mode 3 (move one env into the other):** Significant infra restructure — recreating an env is destructive. Deferred to a post-v1.0 consolidation review.
- **Mode 4 (VNet peering):** Requires both envs to have `infrastructureSubnetId` configured. Both have `vnetConfig: null`. Adopting it would require recreating both envs. Out of scope for v1.0.
- **Mode 2 (public HTTPS):** Viable immediately. Mitigated by IP allowlist + HSTS + min TLS 1.2 + Bearer-token auth (already designed, `prod-discord-bot-api-key`). **Chosen.**

**Mitigations for Mode 2.**

1. IP allowlist (Container Apps ingress restrictions) scoped to `20.245.166.6/32` (siege-web env static outbound IP; verified 2026-05-21).
2. HSTS enabled on ingress.
3. Minimum TLS 1.2.
4. Bearer-token auth via `prod-discord-bot-api-key` (B-9 provisions).

**Cross-region RTT.** West US ↔ East US 2 is ~50-70ms per call. Acceptable for the operator-driven sidecar call volume (notify / post-message / post-image / members). Flag if siege-web tightens its `httpx` timeouts.

**Env consolidation.** Deferred to a post-v1.0 review item. No action required for this epic.

**Source.** `az containerapp env show` for `cae-mom-bot-eastus2` and `siege-web-cae-prod-yf3fl2t3yxmtk` (verified 2026-05-21); `infra/modules/container-apps.bicep:209` in siege-web (verified 2026-05-21 via in-session paste); epic body's "Container Apps ingress design" open question.

**Cross-repo follow-up.** C-1 collapses to verify-and-document (see D-5). C-2 closed in-session — siege-web's bundled bot confirmed as a separate Container App; no PR needed.

### D-3. Discord token migration — **swap-in-place during the cutover window, no rotation** **(CROSS-REPO CONFIRM)**

**Decision.** mom-bot inherits the **existing** Discord token from siege-web's bundled bot. The cutover stops the bundled bot first (gateway disconnects, token is released), then mom-bot starts (acquires the gateway on the same token). No token rotation in flight. The Pre-Epic-0 Discord application audit already confirmed inheritance is viable (framework plan § Pre-Epic-0, resolved 2026-05-08).

**Rationale.**
1. Token rotation requires updating Discord developer portal + every consumer in lockstep — strictly more moving parts than process replacement.
2. The framework plan's "inherit existing token" pillar is already accepted for Epic 4's reminder-bot cutover; the same machinery applies here (the gateway is single-holder at any instant).
3. The "no overlap window" rule named in the epic body matches Epic 4's cutover-time-of-day rule (Thursday/Friday post-13:00 UTC) — both reminders for the week have already fired, minimizing operational risk.

**Operational rule (revised, post-review).** The stop bundled bot → start mom-bot sequence is **the same operator action** in the runbook (D-7 below). Discord's gateway will accept the new connection within seconds of the old session closing, but the sidecar HTTP surface is unavailable for the duration of the swap (10-30s typical).

The **previous version of this plan claimed** siege-web's `BotClient` "retries 503s with backoff per `INTERFACE.md:180-194`." That claim is incorrect on two counts:

1. `INTERFACE.md:180-194` is the error-semantics _table_ — it documents what status codes mean, not retry logic.
2. The actual `BotClient` implementation at `siege-web/backend/app/services/bot_client.py:43-99` makes a single `httpx.AsyncClient` request per sidecar method, calls `raise_for_status()`, and **swallows `httpx.HTTPError`, returning `False`/`None`/`[]`**. There is no retry loop. The only retry path in `BotClient` is `sync_day_role` (one 5xx retry, separate outbound webhook to mom-bot — not a sidecar call).

A 10-30s gateway gap means in-flight `notify` / `post_message` / `post_image` / `get_members` calls drop silently with no surfacing and no automatic reconciliation.

**Mitigation (defense in depth — both required).**

1. **C-4 — add 503-retry-with-backoff to BotClient sidecar methods.** New cross-repo sub-issue (filed against siege-web). C-4 is a **hard prerequisite of B-11** — the cutover MUST NOT execute until C-4 has merged on siege-web's `main` AND been deployed to prod. Once C-4 lands, transient 503s during the cutover window self-heal; the cutover becomes safe-by-default rather than depending on operator vigilance.
2. **B-11 runbook hardening.** Even with C-4 deployed, the runbook requires a **verified-quiet window** before stopping the bundled bot: operator queries App Insights for the previous 60 seconds and confirms zero in-flight sidecar calls (or a count low enough that retry-budgets cover them). The "stop bundled bot" step is gated on that observation, not on a wall-clock timer.

**Cross-repo follow-up.** **C-4** (BotClient retries — hard gate on B-11). Token migration itself remains zero cross-repo work — the token is shared infra already in mom-bot's KV.

**Source.** `siege-web/backend/app/services/bot_client.py:43-99` (single-attempt, error-swallowing sidecar methods — verified 2026-05-20); framework plan § Pre-Epic-0 (token-inheritance resolved 2026-05-08).

### D-4. `bot_connected` semantics — **verify, do not assume**

**Decision.** mom-bot's `/api/health` MUST return `bot_connected` reflecting `is_ready()` on the discord.py `Client` at handler-call time (no caching, no last-known value). Phase 2 (sidecar lift-and-shift) includes a verification step that probes the live behavior — not just a code-read — before Phase 9 (cutover) signs off.

**Rationale.**
1. The contract is explicit: "`bot_connected` reflects the result of `is_ready()` on the discord.py client at the moment the health handler runs" (`INTERFACE.md:237-249`).
2. mom-bot's current sidecar (`src/mom_bot/sidecar/app.py`) does not yet implement `/api/health` — there is nothing to verify until Phase 2 lands the lift-and-shift port. The verification is a sub-issue exit criterion, not a Phase 1 gate.
3. The contract's "advisory-only SLA" (`INTERFACE.md:240-249`) means the goal is correctness at handler-call time, not a TOCTOU guarantee. discord.py exposes `is_ready()` directly; no caching layer is needed or wanted.

**Cross-repo follow-up.** None. siege-web's tests at `siege-web/backend/tests/integration/sidecar/test_health.py` serve as the conformance check — they run against mom-bot's port-8001 surface in Phase 8 (integration-test portability, sub-issue B-7).

**Source.** `siege-web/bot/INTERFACE.md:237-269`; `siege-web/backend/tests/integration/sidecar/test_health.py`.

### D-5. Cross-repo go/no-go from siege-web owner — **assume aligned, confirm before cutover (Phase 9)** **(CROSS-REPO CONFIRM)**

**Decision.** Phases 2-8 proceed in mom-bot without blocking on siege-web's v1.2 timeline, because every code change in those phases lands on mom-bot's `useExternalSidecar=false` default path (no siege-web behavior change until the operator flips). Phase 9 (cutover) is the explicit go/no-go gate — it requires confirmed sign-off from the siege-web maintainer that (a) the `useExternalSidecar=true` Bicep parameter exists in siege-web's infra and (b) siege-web v1.2 has shipped (or is ready to ship) by mom-bot's cutover date.

**Rationale.**
1. Coupling the work timelines is unnecessary — mom-bot's sidecar can be deployed and exercised against siege-web's test instance long before cutover, identical to how Epic 2 verification was sequenced in the framework plan ("siege-web's `DISCORD_BOT_API_URL` pointed at mom-bot's **dev** URL — exercise all 6 endpoints" — framework plan § Verification per epic).
2. **C-1 updated scope (verified 2026-05-21).** siege-web's `useExternalSidecar` and `externalBotApiUrl` Bicep parameters already exist (`infra/modules/container-apps.bicep:209`). C-1 collapses from "add the parameter" to **verify-and-document**: confirm `externalBotApiUrl` accepts mom-bot's `https://<env-default-domain>` URL, optionally relax the "HTTPS required outside dev" guard if a future internal-DNS path is desired (document the same-env vs cross-env case in INTERFACE.md). No mandatory siege-web Bicep change required for v1.0 cutover.

**Cross-repo follow-up.** Sub-issue C-1 (verify-and-document — see updated spec in § 4); sub-issue C-2 closed in-session. For the formal fallback strategy if siege-web cannot deliver C-1 or C-4 on schedule, see § 7 "Cross-repo delay fallback strategy."

**Source.** `INTERFACE.md:122-134`; framework plan § Verification per epic.

### D-6. INTERFACE.md addendum — **siege-web side, names mom-bot as conforming alternate**

**Decision.** A short PR against siege-web's `bot/INTERFACE.md` adds a section (or footnote in `## Replaceability requirements`) naming mom-bot as an *example of a working conforming implementation*, **not** as the plan-of-record sidecar. This is sub-issue C-3 below.

**Suggested wording for C-3.** *"Implementations conforming to this spec include mom-bot (`glitchwerks/mom-bot`), which hosts the sidecar surface alongside its own Discord-bot responsibilities."* This phrasing leaves siege-web free to add or swap conforming implementations later — mom-bot is a named example, not the designated permanent replacement.

**Rationale.** Already foreshadowed by `INTERFACE.md` lines 1-15 ("an alternative sidecar implementation (e.g. mom-bot, or an integration-test stub) can be built against it"). Closing the loop documents reality once mom-bot has demonstrably passed the conformance suite. Framing mom-bot as an example rather than plan-of-record preserves flexibility for future conforming implementations.

**Cross-repo follow-up.** Land C-3 after Phase 8 verification passes, not before.

**Source.** `siege-web/bot/INTERFACE.md:1-15` (introductory replaceability paragraph naming "alternative sidecar implementation"). D-6 does not depend on `INTERFACE.md:122-134` (the Discord coupling section).

### D-7. Cutover-time-of-day — **inherit framework rule (Thursday/Friday post-13:00 UTC)**

**Decision.** Phase 9's cutover happens on a Thursday or Friday after 13:00 UTC. Specific date is selected at cutover-prep time, not now.

**Rationale.** This window is chosen for THIS cutover's specific risk shape:

1. **mom-bot owns the weekly reminders.** mom-bot drives the Hydra (Tuesday) and Chimera (Wednesday) weekly reminders. A botched cutover risks both the Discord gateway connection (which serves those reminders) AND the sidecar HTTP surface (which serves siege-web's admin flows). By Thursday/Friday, both reminders have already fired for the week — if something goes wrong, recovery time exists before the next cycle.
2. **Off-primetime gaming hours.** Post-13:00 UTC on Thursday/Friday is off-peak for both NA and EU players. Disruption to siege-web's bot-dependent admin flows affects fewer active users.
3. **Weekday operator availability.** Thursday/Friday keeps the cutover on a standard work day, maximizing operator availability for real-time monitoring and rollback if needed. Weekend cutovers with on-call-only coverage are riskier.

Epic 4's rule (`framework plan § Epic 4`) uses the same window for analogous reasons — it is **project precedent**, not the source of authority here. The reasoning above applies independently to this cutover.

**Source.** Framework plan § Epic 4 (precedent); D-3's verified-quiet-window runbook design; mom-bot weekly reminder schedule (Tuesday/Wednesday).

---

## 4. Sub-issue specs — Phases 2-9 of #128

Sub-issues use the prefix `epic-128/` in titles to group them. Each spec includes the proposed title, labels, body draft, dependency edges, and any cross-repo prerequisites. **Do not file from this document directly** — the user reviews these specs first.

> **Note on phase numbering.** Epic #128's body labels phases by domain area (Phase 2 = `/api/version + /api/health`, Phase 3 = members endpoints, etc.). This plan re-sequences the sub-issues by **logical dependency** rather than by the epic body's domain ordering — so B-1 lands version+health, B-2 layers Bearer auth via notify, and B-5/B-6 (members endpoints) depend on B-2's auth scaffolding. The sub-issue identifiers in this plan (B-1, B-2, …) are local to this document and do not correspond to epic #128's "Phase N" labels.

### Phase 2 — Sidecar endpoint port (mom-bot)

#### B-1. Port `GET /api/version` + `GET /api/health` to mom-bot sidecar

- **Title:** `feat(epic-128): add /api/version + /api/health to mom-bot sidecar`
- **Labels:** `epic-128`, `sidecar`, `feat`
- **Body:**

  **Summary.** Add the two unauthenticated probe endpoints (`GET /api/version`, `GET /api/health`) to mom-bot's existing FastAPI app at `src/mom_bot/sidecar/app.py`. Match the response shapes from `siege-web/bot/INTERFACE.md:200-269`.

  **Context.** mom-bot's sidecar today exposes only `POST /api/internal/role-sync` (`src/mom_bot/sidecar/app.py:260-264`). The seven INTERFACE.md endpoints are net-new. Health + version land first because they are unauthenticated, have the simplest semantics, and unblock siege-web's `BotClient` from connecting at all.

  **Acceptance criteria.**
  - `GET /api/version` returns `{"version": "<semver+build>"}` matching the format at `INTERFACE.md:200-225` (semver from `mom_bot/VERSION`, fall back to `"unknown"` if missing; build suffix from `BUILD_NUMBER` + `GIT_SHA` env vars when present).
  - `GET /api/health` returns `{"status": "healthy", "bot_connected": <bool>}` where `bot_connected` reads `is_ready()` on the connected discord.py client at handler-call time, per D-4 and `INTERFACE.md:237-249`. No caching.
  - Both endpoints are unauthenticated (no Bearer required).
  - Unit tests cover: version-string format with and without build env vars; `bot_connected: true` when client is ready; `bot_connected: false` when client is not ready.
  - The shape-conformance tests at `siege-web/backend/tests/integration/sidecar/test_version.py` and `test_health.py` pass against mom-bot's port-8001 surface (executed manually for this sub-issue; automated runner lands in B-7).

  **Technical notes.**
  - mom-bot's sidecar app factory (`build_app` at `src/mom_bot/sidecar/app.py:167-191`) takes a `guild: discord.Guild` — extend its signature (or its container) to accept the full `discord.Client` so `is_ready()` is reachable. Do not introduce a separate health-cache abstraction.
  - **Land the fake-mode dispatch signature in B-1, not B-7.** B-7 ports the conformance harness and expects a `FakeDiscordClient` to substitute for the live `discord.Client`. Extend `build_app` in this sub-issue to accept either a real `discord.Client` or a duck-typed test client (recommend a `client` parameter plus `MOM_BOT_TEST_MODE` env-gated branch in the caller — see B-7 technical notes). Doing this in B-1 avoids a B-1 retrofit when B-7 lands.
  - **Auth scaffolding (B-1 lands the swap, B-2 uses it).** Verified `siege-web/backend/tests/integration/sidecar/test_auth.py:29-134` asserts `response.status_code == 403` on missing `Authorization` header across 4 endpoints (`test_notify_missing_auth_header_returns_403`, `test_post_message_missing_auth_header_returns_403`, `test_get_members_missing_auth_header_returns_403`, `test_get_member_by_id_missing_auth_returns_403`). mom-bot's current `_require_bearer` (`src/mom_bot/sidecar/app.py:237-254`) returns **401** on absent header — this WILL fail the ported conformance suite. **B-1 must replace `_require_bearer` with `fastapi.security.HTTPBearer(auto_error=True)` + a wrapper that raises 401 on wrong-token-present, matching the bundled bot's behavior at `siege-web/bot/app/http_api.py` `verify_api_key`.** Existing `/api/internal/role-sync` keeps the old `_require_bearer` (or moves to the new dependency — implementer's call, but verify Epic 2.6's integration tests still pass).

  **Out of scope.** Authenticated endpoints' route handlers (B-2 onwards). Existing `/api/internal/role-sync` behavior is unchanged in its response contract.

- **Depends on:** none.
- **Cross-repo prereqs:** none.

#### B-2. Port `POST /api/notify` (DM send by username)

- **Title:** `feat(epic-128): add /api/notify DM-send endpoint`
- **Labels:** `epic-128`, `sidecar`, `feat`
- **Body:**

  **Summary.** Implement `POST /api/notify` per `INTERFACE.md:272-326`. Bearer-auth-gated; sends a DM to a guild member by username.

  **Acceptance criteria.**
  - Endpoint matches the full conformance table at `INTERFACE.md:759-770` (all 8 rows: success, 404 non-member, 422 missing fields, 503 bot-not-connected, 403 DM-blocked, 502 Discord 4xx, 503 Discord 5xx).
  - Auth: 401 on wrong token present, **403 on absent `Authorization` header** (matching the bundled bot via `HTTPBearer(auto_error=True)`, landed in B-1). Although `INTERFACE.md:115-117` permits 401 OR 403 on missing header, the ported integration suite at `siege-web/backend/tests/integration/sidecar/test_auth.py:29-134` asserts `== 403` specifically across 4 missing-header test cases — so mom-bot must return 403 to pass B-7's conformance harness. See B-1 technical notes for the `_require_bearer` → `HTTPBearer(auto_error=True)` swap rationale.
  - Discord exception translation: `discord.Forbidden` → 403 with body `{"detail": "Discord permission denied"}`; `discord.HTTPException` 4xx → 502; 5xx or `asyncio.TimeoutError` → 503. Per `INTERFACE.md:311-326`.
  - Username resolution: case-insensitive match against guild member cache; not found → 404 (one cause only, per `INTERFACE.md:306-310`).
  - Integration test that boots the sidecar in a fake-discord mode (analog of `siege-web/bot/app/fake_discord.py`) and exercises all conformance rows. **Decision needed:** port siege-web's `fake_discord.py` verbatim or build mom-bot's own? See sub-issue B-7's harness decision.

  **Technical notes.** mom-bot's `_validation_error_handler` currently converts 422 → 400 (`src/mom_bot/sidecar/app.py:199-231`) because that's what the `/api/internal/role-sync` contract specifies. The new INTERFACE.md endpoints require 422, not 400 — install a per-route override or scope the 400-conversion to the role-sync path only. See B-2 implementation note in the implementation plan.

  **Out of scope.** Channel-message endpoints (B-3, B-4). Member endpoints (B-5).

- **Depends on:** B-1 (shared health/auth scaffolding).
- **Cross-repo prereqs:** none.

#### B-3. Port `POST /api/post-message` (channel message)

- **Title:** `feat(epic-128): add /api/post-message endpoint`
- **Labels:** `epic-128`, `sidecar`, `feat`
- **Body:**

  **Summary.** Implement `POST /api/post-message` per `INTERFACE.md:330-388`. Bearer-auth-gated; posts a text message to a guild channel by name.

  **Acceptance criteria.**
  - Endpoint matches the conformance table at `INTERFACE.md:772-783`.
  - Channel resolution: exact name match against guild text channels; not found → 404 (per `INTERFACE.md:363-372`).
  - Discord exception translation identical to B-2.

  **Out of scope.** Image variant (B-4).

- **Depends on:** B-2 (auth + exception-handler infrastructure).
- **Cross-repo prereqs:** none.

#### B-4. Port `POST /api/post-image` (multipart upload + channel post)

- **Title:** `feat(epic-128): add /api/post-image multipart endpoint`
- **Labels:** `epic-128`, `sidecar`, `feat`
- **Body:**

  **Summary.** Implement `POST /api/post-image` per `INTERFACE.md:391-460`. Accepts `multipart/form-data` (`channel_name` form field + `file` UploadFile), posts to channel, returns Discord CDN URL.

  **Acceptance criteria.**
  - Endpoint matches the conformance table at `INTERFACE.md:785-795`.
  - `channel_name` is a form field, NOT a query parameter — sending it as a query parameter MUST return 422 (`INTERFACE.md:791`).
  - Response shape: `{"status": "sent", "url": "<discord cdn url>"}`; `url` is non-empty on 200.
  - Discord exception translation identical to B-2 / B-3.

- **Depends on:** B-3.
- **Cross-repo prereqs:** none.

#### B-5. Port `GET /api/members` (guild roster)

- **Title:** `feat(epic-128): add /api/members guild-roster endpoint`
- **Labels:** `epic-128`, `sidecar`, `feat`
- **Body:**

  **Summary.** Implement `GET /api/members` per `INTERFACE.md:464-523`. Returns the full cached guild member list as a JSON array.

  **Acceptance criteria.**
  - Each element has exactly three keys: `id` (snowflake string), `username`, `display_name`. **Key for Discord ID is `id`, NOT `discord_id`** — per the load-bearing distinction at `INTERFACE.md:497-506`.
  - No pagination — single-shot delivery of full guild member list (`INTERFACE.md:508-513`).
  - Conformance table rows at `INTERFACE.md:797-803` pass.

  **Out of scope.** Single-member lookup (B-6).

- **Depends on:** B-1 (factory + auth scaffold), B-2 (Bearer-auth dependency reusable across endpoints).
- **Cross-repo prereqs:** none.

#### B-6. Port `GET /api/members/{discord_user_id}` (single-member lookup with `is_member` discriminator)

- **Title:** `feat(epic-128): add /api/members/{discord_user_id} single-member endpoint`
- **Labels:** `epic-128`, `sidecar`, `feat`
- **Body:**

  **Summary.** Implement `GET /api/members/{discord_user_id}` per `INTERFACE.md:527-592`. Returns the discriminated full-envelope shape with `is_member: bool` + 5 fields that are non-null when `is_member=true`, all null when false.

  **Acceptance criteria.**
  - Path parameter validated against `^\d+$` by FastAPI (`Path(..., pattern=r"^\d+$")`) — non-numeric returns 422 BEFORE the handler runs (`INTERFACE.md:537-540`).
  - Response on member found: all six keys present (`is_member`, `discord_id`, `username`, `display_name`, `roles`, `role_names`) with non-null values; `@everyone` excluded from `roles` and `role_names` (`INTERFACE.md:568-569`).
  - Response on non-member: all six keys present; `is_member: false`, other five fields `null` (`INTERFACE.md:555-565`).
  - **Key for Discord ID is `discord_id`, NOT `id`** — per the load-bearing distinction at `INTERFACE.md:497-506`. Renaming either field across endpoints is a breaking change.
  - Conformance table rows at `INTERFACE.md:805-811` pass.

- **Depends on:** B-5, B-2 (auth scaffold).
- **Cross-repo prereqs:** none.

---

### Phase 3 — Conformance harness (mom-bot, depends on Phase 2)

#### B-7. Port siege-web integration suite as mom-bot conformance harness

- **Title:** `test(epic-128): port siege-web sidecar integration suite as mom-bot conformance harness`
- **Labels:** `epic-128`, `sidecar`, `test`
- **Body:**

  **Summary.** Port `siege-web/backend/tests/integration/sidecar/` into mom-bot at `tests/integration/sidecar/` so the same conformance tests run against mom-bot's sidecar subprocess. This is the executable acceptance criterion for "mom-bot is a conforming alternate sidecar" per `INTERFACE.md:1-15`.

  **Context.** siege-web's harness launches the bundled bot in fake mode (`BOT_TEST_MODE=fake`, `siege-web/bot/app/fake_discord.py`) as a subprocess on port 8001 (`siege-web/backend/tests/integration/sidecar/conftest.py:91-140`). Mom-bot needs its own equivalent fake-discord-client implementation and a parallel `conftest.py` that boots `src/mom_bot/sidecar/app.py` instead of `bot/app/main.py`.

  **Acceptance criteria.**
  - Mom-bot has a `FakeDiscordClient` at `src/mom_bot/sidecar/fake_discord.py` modeled on `siege-web/bot/app/fake_discord.py` — supports the same minimum surface the 7 endpoints depend on (member lookup by name + ID, channel resolution by name, DM/post/post-image stubs that exercise the success + error paths the conformance table requires).
  - Mom-bot's `tests/integration/sidecar/conftest.py` boots `src/mom_bot/sidecar/app.py` in fake mode on port 8001 (or a configurable port for parallel runs).
  - All shape-assertion tests from `siege-web/backend/tests/integration/sidecar/` are ported and pass against mom-bot's surface. Path renames are mechanical (`bot/app/` → `mom_bot/sidecar/`); test bodies should be near-verbatim.
  - The "meta-shape" tests (`test_meta_shape_assertions.py`) that engineer broken shapes to confirm the assertions are tight — port these too; they catch regressions in shape-checking.
  - CI workflow runs this integration suite on every PR.

  **Technical notes.**
  - **Decision:** mom-bot's `FakeDiscordClient` lives at `src/mom_bot/sidecar/fake_discord.py` (shipped, gated by `MOM_BOT_TEST_MODE`), matching siege-web's `bot/app/fake_discord.py` structure. No live code paths are affected because dispatch is env-var-gated.
  - **`build_app` fake-mode dispatch lands in B-1, not here.** B-7 builds on the signature B-1 already extended. If `build_app` still takes only `discord.Guild` when B-7 starts, the implementer must retrofit B-1 first — flag that to the user before continuing. The expected B-1 signature is `build_app(api_key, client, session_factory)` where `client` is either `discord.Client` (prod) or `FakeDiscordClient` (test mode), with `is_ready()` and `guilds`/`get_guild(...)` duck-typed on both.

  **Out of scope.** Changing siege-web's bundled bot harness. Cross-repo CI integration (separate concern).

- **Depends on:** B-1 through B-6 (all 7 endpoints implemented).
- **Cross-repo prereqs:** read-only dependency on siege-web's integration suite (no PR against siege-web required for this sub-issue).

---

### Phase 4 — Deployment infra (mom-bot)

#### B-8. Update mom-bot Bicep to expose sidecar ingress

- **Title:** `feat(epic-128): expose sidecar port 8001 via Container Apps public HTTPS ingress with siege-web IP allowlist`
- **Labels:** `epic-128`, `infra`, `bicep`
- **Body:**

  **Summary.** Update mom-bot's Container Apps Bicep template so the sidecar's port 8001 is publicly reachable over HTTPS with a siege-web-scoped IP allowlist. Per D-2 (Mode 2 — public HTTPS, cross-region).

  **Acceptance criteria.**
  - mom-bot Container App declares an ingress entry for port 8001 with `external: true`.
  - Auto-managed certificate on the Container Apps environment's default domain (no custom domain required).
  - Container Apps ingress restrictions (IP allowlist) scoped to `20.245.166.6/32` only (siege-web env static outbound IP; verified 2026-05-21).
  - HSTS enabled on ingress.
  - Minimum TLS 1.2 enforced.
  - Bearer-token auth via `prod-discord-bot-api-key` (B-9 provisions).
  - [ ] **Verify deploy FIC has Container App contributor role at merge time** — soft note: #103 affects deployment RBAC scope for the FIC that deploys this Container App change. No friction either way (existing single FIC handles B-8 fine if #103 hasn't landed; verify role scope if #103 has landed first).
  - **Observability.**
    - **Tooling: Azure Monitor + App Insights** — reuse mom-bot's existing Application Insights workspace (no new resource).
    - **Alert ownership:** the **epic #128 driver (mom-bot maintainer)** owns alert rules at land time. Post-cutover, ownership transfers to the on-call rotation if one exists; otherwise stays with the maintainer.
    - **Alert thresholds:** implementer-defined against observed baseline traffic during the dev smoke (B-10). Thresholds are not pre-committed in this scoping plan — baseline traffic for the new endpoints is unknown until siege-web's dev exercises them; pre-committing without data produces either over-alerting noise or under-alerting blindness. Commitments at scoping time are: which signals get alerts (enumerated below), which tool (App Insights), and who owns them (epic #128 driver).
    - **Alert: 4xx rate spike** — could indicate IP allowlist misconfiguration or Bearer-token rotation drift.
    - **Alert: 5xx rate** — sidecar process broken or Container App unhealthy.
    - **Alert: p95 latency degradation** — cross-region path (West US → East US 2) degraded; baseline ~50-70ms per D-2.
    - **Certificate-expiry monitoring** — the auto-managed cert renews automatically; alert on imminent expiry as a defense-in-depth signal.
    - **Outbound IP-change detection for siege-web's env** — if siege-web recreates its Container Apps environment the static outbound IP (`20.245.166.6`) will change, silently breaking the allowlist. Detection method: on-403-spike alert (already enumerated above) provides timely signal when the allowlist goes stale. Originally drafted with both a weekly proactive cron and on-403-spike detection; weekly cron dropped per PR #157 review (over-engineered for a rare operator event when the 403-spike alert already provides timely signal).
  - **Operational runbook — IP allowlist drift.** If the App Insights 4xx alert fires and the pattern suggests allowlist misconfiguration (403 responses from siege-web's expected source): (1) re-run `az containerapp env show --name siege-web-cae-prod-yf3fl2t3yxmtk --resource-group siege-web-prod --query properties.staticIp -o tsv` to get the current outbound IP; (2) if the IP has changed, update the Bicep allowlist and redeploy. Capture this as a named runbook section in `docs/operations/epic-128-cutover.md` (that file lands in B-11; cross-link from there).

- **Depends on:** B-1 (sidecar exists). Can land in parallel with B-2 through B-7.
- **Cross-repo prereqs:** none (C-2 closed in-session; D-2 settled as Mode 2).

#### B-9. Provision `prod-discord-bot-api-key` in mom-bot KV

- **Title:** `chore(epic-128): provision prod-discord-bot-api-key in kv-mombot-eastus2`
- **Labels:** `epic-128`, `infra`, `secrets`
- **Body:**

  **Summary.** Create a new KV secret `prod-discord-bot-api-key` in `kv-mombot-eastus2` holding the Bearer token siege-web's backend will send to mom-bot's inbound sidecar. The value MUST match siege-web's `DISCORD_BOT_API_KEY` env var.

  **Acceptance criteria.**
  - Secret exists in `kv-mombot-eastus2`.
  - mom-bot's sidecar reads it at startup via `DefaultAzureCredential` (matching the existing pattern for `prod-discord-token`).
  - `docs/secrets-inventory.md` updated with the new secret's name + purpose.
  - Value coordinated with siege-web maintainer (whether to reuse siege-web's existing `BOT_API_KEY` for the bundled bot or generate a fresh value — recommend fresh to keep blast radius separate; the bundled bot's token can be retired post-cutover).

- **Depends on:** none.
- **Cross-repo prereqs:** value coordination with siege-web maintainer (no PR needed; secret update on siege-web side is part of C-1).

---

### Phase 5 — Pre-cutover verification (mom-bot)

#### B-10. Manual conformance smoke against mom-bot's dev sidecar from siege-web's dev backend

- **Title:** `chore(epic-128): smoke siege-web → mom-bot sidecar end-to-end against dev`
- **Labels:** `epic-128`, `verification`, `manual`
- **Body:**

  **Summary.** Before any production cutover, exercise the full 7-endpoint contract from siege-web's dev backend against mom-bot's dev sidecar. Captures any real-Discord behavior the fake-mode integration suite cannot.

  **Acceptance criteria.**
  - siege-web dev backend's `DISCORD_BOT_API_URL` is temporarily pointed at mom-bot's dev sidecar URL.
  - All 7 endpoints exercised via the existing siege-web admin flows that call them (send a DM via `/api/notify`, post a message via siege-web's notify UI, post an image, list members, look up a single member).
  - `/api/version` reports `mom-bot v0.x.y` (not the bundled bot version).
  - `/api/health` reports `bot_connected: true` once mom-bot's gateway is up.
  - No 502/503 errors from mom-bot's translation layer beyond expected ones (DM-blocked, etc.).
  - Findings logged in this sub-issue's comments; any drift from `INTERFACE.md` blocks Phase 9.

- **Depends on:** B-7 (conformance harness green), B-8 (ingress reachable from siege-web).
- **Cross-repo prereqs:** siege-web maintainer flips dev backend's `DISCORD_BOT_API_URL` temporarily.

---

### Phase 6 — Cross-repo enablement (siege-web — filed as sibling issues)

These are filed against `glitchwerks/rsl-siege-manager`, cross-linked from this epic.

#### C-1. Verify and document `useExternalSidecar` + `externalBotApiUrl` Bicep parameters in siege-web infra

- **Title (siege-web):** `docs(epic-128-sibling): verify useExternalSidecar + externalBotApiUrl params accept mom-bot HTTPS URL`
- **Labels (siege-web):** `infra`, `bicep`, `cross-repo`
- **Body:**

  **Summary.** Verify that siege-web's existing `useExternalSidecar` and `externalBotApiUrl` Bicep parameters (confirmed present at `infra/modules/container-apps.bicep:209`, verified 2026-05-21) accept mom-bot's `https://<env-default-domain>.eastus2.azurecontainerapps.io` URL and document the same-env vs cross-env usage distinction in `bot/INTERFACE.md`.

  **Context.** These parameters already exist — this sub-issue is verify-and-document, not add-new. The original C-1 framing ("add the parameter if missing") was invalidated by the 2026-05-21 session verification of `infra/modules/container-apps.bicep:209`.

  **Acceptance criteria.**
  - Confirm `externalBotApiUrl` accepts a public HTTPS URL (mom-bot's env-default-domain URL; no custom domain). If a guard enforces HTTPS-only-outside-dev and the URL passes, document that it passes. If the guard needs relaxing for a hypothetical future same-env / internal-DNS path, document the change needed (no mandatory change for v1.0).
  - `bot/INTERFACE.md` § Discord coupling updated to document the same-env vs cross-env distinction: internal HTTP is only valid within one Container Apps env; for cross-env (or cross-region) deployments, `externalBotApiUrl` must be an HTTPS URL.
  - Bicep `what-if` shows zero net infra change when `useExternalSidecar` remains `false` (regression safety).

- **Depends on:** none.
- **Cross-repo prereqs:** mom-bot's Container App URL confirmed (B-8 settles the FQDN).

#### C-2. Confirm/document siege-web bundled bot deployment shape (read-only, may be a comment-only Q&A)

- **Title (siege-web):** `docs(epic-128-sibling): document bundled bot deployment shape for mom-bot cutover planning`
- **Labels (siege-web):** `docs`, `cross-repo`
- **Body:**

  **Summary.** Confirm and document whether siege-web's bundled bot today runs as (a) a separate Container App in the same Container Apps environment as the backend, or (b) a co-located container inside the backend's Container App, or (c) a different shape entirely. The answer determines mom-bot Bicep work (B-8) and siege-web Bicep work (C-1).

  **Acceptance criteria.**
  - Add a section to `bot/INTERFACE.md` or `docs/architecture.md` (whichever fits siege-web's convention) describing the current deployment shape: Container Apps environment name, ingress mode, internal DNS hostname.
  - Link the section from mom-bot epic #128's body comment thread.

- **Depends on:** none.
- **Cross-repo prereqs:** none — siege-web maintainer answers directly.

#### C-4. Add 503-retry-with-backoff to siege-web `BotClient` sidecar methods **(HARD PREREQUISITE OF B-11)**

- **Title (siege-web):** `feat(epic-128-sibling): add 503-retry-with-backoff to BotClient sidecar methods`
- **Labels (siege-web):** `bot-client`, `reliability`, `cross-repo`, `epic-128-blocker`
- **Body:**

  **Summary.** Add a small retry-with-backoff wrapper to the four `BotClient` sidecar methods (`notify`, `post_message`, `post_image`, `get_members`, `get_member`) so transient 503 responses during the mom-bot cutover window self-heal instead of dropping silently. Required because the cutover involves a brief gateway gap (10-30s) during which the sidecar HTTP surface is unavailable.

  **Context.** Verified `siege-web/backend/app/services/bot_client.py:43-99` on 2026-05-20: the four sidecar methods make a single HTTP request, call `raise_for_status()`, and swallow `httpx.HTTPError` returning `False`/`None`/`[]`. No retry loop. The existing `_MAX_ATTEMPTS = 2` constant (line 30) is used only by `sync_day_role` (a separate webhook path, not a sidecar call).

  **Acceptance criteria.**
  - The four sidecar methods (and `get_member`) retry on `httpx.HTTPStatusError` with `response.status_code == 503` and on `httpx.ReadTimeout` / `httpx.ConnectError`. Other `httpx.HTTPError` subclasses (e.g. 4xx) are NOT retried.
  - Retry policy: 2 retries (3 total attempts), exponential backoff with jitter — start at 500ms, cap at ~5s.
  - On retry exhaustion, the methods preserve their current return contract (`False` / `None` / `[]` / raise for `get_member`'s assertion path) — no behavior change for callers.
  - Unit tests cover: 503-then-200 succeeds; 503-503-503 returns failure-sentinel; 404 fails fast (no retry); timeout-then-200 succeeds.
  - Integration test: sidecar restart mid-call results in the call succeeding (or failing cleanly) — no silent drop.

  **Out of scope.** Changes to `sync_day_role`'s retry policy (separate contract, already retries). Changes to `BotClient` callers' error handling.

- **Depends on:** none.
- **Cross-repo prereqs:** none.
- **Gates:** B-11 (cutover MUST NOT execute until C-4 has merged on siege-web `main` AND been deployed to prod).

#### C-3. Update `bot/INTERFACE.md` to name mom-bot as conforming alternate

- **Title (siege-web):** `docs(epic-128-sibling): name mom-bot as first conforming alternate sidecar`
- **Labels (siege-web):** `docs`, `cross-repo`
- **Body:**

  **Summary.** After mom-bot passes the conformance suite (B-7) and the dev smoke (B-10), update `bot/INTERFACE.md` to name mom-bot in the introductory section.

  **Acceptance criteria.**
  - `bot/INTERFACE.md`'s opening paragraph (`INTERFACE.md:1-15`) now reads "...an alternative sidecar implementation (e.g. **mom-bot at `glitchwerks/mom-bot`**, or an integration-test stub)..." or similar.
  - New section or footnote in § Replaceability requirements points readers to mom-bot's conformance harness location.
  - No technical content changes — pure documentation.

- **Depends on:** B-7, B-10.
- **Cross-repo prereqs:** none.

---

### Phase 7 — Production cutover (the actual switch)

#### B-11. Production cutover runbook + execution

- **Title:** `chore(epic-128): execute mom-bot ↔ siege bundled bot prod cutover`
- **Labels:** `epic-128`, `cutover`, `infra`
- **Body:**

  **Summary.** Execute the production cutover from siege-web's bundled bot to mom-bot's sidecar. Thursday or Friday post-13:00 UTC (per D-7). Single-operator runbook with rollback procedure.

  **Acceptance criteria.**
  - **Prerequisites (all must be green before the cutover window opens):**
    - All prior sub-issues complete + green (B-1..B-10, B-12 deferred, C-1, C-2, C-4).
    - **C-4 merged on siege-web `main` AND deployed to prod** (BotClient retries) — without this, the cutover is unsafe per D-3.
    - siege-web v1.2 deployed (Epic 2.5 + Epic 2.6 shipped).
    - `useExternalSidecar=true` deploy artifact ready.
    - Cutover-window slot confirmed with siege-web maintainer (Thursday or Friday post-13:00 UTC per D-7).
  - **Runbook drafted at `docs/operations/epic-128-cutover.md` covering:**
    - **Verified-quiet window check.** Before stopping the bundled bot, query App Insights for sidecar request volume over the previous 60 seconds. The "stop" step is gated on observing zero in-flight calls (or a count low enough that C-4's retry budget covers them). Document the exact KQL query and the pass/fail threshold in the runbook. **The wall clock is not the trigger — the quiet-window observation is.**
    - **Step-by-step.** (1) Verify-quiet check; (2) stop bundled bot; (3) verify Discord gateway session released; (4) start mom-bot's sidecar serving 7 endpoints; (5) flip siege-web `DISCORD_BOT_API_URL`; (6) smoke each endpoint from siege-web admin UI.
    - **Rollback.** Re-flip the env var; re-enable bundled bot's Container App. Rollback is one-step because C-4 already protects callers from the brief outage during rollback itself.
  - **Expected sidecar HTTP unavailability during gateway swap: 10-30 seconds.** Post-cutover verification: query App Insights for the cutover window and confirm that (a) no sidecar call exceeded C-4's retry budget (3 attempts, ~5.5s max total wait), and (b) the following graduated disposition applies to final-503s observed in the 5-minute window post-cutover:
    - **≥1 final-503 observed:** triggers investigation (review App Insights traces, confirm whether the call was retried per C-4, identify the root cause). Does not by itself block declaring the cutover complete.
    - **≥3 final-503s observed:** triggers rollback (re-flip `DISCORD_BOT_API_URL` per the runbook). C-4 was designed to make transient gateway-gap drops self-heal; ≥3 final-503s indicates either C-4 is not deployed (cutover prerequisite violated — should be impossible) or the failure mode is not transient (likely a sidecar contract or auth issue).
  - *Original Rev 3 phrasing was "any final-503 blocks complete"; refined per PR #157 review to a graduated investigate/rollback threshold. A single transient final-503 is real signal worth investigating but is not by itself sufficient to roll back a successful cutover; ≥3 indicates a systemic issue.*
  - Cutover executed; all 7 endpoints verified live in prod within 60 minutes of switch.
  - Post-cutover monitoring window: 48 hours of App Insights latency + error-rate review before declaring epic complete.

- **Depends on:** B-10 (dev smoke green), **C-1 (siege-web Bicep parameter shipped — critical path)**, **C-4 (BotClient retries shipped — hard gate, see D-3)**, C-3 (INTERFACE.md updated — optional but preferred pre-cutover).
- **Cross-repo prereqs:** siege-web maintainer executes the `useExternalSidecar=true` deploy + the env-var flip during the cutover window. **C-4 must be in prod _before_ the cutover starts.**

---

### Phase 8 — Post-cutover cleanup

#### B-12. Decommission siege-web's bundled bot resources

- **Title (siege-web):** `chore(epic-128-sibling): decommission bundled bot Container App + tests after mom-bot cutover`
- **Labels (siege-web):** `infra`, `cleanup`, `cross-repo`
- **Body:**

  **Summary.** After 7-14 days of stable prod operation on mom-bot, remove the bundled bot's Container App (or sidecar container) from siege-web's Bicep. Keep the integration test suite — it remains the canonical conformance contract for any future alternate sidecar.

  **Acceptance criteria.**
  - siege-web Bicep no longer declares the bundled bot's deployment resource; `useExternalSidecar` parameter retained for documentation but its `false` branch is now no-op (or the parameter is removed entirely — siege-web maintainer's call).
  - `siege-web/bot/` source tree retained (it remains the conformance reference); only the deploy step is removed.
  - Integration test suite at `siege-web/backend/tests/integration/sidecar/` retained verbatim.
  - Rollback ability documented: re-add the bundled bot deploy step if mom-bot needs to be replaced.

- **Depends on:** B-11 + 7-14 day stability window.
- **Cross-repo prereqs:** mom-bot is the sole sidecar in prod with no reverts.

---

### Phase 9 — Epic close-out

#### B-13. Close epic #128 + cross-link decision log to milestone v1.0

- **Title:** `chore(epic-128): close-out + decision log + v1.0 milestone hook`
- **Labels:** `epic-128`, `meta`
- **Body:**

  **Summary.** Close #128, fold this Phase 1 scoping doc's decision log into mom-bot's permanent decision-log (or memory), delete the plan file per CLAUDE.md "Lifecycle: delete plan files when done", and confirm v1.0 milestone close criteria.

  **Acceptance criteria.**
  - Decisions D-1 through D-7 from this plan document are copied into commit messages on the implementing PRs or into a durable memory file before this plan file is deleted.
  - #128 closes via `Closes #128` in B-11's merge commit (per CLAUDE.md "PR body must contain the closing keyword").
  - mom-bot v1.0 milestone closes once all child issues are resolved.

- **Depends on:** B-11, B-12, 14-day stability post-cutover.
- **Cross-repo prereqs:** none.

---

## 5. Dependency graph

```
B-1 (version + health + auth scaffold + build_app fake-mode dispatch)
 ├─→ B-2 (notify, introduces 403-on-missing-header) ─→ B-3 (post-message) ─→ B-4 (post-image)
 ├─→ B-2 ─→ B-5 (members) ─→ B-6 (members/{id})
 └─→ B-8 (Bicep ingress) ─→ B-10 (dev smoke)
B-7 (conformance harness) requires B-1..B-6
B-7 ─→ B-10 (dev smoke also requires conformance harness green)
B-9 (KV secret) — independent, lands early
C-1 (siege-web Bicep parameter) — siege-web side, parallel to B-2..B-7, CRITICAL PATH TO B-11
C-3 (INTERFACE.md update) — after B-7 + B-10 green
C-4 (BotClient 503 retries) — siege-web side, HARD GATE on B-11 (see D-3)
B-11 (cutover) requires B-10 + C-1 + C-4-in-prod
B-12 (siege-web cleanup) requires B-11 + stability window
B-13 (close-out) requires B-11 + B-12
```

**Cross-issue dependency — `build_app` fake-mode dispatch.** B-1 must land the signature extension to `build_app(api_key, client, session_factory)` (accepting either `discord.Client` or `FakeDiscordClient`) before B-7's implementer begins porting the conformance harness. The graph edge `B-1 → B-7` is structurally correct, but the implementation-shape coupling is implicit; review the existing notes in B-1 "Technical notes" and B-7 "Technical notes" before starting either sub-issue.

## 6. Blocking dependencies on other v1.0 milestone issues

Four other open milestone-v1.0 issues named in the brief (#103, #120, #121, #124) — their bodies were not included in the router's brief. The conservative assumption is that none of these block Epic #128's Phase 2-9 sub-issues unless one of them changes the sidecar's HTTP contract.

**Action before filing any sub-issue (5-minute pre-flight, user-driven).** Scan #103, #120, #121, #124 titles and bodies. Resolution rules:

- If any touches `src/mom_bot/sidecar/app.py` → explicit prerequisite to B-1; resequence.
- If any touches Epic 2.6's `/api/internal/role-sync` contract → potential interference with B-1's auth scaffold refactor; coordinate.
- If any touches mom-bot's Container Apps Bicep → explicit prerequisite to B-8.
- Otherwise, proceed.

The user should perform this scan before B-1 is filed, not at Phase 1 sign-off. The Phase 1 deliverable does not depend on the outcome — the plan structure handles either outcome — but B-1's "Depends on: none" claim does.

**Pre-flight executed 2026-05-21** — verified bodies of #103, #120, #121, #124. None touch `src/mom_bot/sidecar/app.py`, `/api/internal/role-sync`, or mom-bot Container Apps Bicep. B-1's `Depends on: none` stands. Two soft notes: (a) #103 affects deployment FIC scope when B-8 modifies the Container App — B-8 body now includes a verify-FIC-role check; (b) #120 references mom-bot Container App outbound IP `57.162.41.33` in `infra/modules/postgres.bicep` — independent of #128, flagged for downstream awareness if mom-bot's env is ever recreated.

## 7. Cross-repo coordination checklist (siege-web side)

Items requiring direct sign-off or action from the siege-web maintainer before specific mom-bot phases proceed:

- [x] **Confirm Container Apps environment topology** (resolved 2026-05-21 — different envs, different regions, see § 2 and D-2). mom-bot is in `cae-mom-bot-eastus2` (East US 2); siege-web's bot env is `siege-web-cae-prod-yf3fl2t3yxmtk` (West US). Internal DNS infeasible; Mode 2 (public HTTPS) chosen for D-2. C-2 confirmed siege-web's bundled bot is a separate Container App (no PR needed).
- [ ] **C-1 — `useExternalSidecar` Bicep parameter (CRITICAL PATH TO B-11).** Confirm whether the parameter exists in siege-web's infra today. If not, C-1 must be filed AND merged AND deployed by siege-web's maintainer before B-11 can execute. **Confirm a C-1 owner and ETA before filing B-11.** If siege-web's maintainer cannot commit to a date, B-11 must be postponed; mom-bot's Phase 2-8 still proceeds.
- [ ] **C-4 — BotClient 503 retries (HARD GATE on B-11, see D-3).** Confirm siege-web maintainer ownership of C-4. C-4 must be merged AND deployed to siege-web prod **before** the cutover window opens. Without C-4, the cutover risks silent data loss (the prior plan revision's "BotClient retries 503s" claim was false — verified `bot_client.py:43-99`).
- [ ] **Confirm siege-web v1.2 timeline** — does Epic 2.5 + Epic 2.6 ship before mom-bot's cutover window? (gates B-11)
- [ ] **Coordinate `prod-discord-bot-api-key` secret value** — reuse existing siege-web `BOT_API_KEY` or rotate? (gates B-9)
- [ ] **Agree on integration test suite portability convention** — siege-web's tests stay canonical, mom-bot copies them. Acceptable? (gates B-7)
- [ ] **Cutover-window slot** — pick a specific Thursday or Friday post-13:00 UTC. (gates B-11)
- [ ] **Post-cutover cleanup ownership** — siege-web maintainer drives C-3 + B-12. Confirm. (gates B-12)
- [ ] **Framework plan correction (#158)** — region-lock claim verified incorrect 2026-05-21; corrective docs PR tracked at #158. Not a blocker for any sub-issue.

### Cross-repo delay fallback strategy

If siege-web cannot deliver C-1 or C-4 on the timeline B-11 requires, the following triggers and actions apply.

**Triggers.**

- **(a) C-1/C-4 ownership unresolved** — siege-web maintainer cannot confirm ownership of C-1 or C-4 within 2 weeks of the planned cutover date.
- **(b) C-4 not in prod** — C-4 has not landed on siege-web `main` by 1 week before the planned B-11 window.
- **(c) siege-web v1.2 slip** — siege-web v1.2 ship date slips past the planned B-11 window.

**Actions per trigger.**

- **C-1/C-4 ownership unresolved (trigger a):** Postpone B-11. Phases 2-8 (B-1..B-10) continue on schedule — they do not depend on siege-web. File a follow-up issue tracking "B-11 cutover blocked on siege-web cross-repo work" so it is not lost in backlog.
- **C-4 not yet in prod (trigger b):** Postpone B-11. Without C-4's BotClient retries, the cutover risks silent call drops during the gateway swap (per D-3). No mom-bot-side workaround exists.
- **siege-web v1.2 slip (trigger c):** Postpone B-11. mom-bot's sidecar can be exercised against siege-web dev (B-10) regardless, but prod cutover requires v1.2's `externalBotApiUrl` to be set.

**Communication owner: epic #128 driver** (`@cbeaulieu-gt` at the time of this plan; in general, whoever owns the mom-bot release coordination at cutover time). They post a comment on epic #128 naming the blocker, the new estimated cutover window, and which mom-bot-side work continues unblocked.

*(D-5's "Cross-repo follow-up" paragraph references this subsection for the formal fallback logic.)*

## 8. Citations & verifications

- `siege-web/bot/INTERFACE.md` — full read on 2026-05-20; line ranges cited inline above. Verified contract is authoritative per its own opening paragraph.
- `siege-web/backend/tests/integration/sidecar/conftest.py:91-140` — fake-mode subprocess pattern verified.
- `siege-web/bot/app/main.py:79-97` — fake-mode + production-mode entry points verified.
- `src/mom_bot/sidecar/app.py:167-191` — current sidecar `build_app` signature (single-guild, no `/api/health`) verified.
- `src/mom_bot/sidecar/app.py:199-231` — current 422→400 conversion verified; explicit out-of-scope override note in B-2.
- `src/mom_bot/sidecar/app.py:237-254` — current `_require_bearer` returns 401 on absent header; B-1 swaps to `HTTPBearer(auto_error=True)` for 403 to match the bundled bot (verified 2026-05-20).
- `siege-web/backend/app/services/bot_client.py:43-99` — verified 2026-05-20: four sidecar methods are single-attempt and swallow `httpx.HTTPError`; no retry loop. Motivates D-3 mitigation + C-4.
- `siege-web/backend/tests/integration/sidecar/test_auth.py:29-134` — verified 2026-05-20: `== 403` assertion on missing-header across 4 endpoints. Motivates B-1's auth swap.
- Framework plan `docs/superpowers/plans/2026-05-08-mom-bot-framework.md` lines 5-22, 90-100, 320-336 — referenced for Pre-Epic-0 token-inheritance resolution, region locking (`eastus2`), and cutover-window rule (Thursday/Friday post-13:00 UTC).
- Epic #128 body — read at https://github.com/glitchwerks/mom-bot/issues/128 via the brief; not re-fetched (router's pre-Explore embedded its content in this task brief).
- `az containerapp env show` for `cae-mom-bot-eastus2` and `siege-web-cae-prod-yf3fl2t3yxmtk` — verified 2026-05-21; env regions (East US 2 / West US), static outbound IPs (`57.162.41.33` / `20.245.166.6`), and `vnetConfig: null` values cited in § 2 and D-2.
- `infra/modules/container-apps.bicep:209` in siege-web — verified 2026-05-21 via in-session paste; `useExternalSidecar` and `externalBotApiUrl` parameters confirmed to exist. C-1 de-scoped accordingly.
- Discord gateway/REST network paths — verified via discord.py docs and in-session reasoning 2026-05-21; sidecar ingress allowlist requires only siege-web egress IPs (`20.245.166.6/32`). Discord gateway events arrive over a WebSocket mom-bot opens (not a separate inbound connection); Discord REST calls are outbound from mom-bot. No Discord IP ranges required in the allowlist.

## 9. Quality-check self-review

Against the project-planner § Quality Checks discipline:

- **Testable & unambiguous acceptance criteria** — each sub-issue's AC names specific endpoints, response shapes, status codes, file paths. ✓
- **Hidden assumptions surfaced** — D-2 / D-3 / D-5 marked CROSS-REPO CONFIRM; v1.0-milestone-issue cross-blocking explicitly flagged as un-verified in § 6. ✓
- **Technical decisions documented as assumptions** — every D-* entry names the choice + rationale; Auto Mode disclaimer at top makes it explicit they are assistant calls, not user confirmations. ✓
- **Scope bounded** — Phase 1 deliverable is this document; sub-issues are specs not filings. ✓
- **A team could start work from this** — each sub-issue has the file paths, contract sources, and acceptance criteria a single implementer needs. ✓
- **Open questions explicit** — § 7 cross-repo checklist; § 6 v1.0-milestone cross-blocking. ✓
- **Citations** — every load-bearing claim cites a file:line range or a framework-plan section. Memory references explicitly avoided per CLAUDE.md "Memory file paths are not an acceptable citation form". ✓
