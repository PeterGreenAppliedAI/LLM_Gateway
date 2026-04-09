# DevMesh LLM Gateway

You have GPU boxes running Ollama. Maybe a vLLM cluster. Maybe OpenAI for some things. Your apps each talk to a different one with different SDKs, different auth, different error handling. Nobody knows who's calling what, how many tokens are being burned, or whether someone just sent a credit card number straight into a model.

**DevMesh Gateway sits in front of all of them.** One API. One auth layer. Full audit trail. Security scanning that adds zero latency. Deploy it inside your infrastructure — it's not a SaaS.

## Who This Is For

Built for teams running models on-prem who need to prove what's going through them. Used in regulated environments where data can't leave the building. If you need an audit trail a compliance officer can read, this is for you.

- **Regulated industries** — Healthcare, finance, legal, government. Data sovereignty is non-negotiable.
- **Air-gapped or on-prem AI deployments** — Your models run on your hardware. Your gateway should too.
- **Compliance-driven AI programs** — You need to show auditors what data touched which model, when, and what controls were in place.
- **Teams with multiple inference runtimes** — Ollama on a GPU box, vLLM on a cluster, OpenAI for overflow. One gateway handles all of them.

## What Makes This Different

This isn't just a proxy. It's a **security and governance layer** with a built-in feedback loop that gets smarter the longer it runs.

**PII detection that doesn't create a second liability.** Every detection is SHA-256 hashed before logging — you can prove the system caught a credit card number and scrubbed it, without your audit logs becoming another place where credit card numbers live. This solves the catch-22 most PII systems ignore: storing the matched value turns your compliance evidence into a compliance violation.

**Your gateway trains its own guard model.** Every request is automatically scanned by both a regex engine and a shadow guard model (Granite Guardian or Llama Guard). Results are persisted. Disagreements are flagged. You label them from the dashboard — safe or unsafe — and export the labeled data in Llama Guard format. The longer your gateway runs, the more training data you collect for a custom guard model tuned to your actual traffic patterns. Most security gateways are static rule sets. This one builds a dataset.

**Zero-latency security analysis.** The guard model runs asynchronously after the response is sent. It never blocks a request. It never adds latency. It silently classifies everything and logs whether it agrees with the regex scanner. You get full security visibility without any performance cost.

<!-- TODO: Add dashboard screenshots here -->
<!-- ![Dashboard](docs/screenshots/dashboard.png) -->
<!-- ![Security Tab](docs/screenshots/security-tab.png) -->

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

## What You Get

| Problem | How the gateway solves it |
|---------|--------------------------|
| 3 GPU boxes, no unified API | One endpoint for all your runtimes — Ollama, vLLM, OpenAI, TRT-LLM |
| No idea who's calling what | Every request logged with client ID, model, tokens, latency, full audit trail |
| Prompt injection goes straight through | Regex pattern detection (sync, ~1ms) + guard model analysis (async, zero latency) |
| PII leaking into models | Detects emails, phones, SSNs, credit cards, IPs. Optional scrubbing. SHA-256 audit trail — raw PII never stored |
| No rate limits or access control | Per-key rate limits, model allowlists, endpoint restrictions, daily token budgets with cost tiers |
| New model deployed, nobody classified it | Auto-discovery polls endpoints every 60s. Unclassified models default to expensive tier until assigned |
| Want to finetune your own guard model | Every scan persisted with regex + guard verdicts. Label from dashboard. Export in Llama Guard format |
| Compliance needs an audit trail | Every request, every PII detection, every security scan — timestamped, client-attributed, exportable |

## How It's Different from LiteLLM

LiteLLM is a good proxy for routing requests to different LLM providers. DevMesh Gateway is a different category — it's a **security and governance layer** that happens to also do routing.

| | DevMesh Gateway | LiteLLM |
|---|---|---|
| **Primary focus** | Security, audit, policy enforcement | Provider routing, cost tracking |
| **Prompt injection defense** | Regex + async guard model (Granite Guardian / Llama Guard) | Not built in |
| **PII detection** | Detect, scrub, cryptographic audit trail | Not built in |
| **Guard model training** | Closed-loop: collect, label, export, finetune | No |
| **Token budgets** | Cost-tier weighted daily quotas per API key | Spend limits per key |
| **Self-hosted only** | Yes — runs inside your infrastructure | Cloud + self-hosted options |
| **Dashboard** | Included React UI with security, PII, budgets, requests | Separate UI project |
| **Test coverage** | 514 tests across Python 3.10/3.11/3.12 | Varies |

If you just need to route requests to different providers, LiteLLM works. If you need to know what's going through your models, stop PII from leaking, build your own guard model, and prove it all to an auditor — that's what this is for.

## Dashboard

React + TypeScript monitoring UI with four tabs:

