# Spike #101: Postgres AAD Auth Verification — Findings

**Issue:** [#101](https://github.com/glitchwerks/mom-bot/issues/101) |
**Plan PR:** [#97](https://github.com/glitchwerks/mom-bot/pull/97) |
**Date:** 2026-05-17

---

## TL;DR

- PGPASSWORD + AAD token + psycopg3 works end-to-end against Azure Postgres Flexible Server (Charge 2 **PROVEN**).
- Observed token TTL is **~86 minutes**, not the plan's assumed 24h upper bound — `pool_recycle` ceiling drops to **≤ 4800 s (80 min)** (Charge 3 **DATA CAPTURED**).
- Migration `0002_reminders_schema.py` fails on Postgres at the `strftime`-based CHECK constraint — Phase 2 dialect-branching is **load-bearing, not speculative** (Charge 5 **PROVEN NECESSARY**).
- Five bonus findings: `postgresql+psycopg://` scheme requirement, guest-UPN URL-encoding, az CLI 2.86+ requirement, `0.0.0.0` public-access semantics, AAD admin propagation observed at <60 s.

---

## Setup

| Field | Value |
|---|---|
| Subscription | cmbdevoutlook333 |
| Resource group | rg-mom-bot-spike-101 |
| Server | mom-bot-spike-101 (Azure Postgres Flexible Server) |
| SKU | B1ms (Burstable) |
| Region | eastus |
| Postgres version | 16 |
| Auth mode | Azure AD only (no password fallback) |
| Admin | cmb_dev@outlook.com (guest UPN in tenant cmbdevoutlook333.onmicrosoft.com) |
| Firewall scope | Operator's specific public IP — not 0.0.0.0 |

**Teardown:** RG deleted same day (2026-05-17). No resources persist.

---

## Charge 2 — PGPASSWORD + AAD token + psycopg3

### PROVEN

#### Result

Connection succeeded. Smoke SELECT returned:

```
current_user=cmb_dev_outlook.com#EXT#@cmbdevoutlook333.onmicrosoft.com
current_database=postgres
now=2026-05-17 19:09:03.815784+00:00
```

#### Token shape

- Length: 2234 characters (JWT, three-part dot-separated).
- Resource URI used for mint: `https://ossrdbms-aad.database.windows.net`.

#### Wire format

DSN pattern:

```
postgresql://<url-encoded-user>@<host>:5432/<db>?sslmode=require
```

No password component in the DSN. The raw AAD token is carried in the `PGPASSWORD` environment variable. psycopg3 reads `PGPASSWORD` from the environment and passes it to the server as the password field over the wire; Postgres Flexible Server in AAD-only mode validates the JWT in place of a password hash.

#### Implication for plan

PR #97 § R2 was rated "Low likelihood, High impact, **unverified**". The approach is now **verified**. Remove the "unverified" qualifier; no probability data needed.

---

## Charge 3 — Token TTL data for `pool_recycle`

### DATA CAPTURED

#### Observed

Initial mint TTL: **5147 seconds ≈ 86 minutes**.

Spike run output:

```
expires : 1779048910 (2026-05-17T20:15:10+00:00 UTC)
```

Minted at 2026-05-17T19:09:23+00:00; expiry at 20:15:10 = 5147 s elapsed.

#### Plan assumption vs. reality

The plan assumed an upper bound of ~24 h. The observed ceiling is **~17× shorter** (86 min). A `pool_recycle` set to, say, 43 200 s (12 h) would use tokens for hours past expiry, causing authentication failures mid-session.

#### Mitigation

`pool_recycle` must be **≤ 4800 seconds (80 min)** to stay safely under the 86-min observed ceiling. Add this as a concrete value to the Phase 3 SQLAlchemy engine config. The 6-minute margin (80 vs. 86 min) is intentional — it allows the pool to recycle and re-mint before expiry rather than after.

#### What we did NOT run

A live pool-past-expiry test (connecting after the 86-min window without recycling). Not needed: the 86-min mint TTL is the binding constraint; conservative `pool_recycle` configuration handles it analytically. Live verification can happen organically during Phase 4 soak — any misconfiguration there will surface as a connection authentication error with a clear token-expired message.

---

## Charge 5 — Phase 2 dialect-branching is required

### PROVEN NECESSARY

#### What broke

Migration `0002_reminders_schema.py` fails when applied against Postgres. The migration includes a CHECK constraint using SQLite's `strftime` function:

```sql
CONSTRAINT ck_fire_time_no_seconds CHECK (CAST(strftime('%S', fire_time_utc) AS INTEGER) = 0)
```

Postgres error:

```
psycopg.errors.UndefinedFunction: function strftime(unknown, time without time zone) does not exist
LINE 15:  CONSTRAINT ck_fire_time_no_seconds CHECK (CAST(strftime('%S...
HINT: No function matches the given name and argument types.
```

#### Result

Migration `0001` (baseline) and `0002` (start of schema) both apply; `0002` dies at the CHECK constraint DDL before it can commit. Migration `0003` (the planned dialect-branch fix) never runs because `0002` is still broken — `0003` depends on `0002` being in a committed state.

#### Implication for plan

PR #97's Phase 2 Task 2.1 — adding `0003_postgres_check_constraint_portability.py` to rewrite the check using `EXTRACT(SECOND FROM fire_time_utc) = 0` — is **load-bearing**, not speculative. Two implementation paths exist:

1. **Rewrite inside `0002`** (recommended for fresh DBs): change the CHECK expression in `0002` to branch on dialect at migration time. Since #91 is a fresh Postgres database with no data migration from SQLite, this is the cleanest path — it prevents the broken migration from ever being in the history.
2. **Drop-and-recreate in `0003`**: leave `0002` broken as-is, add a `0003` that drops the constraint and recreates it with the Postgres-compatible expression. This path is only needed for existing SQLite databases being migrated forward — which #91 explicitly does not require (fresh Postgres, no data migration per the issue description).

**Recommendation:** take path 1. Rewrite `0002` to emit the correct CHECK expression per dialect. Path 2 carries the broken `0002` forward in history unnecessarily.

#### Test coverage gap

`tests/test_alembic.py` lines 343–376 run the migration suite only against SQLite. Without a Postgres-targeted alembic test (testcontainers or a CI service container), every future migration is at risk of the same class of failure — SQLite-specific DDL that passes the test suite but breaks on Postgres at deploy time. Phase 2 plan should include the test fixture as a first-class deliverable, not defer it to Phase 4.

---

## Bonus Findings

1. **SQLAlchemy URL scheme must be `postgresql+psycopg://`**, not bare `postgresql://`. SQLAlchemy defaults the bare `postgresql://` scheme to the psycopg2 dialect; we only have psycopg3 (`psycopg`) installed. Without the `+psycopg` suffix, SQLAlchemy raises an import error at engine-creation time (`ModuleNotFoundError: No module named 'psycopg2'`). Every location in the plan, runbook, or app code that constructs or documents a SQLAlchemy URL — alembic env, app engine config, any direct `create_engine` call — must specify `postgresql+psycopg://`.

2. **Guest-UPN URL-encoding**: when the operator identity is a guest user, the UPN contains `@` and `#` characters (e.g. `cmb_dev_outlook.com#EXT#@cmbdevoutlook333.onmicrosoft.com`). The DSN userinfo component must be `urllib.parse.quote(user, safe="")`-encoded, or psycopg3 mis-parses the `@` as the user/host delimiter and produces a malformed connection string. Production runtime is not affected — the bot connects as the UAMI `clientId` (a UUID, no special characters) — but any operator-run probe script or runbook step that constructs a DSN with the operator's own identity must encode the username.

3. **CLI version requirement**: `az postgres flexible-server create --microsoft-entra-auth` requires az CLI ≥ 2.86. The 2.84 release (still the default in many WinGet channels) does not expose the `--microsoft-entra-auth` flag — neither does `az postgres flexible-server update`. Running against 2.84 returns "unrecognized arguments", which sent us down a path chasing `--active-directory-auth` (a flag that does not exist for Postgres Flexible Server). The runbook and any CI provisioning step must pin az CLI ≥ 2.86 in their prereqs section.

4. **`--public-access 0.0.0.0` is "any Azure tenant source IP", not "my IP only"**: a common misreading of the flag. `0.0.0.0` opens the firewall to all Azure-originating traffic, not specifically to the operator's machine. For the spike we used the operator's real public IP. The production Bicep for #91 must pin specific operator IPs and/or GitHub Actions runner CIDR ranges (these are published by GitHub and change infrequently). This also directly addresses Charge 4 of PR #97.

5. **AAD admin propagation latency** observed effectively instant: the first probe attempt after a 60-second sleep (post `az postgres flexible-server ad-admin set`) succeeded on the first try. Subsequent operations with the same admin were immediate. R8 in PR #97's missing-risks list should remain on the list — the risk is real — but the observed floor is at the low end; a 60 s post-create sleep is a sufficient hedge in CI.

---

## Plan Revision Checklist for PR #97

- [ ] Reconcile against PR #95 (Charge 1 — pre-existing finding, still required)
- [ ] Move PGPASSWORD-AAD-psycopg3 spike to "verified" — cite this findings doc (Charge 2 resolution)
- [ ] Set `pool_recycle` ≤ 4800 s in Phase 3 SQLAlchemy engine config; update R2 risk table with observed 86-min TTL (Charge 3 resolution)
- [ ] Phase 2 Task 2.1: rewrite the `ck_fire_time_no_seconds` CHECK constraint inside migration `0002` using `EXTRACT(SECOND FROM fire_time_utc) = 0` for Postgres; SQLite path keeps `strftime` (Charge 5 resolution)
- [ ] Phase 2 Task 2.1: add `tests/test_alembic_postgres.py` running against a containerized Postgres (testcontainers or CI service container) — covers the dialect-branch and prevents future SQLite-isms slipping through (Charge 5 test gap)
- [ ] Phase 2/3 docs: every SQLAlchemy URL string must use `postgresql+psycopg://` scheme (bonus finding 1)
- [ ] Phase 4 runbook + CI workflow: pin az CLI ≥ 2.86 in any provisioning command (bonus finding 3)
- [ ] Phase 1 Bicep: pin firewall to specific operator IPs / GHA runner ranges, not `0.0.0.0` (bonus finding 4 — also addresses Charge 4 of PR #97)
- [ ] R8 (missing-risks list): keep the entry; tighten "30 s–5 min" to "≤ 60 s observed in spike #101" — risk stays but stop-loss narrows
- [ ] Out-of-scope but related: Dockerfile decision (PR #97 Charge 6) — spike does not inform this; resolve separately

---

## Cost

Throwaway B1ms Postgres Flexible Server ran for approximately 30 minutes on 2026-05-17, plus storage and firewall rules. Estimated total: **< $0.20 USD**. RG deleted same day; no ongoing charges.

---

## What We Did Not Do

- Did not run a live pool-past-token-expiry test (covered analytically by the 86-min TTL observation).
- Did not test password fallback — auth mode was AAD-only by design; password auth was never enabled.
- Did not test private endpoint (#93 rescope is a separate decision; the plan accepted the public-endpoint tradeoff for CAE network-immutability reasons documented in PR #97).
- Did not test Bicep provisioning — server was provisioned via az CLI because the goal was validating the auth pattern, not the production IaC shape.
