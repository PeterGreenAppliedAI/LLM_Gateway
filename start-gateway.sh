#!/usr/bin/env bash
# DevMesh LLM Gateway - Startup Script

cd "$(dirname "$0")" || exit 1

# Load secrets from gitignored .env (API keys, etc.)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# config/gateway.yaml references these without fallbacks. A missing var is
# not fatal: the gateway boots and disables just that API key (with a
# warning in the gateway log). Warn here too so it's visible at launch.
for var in GATEWAY_KEY_ESTIMATOR GATEWAY_KEY_DISCORD GATEWAY_KEY_DEV; do
  if [ -z "${!var}" ]; then
    echo "WARNING: $var is not set — that API key will be disabled (see .env.example)." >&2
  fi
done

# Database
export GATEWAY_DB_URL="sqlite+aiosqlite:///data/gateway.db"
export GATEWAY_DB_STORE_REQUEST_BODY=true
export GATEWAY_DB_STORE_RESPONSE_BODY=true

# Guard model (shadow security analysis)
export GATEWAY_GUARD_ENABLED=false
export GATEWAY_GUARD_BASE_URL="http://10.0.0.15:11434"
export GATEWAY_GUARD_MODEL_NAME="ibm/granite3.2-guardian:5b"

# PII detection
export GATEWAY_PII_ENABLED=true
export GATEWAY_PII_SCRUB_ENABLED=false

export PYTHONPATH=src

exec venv/bin/python3 -m uvicorn gateway.main:app --host 0.0.0.0 --port 8001
