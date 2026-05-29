#!/usr/bin/env bash
# Idempotently register mi-mom-bot as the Entra admin on the mom-bot Postgres
# server (issue #255: mom-bot-gha Postgres admin grant removed).
#
# Migrations now run via the Container Apps Job 'job-mom-bot-migrate' under
# mi-mom-bot (UAMI), which is already registered as Entra admin here.
# mom-bot-gha no longer needs Postgres admin authority — it cannot connect
# to the database directly from GHA runners (CAE egress-only firewall rule).
#
# Required env vars:
#   RESOURCE_GROUP      (default: mom-bot)
#   POSTGRES_SERVER_NAME
#   UAMI_OBJECT_ID      (mi-mom-bot principal ID)
#   UAMI_DISPLAY_NAME   (mi-mom-bot)
#
# Caller (operator or deploy.yml) is responsible for setting these from
# repo variables / Bicep outputs.

set -euo pipefail

: "${RESOURCE_GROUP:=mom-bot}"
: "${POSTGRES_SERVER_NAME:?required}"
: "${UAMI_OBJECT_ID:?required}"
: "${UAMI_DISPLAY_NAME:?required}"

# helper: idempotent admin-create. existence check first, then create if absent.
ensure_admin () {
  local object_id="$1" display_name="$2"
  local existing
  existing=$(az postgres flexible-server microsoft-entra-admin list \
    -g "$RESOURCE_GROUP" --server-name "$POSTGRES_SERVER_NAME" \
    --query "[?objectId=='$object_id'] | length(@)" -o tsv)
  if [ "$existing" -gt 0 ]; then
    echo "Entra admin $display_name ($object_id) already exists — skipping."
    return 0
  fi
  echo "Creating Entra admin $display_name ($object_id)..."
  az postgres flexible-server microsoft-entra-admin create \
    -g "$RESOURCE_GROUP" \
    --server-name "$POSTGRES_SERVER_NAME" \
    --object-id "$object_id" \
    --display-name "$display_name" \
    --type ServicePrincipal
}

ensure_admin "$UAMI_OBJECT_ID" "$UAMI_DISPLAY_NAME"

echo "Entra admin present on $POSTGRES_SERVER_NAME."
