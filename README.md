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
- **Token budgets** with cost-tier weighting per model

If you're running Ollama on a GPU box in a closet, or managing a fleet of vLLM instances — this is the thing that sits in front of all of them.

## Use Cases

- **Unified model access** — Route internal apps across Ollama, vLLM, and cloud APIs behind one interface
- **Security screening** — Add prompt injection and PII scanning in front of self-hosted models
- **Access control** — Enforce per-team or per-client model access policies and rate limits
- **Cost control** — Set daily token budgets per API key with model-tier cost multipliers
- **Audit trail** — Log every AI request for compliance, debugging, and cost tracking
- **Guard model training** — Collect labeled security data to finetune your own guard model
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
# Edit gateway.yaml with your endpoint URLs

# Start with the included script
./start-gateway.sh
```

Or start manually with env vars:

```bash
GATEWAY_DB_URL="sqlite+aiosqlite:///data/gateway.db" \
GATEWAY_DB_STORE_REQUEST_BODY=true \
GATEWAY_DB_STORE_RESPONSE_BODY=true \
GATEWAY_GUARD_ENABLED=true \
GATEWAY_GUARD_BASE_URL="http://localhost:11434" \
PYTHONPATH=src uvicorn gateway.main:app --host 0.0.0.0 --port 8001
```

Your gateway is now running. Point your apps at `http://localhost:8001` using either the OpenAI or Ollama API format.

### With Docker Compose

```bash
docker compose up -d
```

Starts the gateway, React dashboard, Prometheus, and Grafana.

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
  → Policy Check (rate limits, token budgets, model allowlists)
  → Route (resolve model → select endpoint → failover)
  → Respond
  → Async: Guard model analysis (background, zero latency impact)
  → Async: Audit log (SQLite or PostgreSQL)
  → Async: Security scan persistence (training data collection)
```

The security guard model runs **after** the response is sent. It never adds latency. Results are logged and persisted for monitoring, pattern analysis, and guard model finetuning.

## API Surface

The gateway exposes two API formats. Your apps pick whichever they already use.

### OpenAI-Compatible

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | Chat completions (streaming, tool/function calling) |
| `POST /v1/completions` | Text completions (streaming) |
| `POST /v1/embeddings` | Generate embeddings (single or batch) |
| `GET /v1/models` | List available models |

### Ollama-Compatible

| Endpoint | Description |
|----------|-------------|
| `POST /api/chat` | Chat completions (streaming, vision support) |
| `POST /api/generate` | Text generation (streaming) |
| `POST /api/embeddings` | Generate embeddings |
| `GET /api/tags` | List available models |

### Management

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check with per-provider status |
| `GET /metrics` | Prometheus metrics |
| `GET /v1/devmesh/catalog` | Full model catalog across all endpoints |
| `POST /v1/devmesh/catalog/refresh` | Trigger model discovery |
| `GET /api/stats` | Usage statistics (configurable time window) |
| `GET /api/requests` | Audit log entries (filterable) |
| `GET /api/models/usage` | Usage breakdown by model |
| `GET /api/endpoints/usage` | Usage breakdown by endpoint |
| `GET /api/usage/daily` | Daily aggregated usage |

### Security

| Endpoint | Description |
|----------|-------------|
| `GET /api/security/stats` | Security analyzer statistics |
| `GET /api/security/alerts` | Recent security alerts |
| `GET /api/security/results` | Regex vs guard model side-by-side verdicts |
| `GET /api/security/scans` | Persisted scans for review and labeling |
| `POST /api/security/scans/{id}/label` | Label a scan (safe/unsafe) for training data |
| `POST /api/security/scans/bulk-label` | Bulk label scans |
| `GET /api/security/scans/stats` | Labeling progress |
| `GET /api/security/training-data` | Export labeled data for guard model finetuning |

### PII Audit

| Endpoint | Description |
|----------|-------------|
| `GET /api/pii/stats` | Detection counts by type, scrub vs flag-only, unique values |
| `GET /api/pii/events` | Recent detections — SHA-256 hashes only, raw PII never exposed |

### Token Budgets

| Endpoint | Description |
|----------|-------------|
| `GET /api/budget/config` | Tier definitions, model classifications |
| `GET /api/budget/usage` | Per-key budget usage |
| `POST /api/budget/assignments` | Assign model to cost tier (runtime, no restart) |
| `DELETE /api/budget/assignments/{model}` | Remove tier assignment |

### Key Management

| Endpoint | Description |
|----------|-------------|
| `POST /api/keys` | Create API key (with model/endpoint allowlists) |
| `GET /api/keys` | List all keys (masked) |
| `DELETE /api/keys/{id}` | Revoke key |

## Security

### Layered Defense

| Layer | Timing | What It Does |
|-------|--------|-------------|
| **Unicode Sanitization** | Sync, ~0ms | Strips invisible characters, homoglyphs, zero-width joiners |
| **Pattern Detection** | Sync, ~1ms | Regex injection scanning — role overrides, delimiter attacks, encoding tricks, 25+ patterns |
| **PII Detection** | Sync, ~1ms | Detects emails, phones, SSNs, credit cards, IPs. Scrubs per-route. Logs SHA-256 hashes only — raw PII never stored |
| **Guard Model** | Async, background | Llama Guard 3 or Granite Guardian — shadow analysis, never blocks requests |
| **IP Allowlist** | Sync, ~0ms | Trusted internal services skip scanning |

### Guard Model

The guard model is a **shadow analyzer** — it classifies every request in the background and logs whether it agrees with the regex scanner. This lets you:

- Measure regex false positive rates against a real model
- Detect attacks the regex missed
- Build confidence before switching to blocking mode
- **Collect training data** to finetune your own guard model

View disagreements: `GET /api/security/results?disagreements_only=true`

### Training Data Pipeline

Every security scan is automatically persisted to the database with the original messages, regex verdict, and guard verdict. This builds a labeled dataset for finetuning a custom guard model:

1. **Automatic collection** — scans persist as traffic flows through the gateway
2. **Review** — browse scans filtered by disagreements, threat level, or unlabeled status
3. **Label** — mark scans as safe/unsafe with category codes via API or dashboard
4. **Export** — download labeled data in Llama Guard finetuning format

```bash
# Check labeling progress
curl -s http://localhost:8001/api/security/scans/stats

