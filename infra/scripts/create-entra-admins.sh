#!/usr/bin/env bash
# Idempotently create the two Entra admins on the mom-bot Postgres server.
# Replaces the race-prone administrators resources that used to live in
# infra/modules/postgres.bicep. See issue #106.
#
# Required env vars:
#   RESOURCE_GROUP                       (default: mom-bot)
#   POSTGRES_SERVER_NAME                 (default: pg-mombot-flkrgslirk53q — match main.bicepparam)
#   UAMI_OBJECT_ID, UAMI_DISPLAY_NAME    (the mi-mom-bot runtime identity)
#   GHA_SP_OBJECT_ID, GHA_SP_DISPLAY_NAME (the mom-bot-gha federated SP)
#
# Caller (operator or deploy.yml) is responsible for setting these from
# repo variables / Bicep outputs.

set -euo pipefail

: "${RESOURCE_GROUP:=mom-bot}"
: "${POSTGRES_SERVER_NAME:?required}"
: "${UAMI_OBJECT_ID:?required}"
: "${UAMI_DISPLAY_NAME:?required}"
: "${GHA_SP_OBJECT_ID:?required}"
: "${GHA_SP_DISPLAY_NAME:?required}"

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

ensure_admin "$UAMI_OBJECT_ID"   "$UAMI_DISPLAY_NAME"
ensure_admin "$GHA_SP_OBJECT_ID" "$GHA_SP_DISPLAY_NAME"

echo "Both Entra admins present on $POSTGRES_SERVER_NAME."
