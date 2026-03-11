# DevMesh LLM Gateway

An AI inference control plane. One API in front of all your LLM runtimes — with security, routing, policy enforcement, and observability built in.

**Self-hosted. Not a SaaS. Deploys inside your infrastructure.**

```
┌─────────────────────────────────────────────────────┐
│                  Your Applications                   │
│         (Open WebUI, Discord bots, internal tools)   │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│               DevMesh Gateway                        │
│                                                      │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐           │
│  │ Security │ │  Policy  │ │ Observ-   │           │
│  │ Layer    │ │  Engine  │ │ ability   │           │
│  └──────────┘ └──────────┘ └───────────┘           │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐           │
│  │ Routing  │ │  Audit   │ │ Model     │           │
│  │ Engine   │ │  Logger  │ │ Discovery │           │
│  └──────────┘ └──────────┘ └───────────┘           │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│              Inference Runtimes                      │
│                                                      │
│    Ollama    vLLM    TRT-LLM    SGLang    OpenAI    │
└─────────────────────────────────────────────────────┘
```

## Why This Exists

Production AI systems become fragmented fast:

- **No consistent API** — Service A talks to OpenAI, Service B to Ollama, Service C to vLLM. Each with different SDKs, auth patterns, and error handling.
- **No visibility** — Who's calling what model? How many tokens? What's the latency? Nobody knows.
- **No security** — Prompt injection, PII leakage, and jailbreak attempts flow straight through to your models.
- **No governance** — No rate limits, no model restrictions, no audit trail. Every app is on its own.

DevMesh Gateway solves this by acting as the control plane between your applications and your inference runtimes. One deployment. One API. Full visibility.

## Who This Is For

Teams deploying LLMs in production who need:

- A **unified API** across multiple inference runtimes
- **Prompt injection screening** with async guard-model analysis that adds no latency
- **PII detection and scrubbing** before prompts reach models
- **Request auditing** with full provenance
- **Per-client routing** and model access control
- **Centralized rate limiting** and policy enforcement

If you're running Ollama on a GPU box in a closet, or managing a fleet of vLLM instances — this is the thing that sits in front of all of them.

## Use Cases

- **Unified model access** — Route internal apps across Ollama, vLLM, and cloud APIs behind one interface
- **Security screening** — Add prompt injection and PII scanning in front of self-hosted models
- **Access control** — Enforce per-team or per-client model access policies and rate limits
- **Audit trail** — Log every AI request for compliance, debugging, and cost tracking
- **Runtime abstraction** — Standardize model access before building agents and pipelines on top

## What This Is / What This Isn't

**What it is:**
- A self-hosted AI request control plane
- A routing, policy, and observability layer in front of inference runtimes
- A unified API surface for multiple model backends

**What it isn't:**
- A model serving engine (use Ollama, vLLM, TRT-LLM for that)
- A hosted proxy or SaaS
- An agent framework
- A Kubernetes operator

## Quick Start

### Prerequisites

