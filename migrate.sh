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
# Token acquisition uses azure-identity (already a runtime dependency) via
# python -m mom_bot.migrations.acquire_token, which replaces the former
# curl-based IMDS call.  The python:3.12-slim base image does not ship curl,
# which caused the job to fail with "curl: not found" (issue #259).

set -eu

: "${AZURE_CLIENT_ID:?AZURE_CLIENT_ID must be set}"
: "${PGHOST:?PGHOST must be set}"
: "${PGDATABASE:=mom_bot}"

echo "[migrate] acquiring Entra access token for Postgres..."

# Acquire a token via azure-identity ManagedIdentityCredential.
# azure-identity handles IMDS retries and exponential back-off internally.
TOKEN=$(/app/.venv/bin/python -m mom_bot.migrations.acquire_token)

if [ -z "$TOKEN" ]; then
    echo "[migrate] ERROR: failed to acquire Entra token"
    exit 1
fi

echo "[migrate] token acquired (length=${#TOKEN})"

# Build the Postgres URL.  The username is the MI display name (mi-mom-bot).
# PGPASSWORD is consumed by psycopg/psql and is NOT echoed to stdout.
export MOM_BOT_DATABASE_URL="postgresql+psycopg://mi-mom-bot@${PGHOST}:5432/${PGDATABASE}?sslmode=require"
export PGPASSWORD="$TOKEN"

echo "[migrate] running alembic upgrade head against ${PGHOST}/${PGDATABASE}"
exec /app/.venv/bin/alembic upgrade head
