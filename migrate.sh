#!/bin/sh
# migrate.sh — migration entrypoint for the Container Apps Job.
#
# This script is the ENTRYPOINT override (via `command:` in the job template)
# when the bot image runs as a migrations job rather than the Discord bot.
# It must NOT start the Discord bot — it only acquires an Entra token,
# constructs the Postgres URL, and runs alembic upgrade head.
#
# Environment variables expected (set in infra/modules/migrations-job.bicep):
#   AZURE_CLIENT_ID   — mi-mom-bot client ID for ManagedIdentityCredential
#   PGHOST            — Postgres Flexible Server FQDN
#   PGDATABASE        — database name (default: mom_bot)
#
# The TOKEN_URL endpoint is the Azure Instance Metadata Service (IMDS) for
# managed identity token acquisition.  Only available inside Azure Container
# Apps / Container Instances.

set -eu

: "${AZURE_CLIENT_ID:?AZURE_CLIENT_ID must be set}"
: "${PGHOST:?PGHOST must be set}"
: "${PGDATABASE:=mom_bot}"

echo "[migrate] acquiring Entra access token for Postgres..."

# Acquire a token via IMDS.  Retry up to 5 times with exponential back-off
# to handle cold-start IMDS latency.
attempt=0
while [ "$attempt" -lt 5 ]; do
    TOKEN=$(curl -sf \
        -H "Metadata: true" \
        "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fossrdbms-aad.database.windows.net&client_id=${AZURE_CLIENT_ID}" \
        | /app/.venv/bin/python -c "import sys, json; print(json.load(sys.stdin)['access_token'])")
    if [ -n "$TOKEN" ]; then
        break
    fi
    attempt=$((attempt + 1))
    echo "[migrate] IMDS token attempt $attempt failed — retrying in ${attempt}s..."
    sleep "$attempt"
done

if [ -z "$TOKEN" ]; then
    echo "[migrate] ERROR: failed to acquire Entra token after 5 attempts"
    exit 1
fi
echo "[migrate] token acquired (length=${#TOKEN})"

# Build the Postgres URL.  The username is the MI display name (mi-mom-bot).
# PGPASSWORD is consumed by psycopg/psql and is NOT echoed to stdout.
export MOM_BOT_DATABASE_URL="postgresql+psycopg://mi-mom-bot@${PGHOST}:5432/${PGDATABASE}?sslmode=require"
export PGPASSWORD="$TOKEN"

echo "[migrate] running alembic upgrade head against ${PGHOST}/${PGDATABASE}"
exec /app/.venv/bin/alembic upgrade head
