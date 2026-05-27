# mom-bot v1.0.0

*Date TBD (will be set at tag-cut time per #217)*

---

## What this release is

mom-bot is a Discord bot that bridges siege-web (the RSL Siege Manager web app) and a guild's Discord server. It exposes a northbound HTTP API for siege-web webhooks and command-driven integration, and it surfaces siege-web data to Discord members via slash commands.

v1.0.0 is a baseline, not a feature-charter completion. mom-bot has been running in production for months; v1.0.0 freezes the current state and starts semver from here. Future versions follow the policy in `RELEASING.md`: breaking changes are MAJOR, additive changes are MINOR, and fixes are PATCH.

---

## What's new in 1.0

### Northbound integration with siege-web

**Sidecar HTTP API.** mom-bot runs a FastAPI server on port 8001 alongside the Discord gateway. The API uses Bearer token authentication (`discord-bot-api-key` from Key Vault) and provides the following endpoints:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/version` | Returns the running bot version |
| `GET /api/health` | Returns gateway connection status |
| `GET /api/members` | Lists guild members |
| `GET /api/members/{discord_user_id}` | Returns a single member |
| `POST /api/notify` | Sends a DM to a member |
| `POST /api/post-message` | Posts a text message to a channel |
| `POST /api/post-image` | Posts an image to a channel |
| `POST /api/internal/role-sync` | Receives day-role-sync webhooks from siege-web |

The API is publicly reachable via HTTPS on port 8001 with an IP allowlist. All error responses are structured; the 403/404/502/503 status codes map to Discord API error classes predictably.

**Day-role-sync receiver.** When a member's day role changes in siege-web, siege-web POSTs a webhook to `POST /api/internal/role-sync`. mom-bot assigns or unassigns the corresponding Discord role within seconds. The receiver deduplicates exact replays via persisted idempotency state, preventing stale-write races with a per-`discord_id` lock. If the stored sync state is corrupted, it self-heals on the next write.

**Member preferences proxy.** Slash commands that read or write a member's post-condition preferences in siege-web pass the acting member's Discord username in the `X-Acting-Discord-Username` header. This is how siege-web identifies the actor on proxied calls without requiring Discord OAuth.

### Discord experience

Four slash commands are registered guild-wide:

| Command | What it does |
|---------|-------------|
| `/ping` | Returns `pong!`, the running bot version, and process uptime. Useful as a liveness check from Discord. |
| `/post-conditions` | Shows the full post-condition catalog from siege-web, grouped by category (Role, Affinity, Faction, League, Rarity, Effect, Other). Ephemeral. |
| `/post-conditions-get` | Shows the invoking member's current post-condition preferences. Ephemeral. |
| `/post-conditions-set` | Opens a paginated button-grid editor for the invoking member to set their post-condition priorities without leaving Discord. Ephemeral. |

All post-condition commands enforce per-user scope — they operate on the invoking member's Discord ID only. There is no admin-override or target-user parameter. Members who have not yet been registered in siege-web see a directed message asking them to contact a clan admin.

### Operations and observability

**Startup URL log.** At cold start, mom-bot logs the resolved siege-web base URL:

```
INFO mom_bot.main Configured siege-web base URL: <url>
```

This line is the fastest way to catch a cross-environment misconfiguration (e.g., prod KV value pointing at the dev siege-web instance) before Discord traffic starts flowing.

**`/ping` reports version and uptime.** From any Discord client, `/ping` returns the running package version and process uptime in seconds. No shell access required to confirm which image is running.

**KV-backed secrets with rotation support.** All operational secrets — bot token, API key, siege-web URL, guild ID, reminder channel — are resolved from Azure Key Vault at startup via the `{env}-` prefixed secret names. The `discord-bot-api-key` sidecar secret lives on its own KV entry for independent rotation.

**`/healthz` liveness probe.** The Container App liveness probe hits `GET /healthz` over HTTP on port 8080. The probe is available before the Discord gateway connects, so the ACA health check passes as soon as the container is up.

**Scale floor.** `scale.minReplicas = 1` ensures the Container App never scales to zero. Scaling to zero would drop the Discord gateway connection and miss incoming events.

---

## Upgrade notes

v1.0.0 is structurally identical to `main` at the tagged SHA (`3389b2a`). There is no behavioral delta between the `:main` image at tag time and `:v1.0.0`.

- **No code or contract changes from the current `:main` image.** Operators running `:main` in prod are already running what v1.0.0 ships.
- **Pin optionally.** Operators who want a stable pinned image may switch to `:v1.0.0`. The `:main` image stream continues to track `main` as before.
- **No database migration required.** The schema at v1.0.0 is what is already running in prod. `alembic upgrade head` is a no-op on a current deployment.
- **No environment variable changes required.** No new env vars, no renamed env vars, no changed defaults.

Deploying v1.0.0 to prod is a standard image-swap via `deploy.yml` `workflow_dispatch` with the tagged SHA. See `RELEASING.md` for the tag-push and deploy sequence.

---

## Known issues and out-of-scope

**#210 — `_log_loaded_config()` extension deferred to 1.1+.** The startup URL log (above) landed for `siege-web-url`. The broader extension — logging all non-credential KV values at startup so any config drift is visible at a glance — was scoped out of 1.0. Tracked in #210; targeted at 1.1+.

**#75 — `discord-roles-preflight.md` role-name resolution claim is incorrect (documentation bug).** The runbook claims a specific role-name resolution path that does not match the seeder's actual behavior. No runtime impact; only affects operators reading that doc. Fix is straightforward; deferred because it is a documentation-only change.

**#121 — Missing `prod-reminder-channel-name` and `prod-reminder-mention-role-name` KV secrets block #112.** The reminder scheduler requires these two secrets to be present in the prod Key Vault. If they are absent, the reminder-init background task fails at startup (logged at CRITICAL) and the scheduler does not start. The Discord gateway and sidecar API continue to operate normally. This is an ops gap, not a code bug; the secrets need to be provisioned before the reminder feature is usable in prod.

**Semver from here.** v1.0.0 starts the semver clock. Future changes follow the versioning policy in `RELEASING.md`: sidecar contract breaks or slash command renames are MAJOR, additive endpoints or new commands are MINOR, and bug fixes are PATCH. Breaking contract changes go through the coord-issue protocol in `glitchwerks/rsl-mom-apps` before any code lands.

---

## Related contracts

The sidecar HTTP API is specified in `glitchwerks/rsl-mom-apps` at `contracts/sidecar-api.yaml` (contract version **1.0.0**). That file is the canonical source of truth for endpoint shapes, auth scheme, error semantics, and the day-role-sync payload format. `bot/INTERFACE.md` in `glitchwerks/rsl-siege-manager` provides a consumer quick-start checklist against the same contract.

The day-role-sync producer on the siege-web side ships dark behind `DAY_ROLE_SYNC_ENABLED=false` (default). Enabling it requires setting both `DAY_ROLE_SYNC_ENABLED=true` and `DAY_ROLE_SYNC_URL=<receiver-endpoint>` in siege-web's environment. See the v1.3.0 release notes for siege-web for the producer-side configuration.

---

*Source CHANGELOG entries: `## [1.0.0] - 2026-05-26` in `CHANGELOG.md`. Verifiable via `git -C I:/games/raid/mom-bot/.worktrees/release-notes-216 ls-tree HEAD -- CHANGELOG.md`.*
