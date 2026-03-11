# DevMesh LLM Gateway

A deployable AI control plane that sits in front of inference runtimes and foundation model APIs. Provides a consistent interface, security scanning, routing, and observability for AI in production environments.

## Overview

DevMesh Gateway is **client-deployed, not a centralized SaaS**. It enables:

- **Dual API Surface**: OpenAI-compatible and native Ollama endpoints
- **Multiple Runtime Support**: Ollama, vLLM, TRT-LLM, SGLang (and cloud APIs)
- **Security Scanning**: Prompt injection detection, Unicode sanitization, guard model integration
- **Per-Client Routing**: API keys with optional endpoint pinning
- **Audit Logging**: Async SQLite/PostgreSQL request logging with optional body storage
- **Model Discovery**: Auto-discovers models across all endpoints
- **Dashboard**: React-based monitoring UI with security, audit, and catalog views

## Quick Start

### Prerequisites

- Python 3.10+
- At least one inference runtime (e.g., Ollama)

### Local Development

```bash
# Clone and setup
git clone <repo-url>
cd llm_gateway

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Run the gateway
PYTHONPATH=src uvicorn gateway.main:app --host 0.0.0.0 --port 8001
```

### With Security Guard Model

```bash
PYTHONPATH=src \
GATEWAY_DB_URL="sqlite+aiosqlite:///data/gateway.db" \
GATEWAY_GUARD_ENABLED=true \
GATEWAY_GUARD_MODEL_NAME="ibm/granite3.2-guardian:5b" \
GATEWAY_SECURITY_SCAN_ALLOWLIST_IPS='["10.0.0.65"]' \
uvicorn gateway.main:app --host 0.0.0.0 --port 8001
```

### Dashboard

```bash
cd dashboard
npm install
npx vite --host 0.0.0.0 --port 5174
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_HOST` | `127.0.0.1` | Bind address |
| `GATEWAY_PORT` | `8000` | Listen port |
| `GATEWAY_CONFIG_PATH` | `config/gateway.yaml` | Main config file |
| `GATEWAY_DB_URL` | `sqlite:///./data/gateway.db` | Database URL (SQLite or PostgreSQL) |
| `GATEWAY_DB_STORE_REQUEST_BODY` | `false` | Store request prompts in audit log |
| `GATEWAY_DB_STORE_RESPONSE_BODY` | `false` | Store response content in audit log |
| `GATEWAY_GUARD_ENABLED` | `false` | Enable guard model shadow analysis |
| `GATEWAY_GUARD_MODEL_NAME` | `ibm/granite3.2-guardian:5b` | Guard model (`ibm/granite3.2-guardian:5b`, `llama-guard3:8b`) |
| `GATEWAY_GUARD_BASE_URL` | `http://10.0.0.15:11434` | Ollama server hosting guard model |
| `GATEWAY_GUARD_TIMEOUT` | `15.0` | Guard model inference timeout (seconds) |
| `GATEWAY_SECURITY_SCAN_ALLOWLIST_IPS` | `[]` | Source IPs to skip security scanning |

### Endpoint Configuration (gateway.yaml)

```yaml
endpoints:
  - name: gpu-node
    type: ollama
    url: http://10.0.0.19:11434
    enabled: true
    timeout: 120.0
    max_retries: 3
    labels:
      tier: primary

auth:
  enabled: true
  api_keys:
    - key: "${GATEWAY_KEY_ESTIMATOR}"
      client_id: electrical-estimator
      target_endpoint: gpu-node-3060  # Pin to specific endpoint
    - key: "${GATEWAY_KEY_DISCORD}"
      client_id: discord-bot
```

## API Endpoints

### Ollama-Compatible (Native)

| Endpoint | Description |
|----------|-------------|
| `POST /api/chat` | Chat completions (streaming supported) |
| `POST /api/generate` | Text generation (streaming supported) |
| `POST /api/embeddings` | Generate embeddings |
| `GET /api/tags` | List available models |

### OpenAI-Compatible

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | Chat completions (streaming supported) |
| `POST /v1/embeddings` | Generate embeddings |