# Review disagreements (highest value for labeling)
curl -s "http://localhost:8001/api/security/scans?disagreements_only=true"

# Export training data
curl -s "http://localhost:8001/api/security/training-data?format=llama_guard"
```

### PII Detection Audit Trail

When PII detection is enabled, every detection is persisted to the `pii_events` table — but **raw PII values are never stored**. Instead, each detected value is SHA-256 hashed before logging. The audit trail records:

- Detection type (EMAIL, PHONE, SSN, CREDIT_CARD, IP_ADDRESS)
- Position in the message, message role, timestamp
- SHA-256 hash of the original value (for deduplication and audit proof)
- Whether the value was scrubbed or flagged only

This solves the catch-22 of PII protection systems: you need evidence that the control fired, but storing the matched value means your logs become another place where PII lives. Hashing proves the system caught something and handled it correctly without creating a second risk surface.

```bash
# View PII detection stats
curl -s http://localhost:8001/api/pii/stats

# View recent detections (hashes only)
curl -s http://localhost:8001/api/pii/events
```

## Policy Enforcement

- **Rate Limiting**: Global and per-key RPM limits with burst detection
- **Token Limits**: Max tokens per request (default 32,768)
- **Model Allowlists**: Per-key glob patterns (e.g., `llama-*` allows all Llama variants)
- **Endpoint Restrictions**: Per-key endpoint access control
- **Per-Key Rate Overrides**: Database-managed keys can have custom RPM limits

### Token Budgets

Daily token quotas with cost-tier weighting. Models cost different amounts against a budget depending on how expensive they are to run.

- **Cost tiers**: Named levels (e.g., `frontier: 15x`, `midrange: 3x`, `standard: 1x`, `embedding: 0.1x`)
- **Model assignments**: Map any model to a tier — exact names or glob patterns
- **Unknown models default to an expensive multiplier** (configurable) until classified
- **Auto-discovery integration**: The gateway discovers models from all endpoints. Unclassified models show up in the dashboard for assignment
- **Runtime management**: Assign models to tiers via API (`POST /api/budget/assignments`) — no restart, no config file edits
- **Per-tier global caps**: Optional daily token limits per tier across all keys

When a new model appears, it defaults to an expensive rate until someone classifies it from the dashboard. No config changes needed.

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
| `GATEWAY_PII_ENABLED` | `false` | Enable PII detection (always flags, never stores raw PII) |
| `GATEWAY_PII_SCRUB_ENABLED` | `false` | Replace PII with placeholders before reaching models |
| `GATEWAY_PII_SCRUB_ROUTES` | `[]` | Routes where scrubbing is active (empty = all) |
| `GATEWAY_ADMIN_API_KEY` | | Admin key for key management endpoints |
| `GATEWAY_LOG_LEVEL` | `INFO` | Logging level |
| `GATEWAY_LOG_FORMAT` | `json` | Log format (json or text) |
| `GATEWAY_CORS_ORIGINS` | `[]` | Allowed CORS origins for dashboard |

See [`.env.example`](.env.example) for the full list.

## Routing & Discovery

### Multi-Endpoint Dispatch

The gateway resolves models to endpoints using a 5-step priority chain:

1. **Explicit override** — `endpoint/model` syntax in the request
2. **Environment filter** — only env-approved endpoints considered
3. **Per-model default** — config-specified model → endpoint mapping
4. **Endpoint priority** — first in priority list that has the model
5. **Error** — no resolution found

If the primary endpoint is unhealthy, the gateway automatically fails over to the next available endpoint.

### Auto-Discovery

The gateway polls all configured endpoints every 60 seconds to discover available models. New models appear in the catalog and dashboard automatically. Trigger manually with `POST /v1/devmesh/catalog/refresh`.

## Observability

- **Structured JSON logging** — per-request context (request_id, client_id, model), prompt redaction option
- **Prometheus metrics** — `gateway_requests_total`, `gateway_request_latency_ms`, `gateway_tokens_per_second`, `gateway_active_requests`, and more
- **Audit log** — every request persisted to SQLite or PostgreSQL with full metadata
- **Daily aggregation** — automated rollups for dashboard performance
- **Grafana dashboards** — included in Docker Compose setup

## Dashboard

React + TypeScript monitoring UI:

- **Stats**: Request volume, success rates, latency, token usage, cost
- **Security**: Alerts, guard scan results, regex vs guard comparison, labeling workflow, PII detection audit
- **Audit Log**: Recent requests with model, endpoint, latency, tokens
- **Model Catalog**: All discovered models across endpoints with sizes
- **API Keys**: Create/revoke database-managed keys with policies
- **Endpoint Health**: Per-endpoint status and model availability
- **Token Budgets**: Tier assignments, model classifications, per-key usage

## Providers

| Provider | Status | Capabilities |
|----------|--------|-------------|
| **Ollama** | Full | Chat, generate, embeddings, model discovery, vision |
| **OpenAI** | Full | Chat, completions, embeddings, model discovery |
| **vLLM** | Full | Chat, completions, embeddings (OpenAI-compatible) |
| **TRT-LLM** | Scaffolded | NVIDIA TensorRT LLM runtime |
| **SGLang** | Scaffolded | Structured generation runtime |

## Project Structure

```
src/gateway/
├── catalog/         # Model discovery across endpoints
├── dispatch/        # Model → endpoint routing and failover
├── models/          # Internal, OpenAI, and Ollama data models
├── observability/   # Structured logging and Prometheus metrics
├── policy/          # Rate limits, token limits, token budgets
├── providers/       # Runtime adapters (Ollama, OpenAI, vLLM, TRT-LLM, SGLang)
├── routes/          # API endpoints (OpenAI, Ollama, management, security)
├── security/        # Injection defense, PII scrubber, guard model, training data
├── storage/         # Audit logging, API keys, security scans, async DB
├── config.py        # YAML config loader
├── settings.py      # Pydantic settings (env vars)
└── main.py          # FastAPI application
```

## Current Status

DevMesh Gateway is actively developed and running in production. It currently supports:

- OpenAI-compatible and Ollama-compatible API surfaces
- Ollama, OpenAI, and vLLM provider adapters (TRT-LLM, SGLang scaffolded)
- Async audit logging to SQLite or PostgreSQL
- Per-key policy enforcement (rate limits, model allowlists, endpoint restrictions)
- Daily token budgets with cost-tier multipliers and runtime model assignment
- Background guard-model analysis (Llama Guard 3, Granite Guardian)
- PII detection and per-route scrubbing with cryptographic audit trail (SHA-256 hashes, never raw PII)
- Security scan persistence with labeling workflow for guard model finetuning
- React monitoring dashboard
- Prometheus metrics and Grafana dashboards
- GitHub Actions CI (Python 3.10/3.11/3.12 + linting)

Still evolving:

- Broader provider adapter coverage (TRT-LLM, SGLang)
- Guard model finetuning pipeline (data collection in place, training next)
- Dashboard labeling UI for security scans
- Production hardening patterns
- Enterprise deployment guidance

## Testing

```bash
# All tests (514 tests)
pytest tests/ -v

# With coverage
pytest tests/ --cov=gateway --cov-report=html
```

## License

MIT License — see [LICENSE](LICENSE) for details.
