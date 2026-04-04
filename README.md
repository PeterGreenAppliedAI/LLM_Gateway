# DevMesh LLM Gateway

You have GPU boxes running Ollama. Maybe a vLLM cluster. Maybe OpenAI for some things. Your apps each talk to a different one with different SDKs, different auth, different error handling. Nobody knows who's calling what, how many tokens are being burned, or whether someone just sent a credit card number straight into a model.

**DevMesh Gateway sits in front of all of them.** One API. One auth layer. Full audit trail. Security scanning that adds zero latency. Deploy it inside your infrastructure — it's not a SaaS.

<!-- TODO: Add dashboard screenshot here -->
<!-- ![Dashboard](docs/screenshots/dashboard.png) -->

## Get Running in 2 Minutes

```bash
git clone https://github.com/PeterGreenAppliedAI/LLM_Gateway.git
cd LLM_Gateway

python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"

cp config/gateway.yaml.example config/gateway.yaml
# Edit gateway.yaml with your endpoint URLs

./start-gateway.sh
```

Point your apps at `http://your-server:8001`. Use OpenAI format or Ollama format — both work.

```python
# Works with any OpenAI-compatible client
from openai import OpenAI
client = OpenAI(base_url="http://your-server:8001/v1", api_key="your-key")
response = client.chat.completions.create(model="llama3.1:8b", messages=[...])
```

```bash
# Works with Ollama clients too
curl http://your-server:8001/api/chat -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hello"}]}'
```

Dashboard:

```bash
cd dashboard && npm install && npx vite --host 0.0.0.0 --port 5174
```

## What You Get

| Problem | How the gateway solves it |
|---------|--------------------------|
| 3 GPU boxes, no unified API | One endpoint for all your runtimes — Ollama, vLLM, OpenAI, TRT-LLM |
| No idea who's calling what | Every request logged with client ID, model, tokens, latency, full audit trail |
| Prompt injection goes straight through | Regex pattern detection (sync, ~1ms) + guard model analysis (async, zero latency impact) |
| PII leaking into models | Detects emails, phones, SSNs, credit cards, IPs. Optional scrubbing. Cryptographic audit trail (SHA-256 hashes, never stores raw PII) |
| No rate limits or access control | Per-key rate limits, model allowlists, endpoint restrictions, daily token budgets with cost tiers |
| New model deployed, nobody classified it | Auto-discovery polls all endpoints. Unclassified models default to expensive tier until someone assigns them from the dashboard |
| Want to finetune your own guard model | Every security scan persisted with regex + guard verdicts. Label from dashboard, export in Llama Guard format |

## How It's Different from LiteLLM

LiteLLM is a good proxy for routing requests to different LLM providers. DevMesh Gateway is a different thing — it's a **security and governance layer** that happens to also do routing.

| | DevMesh Gateway | LiteLLM |
|---|---|---|
| **Primary focus** | Security, audit, policy enforcement | Provider routing, cost tracking |
| **Prompt injection defense** | Regex + async guard model (Granite Guardian / Llama Guard) | Not built in |
| **PII detection** | Detect, scrub, cryptographic audit trail | Not built in |
| **Guard model training pipeline** | Collect scans, label safe/unsafe, export training data | No |
| **Security scan labeling** | Dashboard UI for reviewing and labeling scans | No |
| **Token budgets** | Cost-tier weighted daily quotas per API key | Spend limits per key |
| **Self-hosted only** | Yes — runs inside your infrastructure | Cloud + self-hosted options |
| **Dashboard** | Included React UI with security, PII, budgets, requests | Separate UI project |

If you just need to route requests to different providers, LiteLLM works. If you need to know what's going through your models, stop PII from leaking, build your own guard model, and enforce per-team budgets — that's what this is for.

## Dashboard

React + TypeScript monitoring UI with four tabs:

<!-- TODO: Take screenshots and add them here -->
<!-- ![Dashboard Tab](docs/screenshots/dashboard-tab.png) -->
<!-- ![Security Tab](docs/screenshots/security-tab.png) -->
<!-- ![Keys & Budgets Tab](docs/screenshots/keys-budgets-tab.png) -->
<!-- ![Requests Tab](docs/screenshots/requests-tab.png) -->

- **Dashboard** — Request volume, success rates, latency, token usage, endpoint health, top models
- **Security** — Guard model verdicts, regex vs guard comparison, PII detection audit, security scan labeling with training data export
- **Keys & Budgets** — API key management (create/revoke with policies), token budget tiers, model-to-tier assignments, per-key usage
- **Requests** — Full audit log with click-to-expand request/response details

## How It Works

Every request flows through the same pipeline:

```
Request → Auth → Sanitize → PII Scan → Policy Check → Route → Respond
                                                          ↓
                              Async: Guard model + Audit log + Security scan
```

The security guard model runs **after** the response is sent. It classifies every request in the background and logs whether it agrees with the regex scanner. Zero latency impact. This lets you measure false positive rates, detect attacks the regex missed, and build a labeled dataset for finetuning your own guard model.

## Security

