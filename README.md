# mom-bot

Discord bot consolidating two existing bots — `siege-web`'s notifications sidecar and the reminder system from `I:\games\raid\siege\clan\` — into a single bot with interactive slash commands.

**Status:** v1.0 shipped (2026-05-26) — full sidecar + reminder bot consolidation, interactive slash commands, PostgreSQL on Azure. v1.1 in progress ([milestone #3](https://github.com/glitchwerks/rsl-mom-bot/milestone/3)) — infra + documentation hardening: UAMI migration job, KV-secret parameterisation, ACA ingress audit, dead-mount cleanup, observability wire-up, plus a documentation correctness sweep. Manual Azure provisioning for new environments requires the AAD preflight steps in `infra/aad-runbook.md`.

## Documentation

- **Framework plan:** [`docs/superpowers/plans/2026-05-08-mom-bot-framework.md`](docs/superpowers/plans/2026-05-08-mom-bot-framework.md) — locked design decisions, phasing, risks, and verification per epic
- **Cross-repo dependency:** Epic 2.5 lands as a v1.2 ticket in [glitchwerks/siege-web](https://github.com/glitchwerks/siege-web)

## Roadmap

The plan defines 5 epics + 1 cross-cut + 1 pre-epic gate:

| Phase | Scope | Status |
| --- | --- | --- |
| **Pre-Epic-0** | Discord application audit + reminder-bot deployment typing (gates Epic 0) | Shipped (v1.0) |
| **Epic 0** | Skeleton: new repo wiring, Discord client, App Insights, SQLite baseline, `/ping` health-check | Shipped (v1.0) |
| **Epic 1** | Reminder lift-and-shift (port from `siege/clan/`; JSON file → SQLite) | Shipped (v1.0) |
| **Epic 2** | Sidecar lift-and-shift (port `siege-web/bot/`'s 6 HTTP endpoints into mom_bot's service half) | Shipped (v1.0, #128) |
| **Epic 2.5** | Siege-web cross-cut (`/me/preferences` endpoints + `X-Acting-Discord-Id` header support — lands in siege-web v1.2) | Open (siege-web v1.2) |
| **Epic 2.6** | Day-role sync — `POST /api/internal/role-sync` + `mom_bot/roles/` service | Shipped (v1.0, #6) |
| **Epic 3** | Interactive slash commands (`/post-conditions catalog`, `/post-conditions me`) | Shipped (v1.0) |
| **Epic 4** | Cutover (deploy to Azure RG `mom-bot`, retire siege-bot + old reminder-bot) | Shipped (v1.0) |
| **PostgreSQL migration** | Replace SQLite-on-SMB with Postgres Flexible Server + AAD token auth | Shipped (v1.0, #91) |

See the framework plan for design decisions, scope locks, risks, and verification per epic.

## Prerequisites

- **Python 3.12** — `python --version` must show `3.12.x`
- **[uv](https://github.com/astral-sh/uv)** — fast Python package manager (`pip install uv` or see uv docs)
- **Docker** — for container smoke tests (`docker build .`)

## Local Development

```bash
# 1. Create a virtual environment
uv venv .venv

# 2. Install the package and dev dependencies
uv pip install -e ".[dev]"

# 3. Run the test suite
.venv/Scripts/python.exe -m pytest          # Windows
# .venv/bin/python -m pytest               # Linux / macOS

# 4. Lint and format checks
.venv/Scripts/python.exe -m ruff check src/ tests/
.venv/Scripts/python.exe -m black --check src/ tests/

# 5. Type checking
.venv/Scripts/python.exe -m mypy src/

# 6. Container smoke build
docker build .
```

## Local Azure Access

Mom-bot reads secrets from Azure Key Vault (`kv-mombot-eastus2`) at runtime via
`DefaultAzureCredential`. On a developer laptop this resolves to your `az login`
session — no managed identity or service principal needed locally.

**Prerequisites:**

```bash
# 1. Log in to the mom-bot tenant (always pass --tenant to avoid cross-tenant confusion)
az login --tenant 48bca6c3-6d4f-4884-bc1a-648ae2362a32

# 2. Set the target subscription
az account set --subscription 213aa1f8-32d1-4ffe-8f4d-6e60f1cd9dc0

# 3. Verify
az account show --query '{tenant:tenantId, sub:id}' -o table
```

**Role requirement:** your user account needs `Key Vault Secrets User` on
`kv-mombot-eastus2`. Request this from the repo admin (@cbeaulieu-gt), or grant
it yourself if you have Owner/User Access Administrator on the subscription:

```bash
MY_OID=$(az ad signed-in-user show --query id -o tsv)
KV_ID=$(az keyvault show -g mom-bot -n kv-mombot-eastus2 --query id -o tsv)
az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee-object-id "$MY_OID" \
  --assignee-principal-type User \
  --scope "$KV_ID"
```

**Running locally with Key Vault secrets:**

```bash
# MOM_BOT_ENV=dev causes config.load_secret() to read dev-* secrets from KV.
MOM_BOT_ENV=dev python -m mom_bot
```

`DefaultAzureCredential` picks up your `az login` session automatically — no
additional environment variables required. See `docs/secrets-inventory.md` for
the full list of secrets and their purposes.

### Running the bot locally

After `Local Azure Access` is set up and `dev-discord-token` + `dev-guild-id`
are seeded in `kv-mombot-eastus2`:

```powershell
$env:MOM_BOT_ENV = "dev"
.\.venv\Scripts\python.exe -m mom_bot
```

The bot connects, logs connection details, and registers `/ping` to the dev
guild. Test it from the dev guild's chat — the response is ephemeral (only
visible to you). Seed `dev-guild-id` via:

```bash
az keyvault secret set \
  --vault-name kv-mombot-eastus2 \
  --name dev-guild-id \
  --value "<your-discord-server-id>"
```

Enable Discord Developer Mode (User Settings → Advanced → Developer Mode) to
right-click the server icon and copy the guild ID.

## Database / Migrations

Mom-bot uses [Alembic](https://alembic.sqlalchemy.org/) for schema migrations backed by SQLAlchemy.
The local dev default is SQLite (developer convenience — no Azure credentials needed for schema work); production uses a PostgreSQL Flexible Server (`pg-mombot-*` in resource group `mom-bot`). The active database is selected via the `MOM_BOT_DATABASE_URL` environment variable (see `docs/secrets-inventory.md` for the canonical secret names).

**Apply all pending migrations:**

```bash
alembic upgrade head
```

**Generate a new migration after adding or changing models:**

```bash
# 1. Generate the migration file (review it before applying)
alembic revision --autogenerate -m "describe change"

# 2. Review migrations/versions/<rev>_describe_change.py — remove any spurious ops

# 3. Apply the migration
alembic upgrade head
```

Set `MOM_BOT_DATABASE_URL` to override the default SQLite URL for prod/staging
(e.g. `postgresql+psycopg://user:pass@host/dbname` — the project uses psycopg v3; `psycopg2` is not installed).

## Project Structure

```
mom-bot/
├── src/
│   └── mom_bot/                        # Main package (src-layout)
│       ├── __init__.py                 # Package version
│       ├── __main__.py                 # `python -m mom_bot` entrypoint
│       ├── main.py                     # Discord client, intents, slash commands
│       ├── config.py                   # MOM_BOT_ENV-aware config + KV secret load
│       ├── db/
│       │   └── __init__.py             # SQLAlchemy DeclarativeBase
│       ├── health/
│       │   ├── __init__.py
│       │   └── liveness.py             # /health/* liveness/readiness probes
│       ├── post_conditions/            # Siege post-conditions: grid layout, Discord views, slash commands
│       │   ├── client.py
│       │   ├── commands.py
│       │   ├── discord_display.py
│       │   ├── grid_layout.py
│       │   ├── grouping.py
│       │   └── views.py
│       ├── reminders/                  # Reminder system (Epic 1 lift-and-shift)
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── scheduler.py
│       │   ├── seed.py
│       │   └── sent_store.py
│       ├── roles/                      # Day-role sync (Epic 2.6)
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── seed.py
│       │   └── service.py
│       └── sidecar/                    # HTTP sidecar (Epic 2 lift-and-shift)
│           ├── __init__.py
│           ├── app.py
│           ├── auth.py
│           └── models.py
├── migrations/                         # Alembic migration scripts
│   ├── env.py                          # Wired to Base.metadata; reads MOM_BOT_DATABASE_URL
│   ├── script.py.mako                  # Migration file template
│   └── versions/                       # Migration history (baseline + 4 revisions)
├── tests/                              # Pytest suite (unit + integration)
│   ├── post_conditions/
│   ├── roles/
│   ├── sidecar/
│   └── test_*.py                       # Top-level smoke, config, alembic, health tests
├── alembic.ini                         # Alembic config (local SQLite default)
├── docs/                               # Design docs, secrets inventory, framework plan
├── infra/                              # Bicep templates + AAD runbook
├── pyproject.toml                      # PEP 621 metadata, tool configs
├── Dockerfile                          # Container build (python:3.12-slim, non-root)
└── .dockerignore
```

## References

- Framework plan: [`docs/superpowers/plans/2026-05-08-mom-bot-framework.md`](docs/superpowers/plans/2026-05-08-mom-bot-framework.md)
- Active milestone: [mom-bot v1.1 — infra + doc hardening](https://github.com/glitchwerks/rsl-mom-bot/milestone/3)

## Versioning

Mom-bot is its own product on its own version track (`mom-bot v0.1` → `v1.0`), separate from siege-web. The runtime is coupled to siege-web by design (shared Discord token, sidecar HTTP contract, shared guild) — the separate-repo / separate-versioning is for code-organization clarity, not real separability.

## License

TBD — to be set before first public release.
