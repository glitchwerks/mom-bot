# Postgres Role-Ownership Cutover — `mom-bot-gha` → `mi-mom-bot`

**Issue:** [#261](https://github.com/glitchwerks/rsl-mom-bot/issues/261) (blocks [#103](https://github.com/glitchwerks/rsl-mom-bot/issues/103)) · refs #259, #256, #255
**Date:** 2026-05-29
**Status:** Advisory / design only. **No prod mutations performed in producing this doc.** Execution is a separate, operator-run step gated on review of this plan.
**Server:** Azure Database for PostgreSQL **Flexible Server** `pg-mombot-flkrgslirk53q`, database `mom_bot`, RG `mom-bot`, PG version **16** (per spike #101 setup, [docs/spike/2026-05-17-postgres-aad-findings.md:L27](../../spike/2026-05-17-postgres-aad-findings.md)).

---

## 1. Context

The UAMI Container Apps Job `job-mom-bot-migrate` now runs `alembic upgrade head` as the managed identity `mi-mom-bot` and is proven end-to-end in prod (execution `8fkwf6c` Succeeded — issue [#261](https://github.com/glitchwerks/rsl-mom-bot/issues/261) body). The blast-radius-reduction goal of #103 is to revoke the `mom-bot-gha` service-principal's Postgres Entra-admin grant, since migrations no longer run as that SP.

The revoke (`az postgres flexible-server microsoft-entra-admin delete … --object-id <mom-bot-gha>`) failed:

```
Code: AadAuthPrincipalDropFailed
2BP01: role "mom-bot-gha" cannot be dropped because some objects depend on it
```

**Root cause.** Removing a Flexible-Server Entra admin drops the underlying Postgres role. Migrations historically ran as `mom-bot-gha`, so that role **owns** the alembic-created objects (tables, sequences, indexes, the alembic version table). PostgreSQL refuses `DROP ROLE` while the role owns objects (`2BP01` = `dependent_objects_still_exist`). This is standard PostgreSQL behavior, confirmed by the Azure limitations doc: *"If you delete a Microsoft Entra principal … Database administrators need to transfer ownership and drop roles manually."* — [Microsoft Entra authentication with Azure Database for PostgreSQL § Limitations and considerations](https://learn.microsoft.com/azure/postgresql/security/security-entra-concepts#limitations-and-considerations) (fetched 2026-05-29).

This surfaces **two** problems, both of which this cutover resolves:

1. **Blocker** — the grant can't be revoked until owned objects are reassigned.
2. **Forward risk** — `mi-mom-bot` does not own the legacy objects, so a *future* migration that `ALTER`s or `DROP`s a `mom-bot-gha`-owned table could fail on a privilege error. Today's run only succeeded because it applied no DDL against legacy-owned objects. **Ownership reassignment is therefore required for the cutover to be genuinely complete, not merely for cleanup.**

---

## 2. Privilege-model finding (Q1)

### The vanilla-PostgreSQL rule

In stock PostgreSQL, `REASSIGN OWNED` *"requires membership on both the source role(s) and the target role"* — [PostgreSQL 16 docs, `REASSIGN OWNED`](https://www.postgresql.org/docs/current/sql-reassign-owned.html) (fetched 2026-05-29). The only other way is to be superuser.

On Flexible Server **there is no usable superuser**. The PostgreSQL `SUPERUSER` attribute belongs to `azure_su` / `azuresu`, *"which belongs to the managed service. You don't have access to this role"* — [Server concepts for Azure Database for PostgreSQL § Manage your server](https://learn.microsoft.com/azure/postgresql/configure-maintain/concepts-servers#manage-your-server) (fetched 2026-05-29). The highest principal you can act as is a member of `azure_pg_admin`, which *"doesn't have full superuser permissions"* (same source). So the naive expectation is: to run `REASSIGN OWNED BY "mom-bot-gha" TO "mi-mom-bot"` you would first have to `GRANT "mom-bot-gha" TO <admin>;` and `GRANT "mi-mom-bot" TO <admin>;`.

### The Flexible-Server enhancement — this is the load-bearing finding

Azure adds a behavior that removes the need for that explicit double-GRANT. From [Access management for Azure Database for PostgreSQL § Role management → "Improved control for *azure_pg_admin*"](https://learn.microsoft.com/azure/postgresql/security/security-access-control#role-management) (fetched 2026-05-29):

> "To improve administrative flexibility and address a limitation introduced in PostgreSQL 16, Azure Database for PostgreSQL enhances the capabilities of the *azure_pg_admin* role across all PostgreSQL versions. With this update, **members of the *azure_pg_admin* role can manage roles and access objects owned by any nonrestricted role, even if those roles are also members of *azure_pg_admin***. This … provid[es] a seamless and reliable experience **without requiring superuser access**."

Every Flexible-Server Entra admin is a member of `azure_pg_admin` ([Microsoft Entra authentication … § Differences between a PostgreSQL administrator and a Microsoft Entra administrator](https://learn.microsoft.com/azure/postgresql/security/security-entra-concepts) — Entra admins "Get the same privileges as the original PostgreSQL administrator", which "belongs to the role azure_pg_admin"; fetched 2026-05-29). `mom-bot-gha` and `mi-mom-bot` are both Entra-admin SPs — i.e. **nonrestricted roles that are also members of `azure_pg_admin`**, exactly the case the enhancement names.

### Answer to Q1 (concrete, working approach)

> Connect as an existing Entra admin (e.g. `cmb_dev`). Because every Entra admin is a member of `azure_pg_admin`, and Azure's Flexible-Server enhancement lets `azure_pg_admin` members manage objects owned by any nonrestricted role, the admin can run `REASSIGN OWNED BY "mom-bot-gha" TO "mi-mom-bot";` directly — no superuser, and (on Flexible Server) no prerequisite `GRANT "mom-bot-gha"/"mi-mom-bot" TO <admin>` is required.

**Defensive fallback (belt-and-suspenders).** The enhancement is documented as "across all PostgreSQL versions" but is an Azure-specific behavior; if the `REASSIGN` unexpectedly returns `42501 permission denied` (e.g. on an older server image that predates the enhancement), fall back to the vanilla path *in the same session* before retrying:

```sql
GRANT "mom-bot-gha" TO current_user;
GRANT "mi-mom-bot"  TO current_user;
-- retry REASSIGN OWNED, then optionally REVOKE the two grants back off current_user
```

This GRANT is itself permitted because `current_user` is in `azure_pg_admin` and both target roles are nonrestricted. It is **not** expected to be necessary; it is documented so the operator is not blocked if the enhancement is absent.

### Flexible-Server caveats that bound this work

- **`REASSIGN OWNED` is per-database and per-context.** It *"Runs in the current database context. Run it in each database where you must transfer ownership"* — [Transfer Postgres object ownership](https://learn.microsoft.com/azure/databricks/oltp/projects/transfer-object-ownership#transfer-ownership-of-multiple-objects) (fetched 2026-05-29). Run it while connected to **`mom_bot`** (the only DB the bot uses). It does **not** need running against `postgres` unless `mom-bot-gha` created objects there (the enumeration step below confirms).
- **`REASSIGN OWNED` reassigns ownership only.** It *"does not change existing GRANT permissions or default privileges"* (same source). It re-points the `pg_class.relowner` etc., it does not touch table ACLs. This is exactly why the app is unaffected (see §5).
- **The `public` schema is owned by `azure_pg_admin`, not by any app role**, on all Flexible-Server PG versions — [Access management § Public schema ownership changes](https://learn.microsoft.com/azure/postgresql/security/security-access-control#role-management) (fetched 2026-05-29). So `mom-bot-gha` does **not** own the schema; only the objects it created inside it. The enumeration step will reflect this.
- **`DROP ROLE` is performed by Azure, not by you.** You never run `DROP ROLE "mom-bot-gha"`. The `az … microsoft-entra-admin delete` control-plane call drops the role internally; it succeeds only once the role owns nothing. Your job is to get the owned-object count to zero, then re-issue the `az` delete.

---

## 3. Forward-fix recommendation (Q2)

**Recommendation: belt-and-suspenders — primary fix is `REASSIGN OWNED` (already in the runbook below); the durable forward fix is to stop creating per-SP ownership divergence by making `mi-mom-bot` the consistent migration owner, and verify it with a Postgres-targeted DDL probe in CI.** Concretely:

1. **`mi-mom-bot` becomes and stays the object owner (primary, no code change).** After the reassignment in §4, `mi-mom-bot` owns all existing objects, and `migrate.sh` already runs all future migrations as `mi-mom-bot` ([migrate.sh:L40](../../../migrate.sh) sets the connection user to `mi-mom-bot`). New objects created by a migration are owned by the connected role by default, so going forward ownership stays on `mi-mom-bot` automatically. **No `env.py` change is required for correctness** — the single-identity migration path (#255) already guarantees owner-consistency once the legacy objects are reassigned. This is the cheapest durable fix and it is the one I recommend adopting as the baseline.

2. **Do *not* reach for `ALTER DEFAULT PRIVILEGES` here.** `ALTER DEFAULT PRIVILEGES` governs *grants* on future objects, not *ownership*. It would not have prevented this incident (the problem was ownership, not ACLs) and adds a moving part. Skip it.

3. **Optional hardening — shared group-owner role.** If you anticipate *more than one* identity ever creating schema objects (e.g. a future second migration identity, or operator-run DDL during incidents), introduce a `NOLOGIN` group role (e.g. `mom_bot_owners`), grant it to every identity that runs DDL, and `SET ROLE mom_bot_owners` at the top of each migration so objects are owned by the group, not the individual login role. This is the pattern the Azure ownership-transfer doc itself models with `temp_table_owners` ([Transfer Postgres object ownership](https://learn.microsoft.com/azure/databricks/oltp/projects/transfer-object-ownership#transfer-ownership-of-multiple-objects), fetched 2026-05-29). **For mom-bot's current single-identity reality this is over-engineering** — recommend deferring it to a follow-up issue and only adopting it if a second DDL identity actually appears.

4. **Close the recurrence-detection gap (the real forward investment).** Spike #101 already flagged that the alembic test suite runs only against SQLite ([docs/spike/2026-05-17-postgres-aad-findings.md:L136](../../spike/2026-05-17-postgres-aad-findings.md)). A migration that fails *specifically* on a privilege/ownership boundary would not be caught by a SQLite test. The durable forward fix is a Postgres-targeted alembic test (testcontainers / CI service container) that runs `upgrade head` then a `downgrade`/`ALTER` probe as a non-owner-then-owner. **Where it belongs:** a CI test fixture, tracked as a follow-up issue — not in `env.py`.

**Net Q2 answer:** the reassignment makes `mi-mom-bot` the owner; the single-identity migration path keeps it the owner with no code change; the worthwhile forward investment is a Postgres CI probe, not `ALTER DEFAULT PRIVILEGES` and not (yet) a group role.

---

## 4. Ordered runbook (copy-paste-ready)

> **Pre-flight identity note.** Connect as `cmb_dev` (the human Entra admin). The guest-UPN username must be URL-encoded in the DSN — `cmb_dev` resolves to something like `cmb_dev_outlook.com#EXT#@cmbdevoutlook333.onmicrosoft.com` ([docs/spike/2026-05-17-postgres-aad-findings.md:L144](../../spike/2026-05-17-postgres-aad-findings.md)). For interactive `psql` (below) you pass the username as a plain `user=` field (no URL parse), so encoding is not needed there; it is only needed if you build a SQLAlchemy/libpq *URL*. Use `postgresql+psycopg://` scheme if you script this via SQLAlchemy (bonus finding 1, same spike doc).
>
> **Token resource URI:** `https://ossrdbms-aad.database.windows.net` (spike #101). Token TTL is ~86 min (empirically observed, not documented) — do the whole sequence inside one token window.

Set shell variables (bash):

```bash
RG=mom-bot
PG_SERVER=pg-mombot-flkrgslirk53q
PG_FQDN="${PG_SERVER}.postgres.database.azure.com"
PG_DB=mom_bot
ADMIN_USER='cmb_dev@outlook.com'   # the human Entra admin display/login name

# mom-bot-gha object-id from the issue body (verify before destructive step — see 4d):
GHA_OID=6fcf4d62-e6da-4819-9667-234a55018fa2
```

Open a firewall rule for your workstation IP (per runbook "Dev-laptop ad-hoc Postgres access"; this is the documented mechanism — `operatorIpAddress` was removed from Bicep in #166):

```bash
MYIP=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create \
  --resource-group "$RG" --name "$PG_SERVER" \
  --rule-name "dev-cmb-261-cutover" \
  --start-ip-address "$MYIP" --end-ip-address "$MYIP"
# effective within ~30s
```

Acquire an Entra token and connect as the admin:

```bash
export PGPASSWORD=$(az account get-access-token \
  --resource-type oss-rdbms \
  --query accessToken -o tsv)

# connect (plain user= field; no URL-encoding needed for interactive psql)
psql "host=${PG_FQDN} port=5432 dbname=${PG_DB} user=${ADMIN_USER} sslmode=require"
```

### 4a — Enumerate objects owned by `mom-bot-gha` (confirm scope first)

Run all of the following inside the `psql` session, connected to `mom_bot`. **Capture the output** — it is your before-state evidence and your rollback reference.

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

-- Single-number guard: total owned-object count (drives the verify in 4c)
SELECT
  (SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE r.rolname='mom-bot-gha')
  +
  (SELECT count(*) FROM pg_proc  p JOIN pg_roles r ON r.oid=p.proowner WHERE r.rolname='mom-bot-gha')
  +
  (SELECT count(*) FROM pg_type  t JOIN pg_roles r ON r.oid=t.typowner WHERE r.rolname='mom-bot-gha' AND t.typtype != 'c')
  -- typtype != 'c' excludes composite types already counted in pg_class above
  AS gha_owned_count;
```

`\dt`-style quick look for the human eye:

```
\dt *.*
\dn
```

> **Decision gate.** If `gha_owned_count` is non-zero (expected — that is the whole problem), proceed to 4b. If it is unexpectedly **zero**, stop: the drop should already succeed, so the real failure is elsewhere (e.g. wrong object-id, or `mom-bot-gha` owns objects in *another* database — re-run 4a connected to `postgres`).

### 4b — Reassign ownership

```sql
BEGIN;
REASSIGN OWNED BY "mom-bot-gha" TO "mi-mom-bot";
-- (Do NOT add DROP OWNED — that would drop privileges/grants, not just ownership.)
COMMIT;
```

> If this returns `42501 permission denied`, the Azure `azure_pg_admin` enhancement is not active on this server image. Run the §2 fallback GRANTs in the same session, then re-run the `BEGIN…COMMIT` block. Do not proceed past a failed reassignment.

### 4c — Verify ownership transferred (count must be zero)

```sql
SELECT
  (SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE r.rolname='mom-bot-gha')
  +
  (SELECT count(*) FROM pg_proc  p JOIN pg_roles r ON r.oid=p.proowner WHERE r.rolname='mom-bot-gha')
  +
  (SELECT count(*) FROM pg_type  t JOIN pg_roles r ON r.oid=t.typowner WHERE r.rolname='mom-bot-gha' AND t.typtype != 'c')
  -- typtype != 'c' excludes composite types already counted in pg_class above
  AS gha_owned_count_after;   -- MUST be 0

-- Confirm the objects are now mi-mom-bot's (spot-check the alembic version table + a real table)
SELECT n.nspname, c.relname, pg_get_userbyid(c.relowner) AS owner
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relname IN ('alembic_version','reminders')  -- adjust to real table names from 4a
ORDER BY 1,2;
```

`gha_owned_count_after` **must be 0** before continuing. If it is not zero, re-run 4a to see what remains (likely a different database context) and repeat 4b there. Exit `psql` (`\q`).

### 4d — Verify the principal/target is still correct, then retry the Entra-admin revoke

First confirm the object-id actually belongs to `mom-bot-gha` (guard against revoking the wrong principal):

```bash
# Resolve mom-bot-gha's object-id dynamically from Entra
GHA_OID=$(az ad sp list --display-name mom-bot-gha --query "[0].id" -o tsv)
echo "Resolved mom-bot-gha object-id: $GHA_OID"
# Operator: confirm this equals 6fcf4d62-e6da-4819-9667-234a55018fa2 before proceeding

# List current admins (before)
az postgres flexible-server microsoft-entra-admin list \
  -g "$RG" --server-name "$PG_SERVER" -o table
```

Then revoke:

```bash
az postgres flexible-server microsoft-entra-admin delete \
  -g "$RG" --server-name "$PG_SERVER" \
  --object-id "$GHA_OID" --yes
```

### 4e — Verify the final admin list

```bash
az postgres flexible-server microsoft-entra-admin list \
  -g "$RG" --server-name "$PG_SERVER" -o table
# Expected remaining admins: mi-mom-bot (SP), cmb_dev (User). mom-bot-gha GONE.
```

Optionally prove the forward-fix acceptance criterion (a schema-changing migration works as `mi-mom-bot`): run the migration job and confirm Succeeded —

```bash
az containerapp job start --name job-mom-bot-migrate --resource-group "$RG"
```

Close the firewall rule:

```bash
az postgres flexible-server firewall-rule delete \
  --resource-group "$RG" --name "$PG_SERVER" \
  --rule-name "dev-cmb-261-cutover" --yes
```

### 4f — Document in the runbook

Add the reassignment + revocation sequence to `infra/aad-runbook.md` Step 5.5 (this is an acceptance criterion of #261). The runbook already carries the optional `mom-bot-gha` revoke snippet under `infra/aad-runbook.md § "Cutover completion: reassign ownership before revoking mom-bot-gha"`; extend it with the §4a–4c reassignment prerequisite so the revoke is not attempted before reassignment again.

---

## 5. Risks & rollback

### Does reassignment break the running bot? — **No.** (Q4)

The runtime Container App `ca-mom-bot` connects as `mi-mom-bot` (same UAMI as the migration job). After reassignment, `mi-mom-bot` *owns* the objects it was already reading and writing — strictly more privilege than before, never less. Critically, `REASSIGN OWNED` **changes ownership only and does not alter existing GRANTs or default privileges** ([Transfer Postgres object ownership](https://learn.microsoft.com/azure/databricks/oltp/projects/transfer-object-ownership#transfer-ownership-of-multiple-objects), fetched 2026-05-29). So:

- The app's table-level `SELECT/INSERT/UPDATE/DELETE` grants are untouched — read/write access is unaffected.
- There is no exclusive-lock outage of consequence: `REASSIGN OWNED` takes brief catalog locks per object; for a handful of small tables this is sub-second. The bot is a single-replica, low-QPS Discord bot — a momentary catalog lock is not user-visible. Run during low activity regardless.
- `mom-bot-gha` losing ownership does **not** affect the app, because the app never connected as `mom-bot-gha`.

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Reassignment runs against the wrong DB context and misses objects in `postgres` | Low | Medium | 4a/4c count guards; re-run connected to `postgres` if count ≠ 0 |
| `azure_pg_admin` enhancement absent on this server image → `REASSIGN` denied | Low | Low | §2 fallback double-GRANT in same session |
| Revoking the wrong object-id (typo / stale id) | Low | High | 4d resolves `mom-bot-gha` id from Entra and asserts it matches before delete |
| Token expires mid-sequence (86-min TTL) | Low | Low | Re-mint token, reconnect; steps are idempotent |
| Firewall rule left open after session | Med | Med | Explicit delete in 4e; runbook "verify rule is gone" check |
| App write outage during catalog lock | Very Low | Low | Sub-second locks; run during low activity |

### Rollback / abort guidance per step

- **Abort before 4b (after 4a):** nothing was changed. Close firewall, `\q`. No rollback needed.
- **4b fails (permission/other):** the `BEGIN…COMMIT` wraps it — a failed statement rolls back the transaction; ownership is unchanged. Diagnose (most likely the enhancement-absent case → fallback GRANTs), retry. Do not proceed to 4d.
- **After 4b commits, before 4d:** ownership is now on `mi-mom-bot`. This is a *safe terminal state on its own* — the app keeps working and you have simply completed the forward-fix without yet revoking the admin. If you want to *undo* it (you should not need to), the inverse is `REASSIGN OWNED BY "mi-mom-bot" TO "mom-bot-gha";` **but** beware this re-reassigns *all* `mi-mom-bot` objects including ones it legitimately owned pre-cutover — so the clean rollback is "leave ownership on `mi-mom-bot` and stop", not a blanket reverse-reassign. Do not blanket-reverse.
- **4d `az delete` fails:** the most common cause is the same `2BP01` — meaning 4c was not actually zero (wrong DB context). Re-check 4a/4c. The admin list is unchanged on failure (the original failed delete "made no modification" per the issue body), so retrying is safe.
- **Anything unexpected:** stop, capture the `psql`/`az` output, leave ownership wherever it landed (both `mom-bot-gha`-owned and `mi-mom-bot`-owned are non-broken states for the running bot), and escalate. The bot's runtime is unaffected by ownership location.

---

## Citations summary

| Claim | Source | Fetched |
|---|---|---|
| `azure_pg_admin` is pseudo-superuser, no true superuser access | [Server concepts § Manage your server](https://learn.microsoft.com/azure/postgresql/configure-maintain/concepts-servers#manage-your-server) | 2026-05-29 |
| `azure_pg_admin` members can manage objects of any nonrestricted role w/o superuser (the enabling enhancement) | [Access management § Role management](https://learn.microsoft.com/azure/postgresql/security/security-access-control#role-management) | 2026-05-29 |
| Entra admins get same privileges as PG admin (member of azure_pg_admin) | [Entra auth § Differences …](https://learn.microsoft.com/azure/postgresql/security/security-entra-concepts) | 2026-05-29 |
| Deleting Entra principal requires manual ownership transfer + drop | [Entra auth § Limitations and considerations](https://learn.microsoft.com/azure/postgresql/security/security-entra-concepts#limitations-and-considerations) | 2026-05-29 |
| Vanilla PG: REASSIGN OWNED requires membership in both source and target | [PostgreSQL 16 REASSIGN OWNED](https://www.postgresql.org/docs/current/sql-reassign-owned.html) | 2026-05-29 |
| REASSIGN OWNED is per-DB context; reassigns ownership only, not grants/default privileges | [Transfer Postgres object ownership](https://learn.microsoft.com/azure/databricks/oltp/projects/transfer-object-ownership#transfer-ownership-of-multiple-objects) | 2026-05-29 |
| public schema owned by azure_pg_admin on all PG versions (Flexible Server) | [Access management § Public schema ownership changes](https://learn.microsoft.com/azure/postgresql/security/security-access-control#role-management) | 2026-05-29 |
| Token resource URI, guest-UPN URL-encoding, `postgresql+psycopg://` scheme | [docs/spike/2026-05-17-postgres-aad-findings.md](../../spike/2026-05-17-postgres-aad-findings.md) | repo |
| ~86-min token TTL | empirically observed, not documented | — |
| migrate.sh connects as `mi-mom-bot` | [migrate.sh:L40](../../../migrate.sh) | repo |
| `operatorIpAddress` removed from Bicep; ad-hoc firewall is the documented path | [infra/aad-runbook.md](../../../infra/aad-runbook.md) "Dev-laptop ad-hoc Postgres access" | repo |