| Layer | Timing | What It Does |
|-------|--------|-------------|
| **Unicode Sanitization** | Sync, ~0ms | Strips invisible characters, homoglyphs, zero-width joiners |
| **Pattern Detection** | Sync, ~1ms | Regex injection scanning — role overrides, delimiter attacks, encoding tricks, 25+ patterns |
| **PII Detection** | Sync, ~1ms | Detects emails, phones, SSNs, credit cards, IPs. Logs SHA-256 hashes only — raw PII never stored |
| **Guard Model** | Async, background | Granite Guardian or Llama Guard — shadow analysis, never blocks requests |

### PII Audit Trail

PII detection has a catch-22: you need evidence the system caught something, but storing the matched value means your logs become another PII liability. The gateway solves this with cryptographic hashing — every detection is logged with a SHA-256 hash of the original value. You can prove detection happened, deduplicate across requests, and audit compliance without ever storing the raw PII.

### Training Data Pipeline

Every security scan is automatically persisted with the original messages, regex verdict, and guard verdict. Label scans as safe/unsafe from the dashboard, then export in Llama Guard format for finetuning your own guard model.

## API Compatibility

The gateway speaks both OpenAI and Ollama format. Your apps don't need to change.

**OpenAI:** `POST /v1/chat/completions`, `POST /v1/completions`, `POST /v1/embeddings`, `GET /v1/models`

**Ollama:** `POST /api/chat`, `POST /api/generate`, `POST /api/embeddings`, `GET /api/tags`

**Management:** `/health`, `/metrics`, `/api/stats`, `/api/requests`, `/api/models/usage`, `/api/endpoints/usage`

**Security:** `/api/security/stats`, `/api/security/alerts`, `/api/security/scans`, `/api/pii/stats`, `/api/pii/events`

**Budgets:** `/api/budget/config`, `/api/budget/usage`, `/api/budget/assignments`

**Keys:** `POST /api/keys`, `GET /api/keys`, `DELETE /api/keys/{id}`

## Routing & Failover

The gateway resolves models to endpoints using a priority chain:

1. **Explicit override** — `endpoint/model` syntax (e.g., `gpu-node/phi4:latest`)
2. **Per-client pinning** — config-specified `target_endpoint` per API key
3. **Endpoint priority** — first in priority list that has the model
4. **Automatic failover** — if primary endpoint is unhealthy, route to next available

Auto-discovery polls all endpoints every 60 seconds. New models appear in the catalog and dashboard automatically.

## Policy Enforcement

- **Rate Limiting** — Global and per-key RPM limits
- **Token Budgets** — Daily quotas with cost-tier weighting (frontier 15x, standard 1x, embedding 0.1x)
- **Model Allowlists** — Per-key glob patterns (e.g., `llama-*`)
- **Endpoint Restrictions** — Per-key endpoint access control
- **Runtime management** — Assign models to tiers via API, no restart needed

## Configuration

```yaml
# config/gateway.yaml
endpoints:
  - name: gpu-box-1
    type: ollama
    url: http://192.168.1.100:11434
    enabled: true

  - name: gpu-box-2
    type: ollama
    url: http://192.168.1.101:11434
    enabled: true

resolution:
  endpoint_priority:
    - gpu-box-1
    - gpu-box-2

auth:
  enabled: true
  api_keys:
    - key: "${GATEWAY_KEY_APP1}"
      client_id: my-app
      target_endpoint: gpu-box-1    # Pin this client to a specific endpoint
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_DB_URL` | `sqlite:///./data/gateway.db` | Database URL (SQLite or PostgreSQL) |
| `GATEWAY_DB_STORE_REQUEST_BODY` | `false` | Store prompts in audit log |
| `GATEWAY_GUARD_ENABLED` | `false` | Enable guard model shadow analysis |
| `GATEWAY_GUARD_MODEL_NAME` | `ibm/granite3.2-guardian:5b` | Guard model name |
| `GATEWAY_GUARD_BASE_URL` | `http://localhost:11434` | Ollama server hosting guard model |
| `GATEWAY_PII_ENABLED` | `false` | Enable PII detection |
| `GATEWAY_PII_SCRUB_ENABLED` | `false` | Replace PII with placeholders |
| `GATEWAY_ADMIN_API_KEY` | | Admin key for key management |
| `GATEWAY_CORS_ORIGINS` | `["*"]` | Allowed CORS origins |

## Providers

| Provider | Status | Capabilities |
|----------|--------|-------------|
| **Ollama** | Full | Chat, generate, embeddings, model discovery, vision |
| **OpenAI** | Full | Chat, completions, embeddings, model discovery |
| **vLLM** | Full | Chat, completions, embeddings (OpenAI-compatible) |
| **TRT-LLM** | Scaffolded | NVIDIA TensorRT LLM runtime |
| **SGLang** | Scaffolded | Structured generation runtime |

## Testing

```bash
pytest tests/ -v              # 514 tests
pytest tests/ --cov=gateway   # With coverage
```

## License

MIT License — see [LICENSE](LICENSE) for details.
