# Postgres AAD Probe — Spike #101

Standalone CLI that verifies the full PGPASSWORD + AAD-token + psycopg3 auth
path against an Azure Postgres Flexible Server.  Refs #91, #97.

**Delete this script after spike #101 closes.**

---

## Prerequisites

1. `az login` — authenticate your local Entra identity.
2. `az account set --subscription <sub-id>` — target the correct subscription.
3. The caller (your Entra account) must be assigned as **Entra Admin** on the
   target Flexible Server instance.
4. Install psycopg3 (not in `pyproject.toml` — spike-only):
   ```
   uv pip install "psycopg[binary]"
   ```
   `azure-identity` is already a project dependency — no extra install needed.

---

## Invocation

### PowerShell

```pwsh
.\.venv\Scripts\python.exe scripts\spike\postgres_aad_probe.py `
  --host pg-mom-bot-spike.postgres.database.azure.com `
  --user cmb_dev@outlook.com
```

### Bash

```bash
./.venv/Scripts/python.exe scripts/spike/postgres_aad_probe.py \
  --host pg-mom-bot-spike.postgres.database.azure.com \
  --user cmb_dev@outlook.com
```

### With `--alembic` flag (full Charge 5 path — validates PR #97 migration)

```pwsh
.\.venv\Scripts\python.exe scripts\spike\postgres_aad_probe.py `
  --host pg-mom-bot-spike.postgres.database.azure.com `
  --user cmb_dev@outlook.com `
  --alembic
```

### Verbose output

Add `--verbose` / `-v` for DEBUG-level logging (token acquisition details,
psycopg3 close confirmation, etc.).

---

## Expected outputs

**Pass:**
```
RESULT: ok
```
All four steps complete without error: token minted, psycopg3 connected,
smoke SELECT returned `current_user` / `current_database()` / `now()`,
and (if `--alembic`) alembic exited 0.

**Fail examples:**
```
RESULT: fail: sys.exit(1)   # token acquisition or connection error
RESULT: fail: alembic upgrade head returned non-zero
```
Preceding log lines will identify the exact step and exception message.