### DevMesh Management

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check with provider status |
| `GET /v1/devmesh/catalog` | Full model catalog across all endpoints |
| `GET /api/requests` | Recent audit log entries |
| `GET /api/stats` | Usage statistics (last 24h) |
| `GET /api/keys` | List API keys (database-managed) |
| `POST /api/keys` | Create new API key |
| `DELETE /api/keys/{key_id}` | Delete API key |

### Security Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/security/stats` | Guard scan statistics |
| `GET /api/security/alerts` | Recent security alerts |
| `GET /api/security/results` | Regex vs guard side-by-side verdicts |

## Security

### Layered Defense

The gateway implements multiple security layers with zero added latency to the request path:

1. **Unicode Sanitization** (sync, zero latency): Strips invisible characters, homoglyphs, and zero-width joiners that can be used to bypass content filters.

2. **Pattern Detection** (sync, minimal latency): Regex-based injection pattern scanning — detects role overrides, delimiter attacks, and known prompt injection patterns. Threat levels: none, low, medium, high, critical.

3. **Guard Model** (async, shadow mode): Runs a guard model in the background after the response is sent. Currently supports:
   - **IBM Granite Guardian 3.2** (5b) — Returns Yes/No per category with confidence. Default categories: `jailbreak`.
   - **Llama Guard 3** (1b/8b) — Returns safe/unsafe with S1-S13 category codes.

4. **IP Allowlist**: Trusted internal services can be exempted from scanning.

### Guard Model Architecture

The guard model runs asynchronously — it does not add latency to requests. Results are stored for monitoring and pattern analysis via the dashboard.

```
Request → Sanitize → Route → Respond (fast path)
                ↘ Queue → Guard Model → Log Result (background)
```

## Dashboard

React + TypeScript dashboard served on port 5174. Features:

- **Stats Overview**: Request counts, success rates, latency, token usage
- **Security Monitor**: Alerts, guard scan results, regex vs guard comparison
- **Audit Log**: Recent requests with model, endpoint, latency, tokens
- **Model Catalog**: All discovered models across endpoints with sizes
- **API Key Management**: Create/delete database-managed API keys
- **Endpoint Health**: Per-endpoint status and model counts

## Architecture

```
Client Applications (Open WebUI, Discord Bot, etc.)
        |
        v
DevMesh Gateway (FastAPI)
├── Auth (API key + per-client routing)
├── Security (sanitize → pattern scan → async guard)
├── Dispatch (model resolution → provider selection)
├── Audit Logger (async SQLite/PostgreSQL)
└── Model Discovery (periodic catalog refresh)
        |
        v
Provider Adapters
├── Ollama (chat, generate, embeddings, streaming, vision)
├── OpenAI (chat, embeddings, streaming)
├── vLLM
├── TRT-LLM
└── SGLang
```

## Project Structure

```
llm_gateway/
├── src/gateway/
│   ├── catalog/         # Model discovery and catalog
│   ├── dispatch/        # Routing, registry, provider selection
│   ├── models/          # Data models (internal, OpenAI, Ollama)
│   ├── observability/   # Structured logging
│   ├── policy/          # Rate limiting, enforcement
│   ├── providers/       # Provider adapters (Ollama, OpenAI, vLLM, etc.)
│   ├── routes/          # API endpoints (Ollama, OpenAI, DevMesh)
│   ├── security/        # Injection defense (sanitizer, detector, guard)
│   ├── storage/         # Audit logging, API key management, DB engine
│   ├── config.py        # YAML configuration loading
│   ├── settings.py      # Pydantic settings (env vars)
│   └── main.py          # FastAPI application
├── dashboard/           # React + Vite dashboard
├── config/
│   └── gateway.yaml     # Endpoint, auth, and routing config
├── data/                # SQLite database (auto-created)
└── tests/
```

## Testing

```bash
pytest tests/ -v
pytest tests/ --cov=gateway --cov-report=html
```

## License

MIT License - see LICENSE file for details.