<!-- TODO: Take screenshots and drop into docs/screenshots/ -->
<!-- ![Dashboard Tab](docs/screenshots/dashboard-tab.png) -->
<!-- ![Security Tab](docs/screenshots/security-tab.png) -->
<!-- ![Keys & Budgets Tab](docs/screenshots/keys-budgets-tab.png) -->
<!-- ![Requests Tab](docs/screenshots/requests-tab.png) -->

- **Dashboard** — Request volume, success rates, latency, token usage, endpoint health, top models
- **Security** — Guard model verdicts, regex vs guard comparison, PII detection audit with hash-only event log, security scan labeling with bulk actions and training data export
- **Keys & Budgets** — API key management (create/revoke with model/endpoint policies), token budget tiers, model-to-tier assignments, per-key usage tracking
- **Requests** — Full audit log with click-to-expand request/response details, token counts, latency, streaming metrics

```bash
cd dashboard && npm install && npx vite --host 0.0.0.0 --port 5174
```

## Security Architecture

| Layer | Timing | What It Does |
|-------|--------|-------------|
| **Unicode Sanitization** | Sync, ~0ms | Strips invisible characters, homoglyphs, zero-width joiners |
| **Pattern Detection** | Sync, ~1ms | 25+ regex patterns — role overrides, delimiter attacks, encoding tricks |
| **PII Detection** | Sync, ~1ms | Emails, phones, SSNs, credit cards, IPs. SHA-256 hashed audit trail. Raw PII never stored |
| **Guard Model** | Async, background | Granite Guardian or Llama Guard — classifies every request, logs agreement/disagreement with regex |

### The Guard Model Training Loop

This is the feature most security gateways don't have: a **closed-loop system** where your gateway generates its own training data.

1. **Every request is scanned** by both regex and guard model simultaneously
2. **Disagreements are flagged** — the most valuable data points for training
3. **You label them** from the dashboard UI — safe or unsafe, with optional category codes
4. **Export labeled data** in Llama Guard finetuning format
5. **Finetune your own guard model** on your actual traffic patterns

The longer your gateway runs, the better your training dataset. Most gateways ship with a frozen rule set. This one adapts.

## How It Works

```
Request → Auth → Sanitize → PII Scan → Policy Check → Route → Respond
                                                          ↓
                              Async: Guard model + Audit log + Security scan
```

## API Compatibility

Both OpenAI and Ollama formats — your apps don't need to change.

**OpenAI:** `POST /v1/chat/completions`, `POST /v1/completions`, `POST /v1/embeddings`, `GET /v1/models`

**Ollama:** `POST /api/chat`, `POST /api/generate`, `POST /api/embeddings`, `GET /api/tags`

**Management:** `/health`, `/metrics`, `/api/stats`, `/api/requests`, `/api/models/usage`, `/api/endpoints/usage`

**Security:** `/api/security/stats`, `/api/security/alerts`, `/api/security/scans`, `/api/pii/stats`, `/api/pii/events`

**Budgets:** `/api/budget/config`, `/api/budget/usage`, `/api/budget/assignments`

**Keys:** `POST /api/keys`, `GET /api/keys`, `DELETE /api/keys/{id}`

## Routing & Failover

1. **Explicit override** — `endpoint/model` syntax (e.g., `gpu-node/phi4:latest`)
2. **Per-client pinning** — `target_endpoint` per API key
3. **Endpoint priority** — first in priority list that has the model
4. **Automatic failover** — unhealthy endpoint? Route to next available

Auto-discovery polls all endpoints every 60 seconds. New models appear automatically.

## Policy Enforcement

- **Rate Limiting** — Global and per-key RPM limits
- **Token Budgets** — Daily quotas with cost-tier weighting (frontier 15x, standard 1x, embedding 0.1x)
- **Model Allowlists** — Per-key glob patterns (e.g., `llama-*`)
- **Endpoint Restrictions** — Per-key endpoint access control
- **Runtime management** — Assign models to tiers via API or dashboard, no restart needed

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
      target_endpoint: gpu-box-1
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

## Production Deployment

For evaluation, `./start-gateway.sh` is all you need. For production:

- **Process management** — Run behind systemd or supervisor. The startup script works as an `ExecStart` target.
- **Database** — Switch from SQLite to PostgreSQL for concurrent access: `GATEWAY_DB_URL=postgresql+asyncpg://user:pass@host/gateway`
- **Reverse proxy** — Put nginx or Caddy in front for TLS termination. The gateway runs HTTP on port 8001.
- **Backups** — If using SQLite, back up `data/gateway.db`. If PostgreSQL, use `pg_dump` on your schedule.
- **Log retention** — `GATEWAY_DB_RETENTION_DAYS=90` auto-deletes old audit records. Adjust based on compliance requirements.
- **Docker Compose** — `docker compose up -d` starts the gateway, dashboard, Prometheus, and Grafana.

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
