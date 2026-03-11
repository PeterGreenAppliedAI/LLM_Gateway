# DevMesh LLM Gateway

A deployable AI control plane that sits in front of inference runtimes and foundation model APIs. Provides a consistent interface, security scanning, routing, and observability for AI in production environments.

## Overview

DevMesh Gateway is **client-deployed, not a centralized SaaS**. It enables:

- **Dual API Surface**: OpenAI-compatible and native Ollama endpoints
- **Multiple Runtime Support**: Ollama, vLLM, TRT-LLM, SGLang (and cloud APIs)
- **Security Scanning**: Prompt injection detection, Unicode sanitization, guard model integration
- **PII Detection & Scrubbing**: Flags personally identifiable information with optional per-route scrubbing
- **Per-Client Routing**: API keys with optional endpoint pinning and model/endpoint allowlists
- **Policy Enforcement**: Configurable rate limits, per-key rate overrides, model and endpoint restrictions
- **Audit Logging**: Async SQLite/PostgreSQL request logging with optional body storage
- **Model Discovery**: Auto-discovers models across all endpoints
- **Dashboard**: React-based monitoring UI with security, audit, and catalog views

## Quick Start

### Prerequisites

- Python 3.10+
- At least one inference runtime (e.g., [Ollama](https://ollama.com))

### Local Development

```bash
# Clone and setup
git clone https://github.com/PeterGreenAppliedAI/LLM_Gateway.git
cd LLM_Gateway

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Copy and edit configuration
cp config/gateway.yaml.example config/gateway.yaml
cp .env.example .env

# Run the gateway
PYTHONPATH=src uvicorn gateway.main:app --host 0.0.0.0 --port 8001
```

### With Docker Compose

```bash
docker compose up -d
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
| `GATEWAY_DB_RETENTION_DAYS` | `90` | Auto-delete audit records older than N days (0 = disabled) |
| `GATEWAY_GUARD_ENABLED` | `false` | Enable guard model shadow analysis |
| `GATEWAY_GUARD_MODEL_NAME` | `ibm/granite3.2-guardian:5b` | Guard model name |
| `GATEWAY_GUARD_BASE_URL` | `http://localhost:11434` | Ollama server hosting the guard model |
| `GATEWAY_GUARD_TIMEOUT` | `15.0` | Guard model inference timeout (seconds) |
| `GATEWAY_PII_ENABLED` | `false` | Enable PII detection (always flags when enabled) |
| `GATEWAY_PII_SCRUB_ENABLED` | `false` | Enable PII scrubbing (replaces PII with placeholders) |
| `GATEWAY_PII_SCRUB_ROUTES` | `[]` | Routes where scrubbing is active (empty = all routes) |
| `GATEWAY_SECURITY_SCAN_ALLOWLIST_IPS` | `[]` | Source IPs to skip security scanning (JSON list) |
| `GATEWAY_ADMIN_API_KEY` | | Admin key for key management endpoints |

See [`.env.example`](.env.example) for a complete template.

### Endpoint Configuration

Copy `config/gateway.yaml.example` to `config/gateway.yaml`:

```yaml
endpoints:
  - name: ollama-local
    type: ollama
    url: http://localhost:11434
    enabled: true
    timeout: 120.0
    labels:
      tier: primary

auth:
  enabled: true
  api_keys:
    - key: "${GATEWAY_KEY_APP1}"
      client_id: my-app
      target_endpoint: ollama-local  # Optional: pin to specific endpoint
```

Keys are referenced via environment variables — never commit secrets to the config file.

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
| `POST /v1/completions` | Text completions |
| `POST /v1/embeddings` | Generate embeddings |

### Management

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check with provider status |
| `GET /v1/devmesh/catalog` | Full model catalog across all endpoints |
| `GET /api/stats` | Usage statistics (last 24h) |
| `GET /api/requests` | Recent audit log entries |
| `POST /api/keys` | Create new API key (admin) |
| `GET /api/keys` | List API keys (admin) |
| `DELETE /api/keys/{key_id}` | Revoke API key (admin) |

### Security

| Endpoint | Description |
|----------|-------------|
| `GET /api/security/stats` | Security scan statistics |
| `GET /api/security/alerts` | Recent security alerts |
| `GET /api/security/results` | Regex vs guard model side-by-side verdicts |

## Security

### Layered Defense

The gateway implements multiple security layers with zero added latency to the request path:

1. **Unicode Sanitization** (sync, ~0ms): Strips invisible characters, homoglyphs, and zero-width joiners used to bypass content filters.

2. **Pattern Detection** (sync, ~1ms): Regex-based injection pattern scanning — detects role overrides, delimiter attacks, and known prompt injection patterns. Threat levels: none, low, medium, high, critical.

3. **PII Detection** (sync, ~1ms): Detects email addresses, phone numbers, SSNs, credit card numbers, and IP addresses. Always flags when enabled. Optional scrubbing replaces PII with `[EMAIL]`, `[SSN]`, etc. placeholders — configurable per route.

4. **Guard Model** (async, shadow mode): Runs a guard model in the background after the response is sent. Supports:
   - **IBM Granite Guardian 3.2** — Returns Yes/No per category with confidence
   - **Llama Guard 3** — Returns safe/unsafe with S1-S13 category codes

5. **IP Allowlist**: Trusted internal services can be exempted from scanning.

### Guard Model Architecture

The guard model runs asynchronously — it does not add latency to requests. Results are stored for monitoring and comparison via the `/api/security/results` endpoint.

```
Request → Sanitize → PII Scan → Route → Respond (fast path)
                 ↘ Queue → Guard Model → Log Result (background)
```

### Policy Enforcement

- **Rate Limiting**: Global and per-user RPM limits (configurable in `gateway.yaml`)
- **Token Limits**: Max tokens per request
- **Per-Key Overrides**: Database-managed API keys can have custom rate limits, model allowlists (glob patterns), and endpoint restrictions

## Dashboard

React + TypeScript dashboard for monitoring. Features:

- **Stats Overview**: Request counts, success rates, latency, token usage
- **Security Monitor**: Alerts, guard scan results, regex vs guard comparison
- **Audit Log**: Recent requests with model, endpoint, latency, tokens
- **Model Catalog**: All discovered models across endpoints with sizes
- **API Key Management**: Create/delete database-managed API keys
- **Endpoint Health**: Per-endpoint status and model counts

## Architecture

```
Client Applications (Open WebUI, Discord bots, internal tools)
        |
        v
DevMesh Gateway (FastAPI)
├── Auth (API key + per-client routing)
├── Security (sanitize → PII scan → pattern detect → async guard)
├── Policy (rate limits, token limits, model/endpoint allowlists)
├── Dispatch (model resolution → provider selection → failover)
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
│   ├── observability/   # Structured logging and metrics
│   ├── policy/          # Rate limiting, token limits, enforcement
│   ├── providers/       # Provider adapters (Ollama, OpenAI, vLLM, etc.)
│   ├── routes/          # API endpoints (Ollama, OpenAI, management)
│   ├── security/        # Injection defense, PII scrubber, guard model
│   ├── storage/         # Audit logging, API key management, DB engine
│   ├── config.py        # YAML configuration loading
│   ├── settings.py      # Pydantic settings (env vars)
│   └── main.py          # FastAPI application
├── dashboard/           # React + Vite monitoring dashboard
├── config/              # Configuration templates
├── tests/               # Test suite
└── docs/                # Architecture and design documentation
```

## Testing

```bash
# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=gateway --cov-report=html
```

## License

MIT License — see [LICENSE](LICENSE) for details.