- Python 3.10+
- At least one inference runtime (e.g., [Ollama](https://ollama.com))

### Get Running

```bash
git clone https://github.com/PeterGreenAppliedAI/LLM_Gateway.git
cd LLM_Gateway

python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"

cp config/gateway.yaml.example config/gateway.yaml
cp .env.example .env
# Edit gateway.yaml with your endpoint URLs

PYTHONPATH=src uvicorn gateway.main:app --host 0.0.0.0 --port 8001
```

Your gateway is now running. Point your apps at `http://localhost:8001` using either the OpenAI or Ollama API format.

### With Docker Compose

```bash
docker compose up -d
```

### Dashboard

```bash
cd dashboard && npm install
npx vite --host 0.0.0.0 --port 5174
```

## How It Works

Every request flows through the same pipeline:

```
Request
  → Auth (API key validation, client identification)
  → Sanitize (Unicode normalization, invisible character stripping)
  → PII Scan (detect and optionally scrub personal data)
  → Policy Check (rate limits, model allowlists, token limits)
  → Route (resolve model → select endpoint → failover)
  → Respond
  → Async: Guard model analysis (background, zero latency impact)
  → Async: Audit log (SQLite or PostgreSQL)
```

The security guard model (Granite Guardian, Llama Guard) runs **after** the response is sent. It never adds latency. Results are logged for monitoring and pattern analysis.

## Configuration

### Endpoints

Define your inference runtimes in `config/gateway.yaml`:

```yaml
endpoints:
  - name: ollama-local
    type: ollama
    url: http://localhost:11434
    enabled: true
    timeout: 120.0
    labels:
      tier: primary

  - name: gpu-cluster
    type: ollama
    url: http://192.168.1.100:11434
    enabled: true

resolution:
  endpoint_priority:
    - ollama-local
    - gpu-cluster
  ambiguous_behavior: first_priority
```

### Authentication & Routing

```yaml
auth:
  enabled: true
  api_keys:
    - key: "${GATEWAY_KEY_APP1}"        # Always use env vars for secrets
      client_id: my-app
      target_endpoint: gpu-cluster      # Pin this client to a specific endpoint

    - key: "${GATEWAY_KEY_DEV}"
      client_id: dev-user               # Uses default routing
```

Database-managed API keys support additional controls: model allowlists (glob patterns), endpoint restrictions, and per-key rate limits.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_CONFIG_PATH` | `config/gateway.yaml` | Main config file |
| `GATEWAY_DB_URL` | `sqlite:///./data/gateway.db` | Database URL (SQLite or PostgreSQL) |
| `GATEWAY_DB_STORE_REQUEST_BODY` | `false` | Store prompts in audit log |
| `GATEWAY_DB_RETENTION_DAYS` | `90` | Auto-delete old audit records (0 = disabled) |
| `GATEWAY_GUARD_ENABLED` | `false` | Enable guard model shadow analysis |
| `GATEWAY_GUARD_MODEL_NAME` | `ibm/granite3.2-guardian:5b` | Guard model name |
| `GATEWAY_GUARD_BASE_URL` | `http://localhost:11434` | Ollama server hosting guard model |
| `GATEWAY_PII_ENABLED` | `false` | Enable PII detection (always flags) |
| `GATEWAY_PII_SCRUB_ENABLED` | `false` | Replace PII with placeholders |
| `GATEWAY_PII_SCRUB_ROUTES` | `[]` | Routes where scrubbing is active (empty = all) |
| `GATEWAY_ADMIN_API_KEY` | | Admin key for key management endpoints |

See [`.env.example`](.env.example) for the full list.

## API Surface

The gateway exposes two API formats. Your apps pick whichever they already use.

### OpenAI-Compatible

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | Chat completions (streaming supported) |
| `POST /v1/completions` | Text completions |
| `POST /v1/embeddings` | Generate embeddings |

### Ollama-Compatible

| Endpoint | Description |
|----------|-------------|
| `POST /api/chat` | Chat completions (streaming supported) |
| `POST /api/generate` | Text generation (streaming supported) |
| `POST /api/embeddings` | Generate embeddings |
| `GET /api/tags` | List available models |

### Management & Security

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check with provider status |
| `GET /v1/devmesh/catalog` | Full model catalog across all endpoints |
| `GET /api/stats` | Usage statistics (last 24h) |
| `GET /api/requests` | Audit log entries |
| `POST /api/keys` | Create API key |
| `GET /api/security/stats` | Security scan statistics |
| `GET /api/security/alerts` | Recent security alerts |
| `GET /api/security/results` | Regex vs guard model side-by-side verdicts |

## Security

### Layered Defense

| Layer | Timing | What It Does |
|-------|--------|-------------|
| **Unicode Sanitization** | Sync, ~0ms | Strips invisible characters, homoglyphs, zero-width joiners |
| **Pattern Detection** | Sync, ~1ms | Regex injection scanning — role overrides, delimiter attacks, known patterns |
| **PII Detection** | Sync, ~1ms | Detects emails, phones, SSNs, credit cards, IPs. Scrubbing is per-route configurable |
| **Guard Model** | Async, background | IBM Granite Guardian or Llama Guard — runs after response, never blocks |
| **IP Allowlist** | Sync, ~0ms | Trusted internal services skip scanning |

### Guard Model

The guard model is a **shadow analyzer** — it classifies every request in the background and logs whether it agrees with the regex scanner. This lets you:

- Measure regex false positive rates against a real model
- Detect attacks the regex missed
- Build confidence before switching to blocking mode

Results are available at `GET /api/security/results?disagreements_only=true`.

### Policy Enforcement

- **Rate Limiting**: Global and per-user RPM limits
- **Token Limits**: Max tokens per request
- **Model Allowlists**: Per-key glob patterns (e.g., `llama-*` allows all Llama variants)
- **Endpoint Restrictions**: Per-key endpoint access control
- **Per-Key Rate Overrides**: Database-managed keys can have custom RPM limits

## Dashboard

React + TypeScript monitoring UI:

- **Stats**: Request volume, success rates, latency, token usage
- **Security**: Alerts, guard scan results, regex vs guard comparison
- **Audit Log**: Recent requests with model, endpoint, latency, tokens
- **Model Catalog**: All discovered models across endpoints with sizes
- **API Keys**: Create/revoke database-managed keys
- **Endpoint Health**: Per-endpoint status and model availability

## Project Structure

```
src/gateway/
├── catalog/         # Model discovery across endpoints
├── dispatch/        # Model → endpoint routing and failover
├── models/          # Internal, OpenAI, and Ollama data models
├── observability/   # Structured logging and metrics
├── policy/          # Rate limits, token limits, enforcement
├── providers/       # Runtime adapters (Ollama, OpenAI, vLLM, TRT-LLM, SGLang)
├── routes/          # API endpoints
├── security/        # Injection defense, PII scrubber, guard model client
├── storage/         # Audit logging, API key management, async DB
├── config.py        # YAML config loader
├── settings.py      # Pydantic settings (env vars)
└── main.py          # FastAPI application
```

## Current Status

DevMesh Gateway is actively developed and running in production. It currently supports:

- OpenAI-compatible and Ollama-compatible API surfaces
- Ollama and OpenAI provider adapters (vLLM, TRT-LLM, SGLang adapters are scaffolded)
- Async audit logging to SQLite or PostgreSQL
- Per-key policy enforcement (rate limits, model allowlists, endpoint restrictions)
- Background guard-model analysis (IBM Granite Guardian, Llama Guard)
- PII detection and per-route scrubbing
- React monitoring dashboard

Still evolving:

- Broader provider adapter coverage (vLLM, TRT-LLM, SGLang)
- Additional policy modes
- Production hardening patterns
- Enterprise deployment guidance

## Testing

```bash
pytest tests/ -v
pytest tests/ --cov=gateway --cov-report=html
```

## License

MIT License — see [LICENSE](LICENSE) for details.
