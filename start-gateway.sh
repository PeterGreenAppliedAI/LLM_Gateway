#!/usr/bin/env bash
# DevMesh LLM Gateway - Startup Script

# Database
export GATEWAY_DB_URL="sqlite+aiosqlite:///data/gateway.db"
export GATEWAY_DB_STORE_REQUEST_BODY=true
export GATEWAY_DB_STORE_RESPONSE_BODY=true

# Guard model (shadow security analysis)
export GATEWAY_GUARD_ENABLED=true
export GATEWAY_GUARD_BASE_URL="http://10.0.0.15:11434"
export GATEWAY_GUARD_MODEL_NAME="ibm/granite3.2-guardian:5b"

# PII detection
export GATEWAY_PII_ENABLED=true
export GATEWAY_PII_SCRUB_ENABLED=false

export PYTHONPATH=src

cd "$(dirname "$0")" || exit 1

exec venv/bin/python3 -m uvicorn gateway.main:app --host 0.0.0.0 --port 8001
