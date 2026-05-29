# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<!-- Add a "### 📣 Highlights" sub-section here before cutting the next release.
     See RELEASING.md § "Discord Highlights convention" for what to write there. -->

### Changed

- README: audit for staleness — Status, Roadmap, Database/Migrations, Project Structure, References sections refreshed to reflect v1.0 ship + v1.1 in progress (refs #249).
- Runbook + secrets-inventory: resolve stale TBDs and "Epic 1+" placeholders (closes #249).

### Fixed

- **OIDC federated credentials** updated to repo's canonical name `rsl-mom-bot` (was `mom-bot` pre-rename). Both `mom-bot-pr` and `mom-bot-main-push` FICs now match GitHub's current OIDC subject claim; unblocks the `Bicep what-if preview` workflow on PR-triggered runs and the next `workflow_dispatch` of `deploy.yml` (#248).

### Removed

- Removed five completed plan files per lifecycle policy (#246).

## [1.0.0] - 2026-05-26

### Added

- **Day-role-sync receiver** — `POST /api/internal/role-sync` sidecar endpoint receives day-role webhooks from siege-web, applies or removes Discord roles via the `mom_bot/roles/` service, and persists idempotency state so exact replays short-circuit the service call. Per-`discord_id` `asyncio.Lock` prevents concurrent stale-write races; corrupted stored JSON self-heals on the next write. Contract: `glitchwerks/rsl-mom-apps` `contracts/sidecar-api.yaml`. (#71)
- **Full northbound sidecar HTTP API** — FastAPI app served on port 8001 at bot startup, with reusable Bearer auth (`secrets.compare_digest`), structured request/response models, and HTTP-level error translation. Endpoints: `GET /api/version`, `GET /api/health` (#184); `GET /api/members`, `GET /api/members/{discord_user_id}` (#185); `POST /api/notify` (#190); `POST /api/post-message` (#191); `POST /api/post-image` (#192). Sidecar wired into `make_client()` / bot startup sequence. (#163, #184, #185, #190, #191, #192)
- **Post-condition slash commands** — `/post-conditions catalog` and `/post-conditions me` proxy the siege-web preferences API, letting Discord members view and set their post-condition priorities without leaving Discord. UX evolved through several iterations: initial Select widget (#129), live-updating embed (#137), Button + Modal + CheckboxGroup flow (#139), button-grid V1 (#147), unified `/post-conditions` + `/post-conditions-get` with set-summary embed (#150). Includes catalog cache, Retry-After backoff, and Discord `defer` for slow responses. (#129, #133, #135, #137, #139, #147, #150)
- **`X-Acting-Discord-Username` header** — outgoing requests to siege-web now include the acting member's Discord username for actor identification on proxied calls. (#156)

### Changed

- **Member-not-registered error message** rewritten to direct users to contact admins rather than exposing an internal state description. (#153)

### Fixed

- **`day_number` resolution on unassign** — sidecar now resolves `day_number` from stored role-sync state on unassign, rather than requiring the caller to supply it. (#205)
- **Sidecar auth: 403 on missing `Authorization` header** — previously returned 401; corrected to 403 per contract. (#188)
- **Sidecar per-boundary validation** — sidecar now returns 422 with structured error bodies on invalid request payloads; previously surfaced as unhandled 500s. (#189)
- **Day-role name seeder** — corrected expected role names to `Siege - Day {n} Attacker`; enriched `DAY_ROLE_NOT_FOUND` log with expected and available role names. (#131, #132)
- **Snowflake columns widened to BIGINT** — reminder table snowflake columns were `INTEGER`, truncating large Discord IDs. (#123)
- **Docker / venv invocation** — bot startup now invokes the venv Python directly, avoiding a `uv` cache-dir permission denial on container restart. (#117, #119)
- **`psycopg` moved to runtime dependencies** — was incorrectly scoped to `[dev]`, causing import failures in production. (#115)
- **`python-multipart` added to lock** — `uv.lock` regenerated to include `python-multipart`, required by FastAPI form parsing. (#201)

### Infrastructure

- **PostgreSQL Flexible Server** — full migration from SQLite: provisioned ACA-integrated Postgres instance (#105), Postgres-portable Alembic dialect branching (#108), AAD-token engine with managed-identity auth + startup migrations removed (#110), Postgres admin-race fixed (#111), `alembic upgrade head` step in deploy pipeline (#113).
- **HTTPS ingress on port 8001** — Container App configured for public HTTPS ingress with IP allowlist; siege-web-api-dev CAE egress allowlisted. (#162, #193)
- **`scale.minReplicas = 1`** — prevents the Container App from scaling to zero and dropping the Discord gateway connection. (#183)
- **RBAC hardening** — `SystemAssigned` identity dropped (#82); `AZURE_CLIENT_ID` wired through Bicep to `ManagedIdentityCredential` (#86); `mom-bot-gha` service principal granted constrained RBAC Admin at RG scope for Key Vault role management (#170); redundant GHA SP role assignments removed and ABAC conditions narrowed (#175).
- **`infra-deploy.yml` workflow** — `workflow_dispatch`-only pipeline for Bicep applies; includes `set +e` wrapper to preserve `az deployment` error output. (#164, #168)
- **Bicep what-if PR preview** — automated what-if diff posted as PR comment on infrastructure changes. (#100)
- **SQLite via AzureFile** (pre-Postgres interim) — volume mount, secret reference, replica lock, and snapshot config. Superseded by the Postgres migration. (#92)

### Observability

- **Startup URL log** — `make_client()` emits `INFO mom_bot.main Configured siege-web base URL: <url>` at cold start, giving operators an instant cross-environment sanity check before any Discord gateway traffic. (#211)
- **`/healthz` liveness probe** — Container App liveness probe switched from `exec`-type (rejected by ARM) to `httpGet` against `/healthz`. (#88)

---

**Pre-1.0 history**: Initial pre-1.0 development — see `git log` and the merged PR history for full provenance.

[Unreleased]: https://github.com/glitchwerks/rsl-mom-bot/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/glitchwerks/rsl-mom-bot/releases/tag/v1.0.0
