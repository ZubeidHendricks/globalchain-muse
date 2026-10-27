#!/bin/sh
# Started by `shopify app dev`, which sets PORT and APP_URL/HOST (its tunnel or localhost URL).
cd "$(dirname "$0")/.." || exit 1
base="${SHOPIFY_APP_URL:-${APP_URL:-$HOST}}"
[ -n "$base" ] || { echo "shopify-dev.sh: no APP_URL from Shopify CLI" >&2; exit 1; }
PUBLIC_BASE_URL="$base" exec .venv/bin/uvicorn globalchain.api:app --host 127.0.0.1 --port "${PORT:-8000}"
