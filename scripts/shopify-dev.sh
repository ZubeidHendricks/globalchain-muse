#!/bin/sh
# Started by `shopify app dev`, which sets PORT and SHOPIFY_APP_URL (its tunnel or localhost URL).
cd "$(dirname "$0")/.." || exit 1
PUBLIC_BASE_URL="$SHOPIFY_APP_URL" exec .venv/bin/uvicorn globalchain.api:app --host 127.0.0.1 --port "${PORT:-8000}"
