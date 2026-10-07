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

python3 -m venv venv && source venv/bin/activate   # Python 3.11 or newer
pip install -e ".[dev]"

cp config/gateway.yaml.example config/gateway.yaml
# Edit gateway.yaml with your endpoint URLs

# Optional: API keys live in a gitignored .env (sourced by the start script).
# Keys referencing unset env vars are disabled with a warning — the gateway
# always boots. Skip this entirely for a local, keyless evaluation.
cp .env.example .env

./start-gateway.sh
```

Point your apps at `http://your-server:8001`. Use OpenAI format or Ollama format — both work. Clients need an API key; keyless clients on other machines need `auth.anonymous` (see [Access modes](#api-compatibility)). To try things out without keys, run `./start-gateway.sh --dev` (test mode, this machine only).

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
| PII leaking into models | Detects emails, US phone numbers, SSNs, credit cards, IPs (pattern-based: names and postal addresses aren't detected). Optional scrubbing over the whole input. SHA-256 audit trail — raw PII never stored |
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
| **Test coverage** | ~915 tests on Python 3.11–3.13, Linux and Windows, SQLite, PostgreSQL and Redis | Varies |

If you just need to route requests to different providers, LiteLLM works. If you need to know what's going through your models, catch common PII before it leaks, build your own guard model, and prove it all to an auditor — that's what this is for.

## Dashboard

React + TypeScript monitoring UI with five tabs:

<!-- TODO: Take screenshots and drop into docs/screenshots/ -->
<!-- ![Dashboard Tab](docs/screenshots/dashboard-tab.png) -->
<!-- ![Security Tab](docs/screenshots/security-tab.png) -->
<!-- ![Keys & Budgets Tab](docs/screenshots/keys-budgets-tab.png) -->
<!-- ![Requests Tab](docs/screenshots/requests-tab.png) -->

- **Dashboard** — Request volume, success rates, latency, token usage, endpoint health, top models
- **Security** — Guard model verdicts, regex vs guard comparison, PII scrubbing controls (admin, applied live) and detection audit with hash-only event log, security scan labeling with bulk actions and training data export
- **Keys & Budgets** — API key management (create/revoke with model/endpoint policies), token budget tiers, model-to-tier assignments, per-key usage tracking
- **Requests** — Full audit log with click-to-expand request/response details, token counts, latency, streaming metrics
- **Voice** — Voice engines (health, profile, voices, models), a text-to-speech playground (voice picker filtered by language/gender, Kokoro voice blending with weights, speed within the engine's range, play/download) and speech-to-text (upload or record, model, language or auto-detect, output format). Playground requests use the real routes, so they are audited and metered

```bash
cd dashboard && npm install && npx vite --host 0.0.0.0 --port 5174
```

On first load, enter the admin key (`GATEWAY_ADMIN_API_KEY`) in the header field (top right) — with `auth.enabled: true` the dashboard endpoints require it, and client keys are refused. In solo mode (`auth.enabled: false`) and test mode the dashboard works from this machine without a key. The key is stored in the browser's localStorage and sent as `X-API-Key` on every request.

## Security Architecture

| Layer | Timing | What It Does |
|-------|--------|-------------|
| **Unicode Sanitization** | Sync, ~0ms | Strips invisible characters, homoglyphs, zero-width joiners |
| **Pattern Detection** | Sync, ~1ms | 25+ regex patterns — role overrides, delimiter attacks, encoding tricks |
| **PII Detection** | Sync, linear in input size | Emails, US phone numbers, SSNs, credit cards, IPs, anywhere in the input. Pattern-based: names and addresses aren't detected. SHA-256 hashed audit trail. Raw PII never stored |
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

**Access modes** (D-042):

- **Keys** (`auth.enabled: true`) — every request needs an API key. Keyless requests (stock Ollama clients such as LocalClaw) are refused unless you enable `auth.anonymous` and list the networks they come from in `auth.anonymous.allowed_networks`; restrict their models, endpoints and rate too, so clients can't drop their key to escape its limits. Dashboard, security, budget and key-management endpoints are the operator console and **require** `GATEWAY_ADMIN_API_KEY`; without it they return 403.
- **Solo** (`auth.enabled: false`) — no keys, for one person on one machine. Requests are accepted only from `auth.anonymous.allowed_networks` (default: this machine). A request that arrived through a reverse proxy never counts as local.
- **Test mode** (`./start-gateway.sh --dev`, or `GATEWAY_DEV_MODE=true`) — try a real config without minting keys: keyless inference and dashboard from `GATEWAY_DEV_NETWORKS` (default: this machine), keys that are sent are still checked, traffic is audited as client `dev`, and the script uses a separate `data/dev.db`. Shown in the startup log, `/health` and the dashboard.

Set `GATEWAY_PROFILE=production` to refuse startup with test mode on, auth off, no admin key, or unrestricted keyless access.

**Upgrading:** keyless access used to be on by default. Keyless clients on other machines now need:

```yaml
auth:
  anonymous:
    enabled: true
    allowed_networks: ["192.168.1.40/32"]   # the client's address or subnet
```

**Environments:** a key bound to an environment (`environment: prod`) is routed only to that environment's endpoints and approved models, including on fallback; `X-Environment` can't override it. Keys without an environment may pick one with `X-Environment`, otherwise they get the default (`dev` if defined, else the first).

**OpenAI:** `POST /v1/chat/completions`, `POST /v1/completions`, `POST /v1/embeddings`, `GET /v1/models`

**Voice (OpenAI audio API):** `POST /v1/audio/speech` (text-to-speech, streamed), `POST /v1/audio/transcriptions` and `/v1/audio/translations` (speech-to-text, multipart). Works with any engine that speaks OpenAI's audio API (Kokoro-FastAPI, speaches/faster-whisper, vLLM, vLLM-Omni, ...): declare the endpoint with `type: openai` and `capabilities: [tts]` or `[stt]`. Same auth, scope, PII, audit and budget rules as chat. `GET /v1/audio/voices` lists the voices the gateway discovered (with language/gender where known). Optional engine profiles (`config/profiles/`, e.g. `profile: kokoro`) add voice metadata, blending and setting ranges; requests then route to an endpoint that has the requested voice, and unknown voices or out-of-range settings are rejected with the valid choices. Engine setup notes: [docs/MEDIA_ENGINES.md](docs/MEDIA_ENGINES.md).

**Ollama:** `POST /api/chat`, `POST /api/generate`, `POST /api/embed`, `POST /api/embeddings` (legacy), `GET /api/tags`

Full Ollama passthrough: `format` (JSON mode and schema-constrained decoding), `options` (`num_ctx`, `top_k`, `stop`, ...), `keep_alive`, `tools` (object arguments, streaming included), and vision `images` all reach the engine untouched. The gateway never invents defaults — unset parameters use the engine's own defaults, and policy caps reject loudly (4xx) instead of silently clamping.

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
| `GATEWAY_DB_REQUIRED` | `true` | Refuse to start if the database can't be initialized (no silent run without an audit trail) |
| `GATEWAY_DB_AUDIT_DURABILITY` | `auto` | How audit rows are written (D-038). `process`: to a local intent log and the response returns; a background task writes them to the DB, and nothing is lost on a gateway crash. `grouped`: the response also waits until the log is on disk (survives power loss; one shared disk flush per burst). `sync`: the response waits for the DB commit. `auto`: `process` on SQLite, `sync` on PostgreSQL |
| `GATEWAY_DB_AUDIT_JOURNAL_PATH` | `data/audit-journal` | Intent log directory. Use local disk; keep it on a persistent volume in containers |
| `GATEWAY_DB_AUDIT_JOURNAL_MAX_MB` | `1024` | Cap while the DB is unreachable; past it the oldest records are dropped (logged as critical) |
| `GATEWAY_DB_KEY_CACHE_SECONDS` | `30` | How long a validated DB-backed API key is remembered (no DB query per request). Revoking a key is immediate in the gateway process that handled the revoke; other processes follow within this time. `0` disables |
| `GATEWAY_DB_AUDIT_SPILL_PATH` | `data/audit-spill.jsonl` | Audit rows that can't reach the DB after retries are written here and replayed on startup; watch `gateway_audit_write_failures_total` |
| `GATEWAY_GUARD_ENABLED` | `false` | Enable guard model shadow analysis |
| `GATEWAY_GUARD_MODEL_NAME` | `ibm/granite3.2-guardian:5b` | Guard model name |
| `GATEWAY_GUARD_BASE_URL` | `http://localhost:11434` | Ollama server hosting guard model |
| `GATEWAY_PII_ENABLED` | `false` | Enable PII detection. Also redacts PII from stored audit bodies and security scans, even when scrubbing is off |
| `GATEWAY_PII_SCRUB_ENABLED` | `false` | Replace PII with placeholders. Startup default only: admins can change scrubbing (on/off, all or selected routes) live from the dashboard's Security tab, and that saved setting overrides this |
| `GATEWAY_ADMIN_API_KEY` | | Operator key for the dashboard, key management, budgets, and security labeling. Required for those routes when `auth.enabled: true` |
| `GATEWAY_DEV_MODE` | `false` | Test mode: keyless inference and dashboard from `GATEWAY_DEV_NETWORKS`, whatever the auth config says. Keys that are sent are still checked. Never in production |
| `GATEWAY_DEV_NETWORKS` | `127.0.0.0/8,::1/128` | Comma-separated addresses or CIDRs test mode accepts keyless requests from |
| `GATEWAY_PROFILE` | `default` | `production` refuses to start with test mode on, auth off, no admin key, or unrestricted keyless access |
| `GATEWAY_DB_RETENTION_DAYS` | `90` | Delete audit rows and budget history older than this (`0` = keep) |
| `GATEWAY_SECURITY_STORE_MESSAGES` | `none` | What security scans keep of the prompt (D-041). `none`: verdicts only. `flagged`: messages of flagged requests, for review and labeling. `all`: every request (training-data collection). Kept messages are always PII-redacted |
| `GATEWAY_SECURITY_RETENTION_DAYS` | `90` | Delete security scans and PII events older than this (`0` = keep) |
| `GATEWAY_CORS_ORIGINS` | `["*"]` | Allowed CORS origins |
| `GATEWAY_REDIS_URL` | | **Optional.** Share rate limits and concurrency slots across gateway processes or replicas (e.g. `redis://:password@host:6379/0`). Unset, everything stays in process memory and nothing else needs to run. If Redis becomes unreachable, each process falls back to its own limits and `/health` reports `shared_state.status: degraded` |
| `GATEWAY_REDIS_PREFIX` | `devmesh` | Key prefix, so several gateways can share one Redis |

## Production Deployment

For evaluation, `./start-gateway.sh` is all you need. For production:

- **Process management** — Run behind systemd or supervisor. The startup script works as an `ExecStart` target.
- **Database** — Switch from SQLite to PostgreSQL for concurrent access: `GATEWAY_DB_URL=postgresql+asyncpg://user:pass@host/gateway`
- **Reverse proxy** — Put nginx or Caddy in front for TLS termination. The gateway runs HTTP on port 8001.
- **Backups** — If using SQLite, back up `data/gateway.db`. If PostgreSQL, use `pg_dump` on your schedule.
- **Upgrades** — The gateway migrates its database on startup: a new database is created at the latest schema, an older one is upgraded in place (including databases made before migrations ran at startup), and a database from a newer gateway is refused. Take a backup first. To migrate as a separate deployment step (e.g. before starting several replicas), run `gateway-migrate` with the same `GATEWAY_DB_URL`; `alembic current` shows the revision.
- **Log retention** — `GATEWAY_DB_RETENTION_DAYS=90` auto-deletes old audit records. Adjust based on compliance requirements.
- **Docker Compose** — `GATEWAY_ADMIN_API_KEY=<operator key> docker compose up -d` starts the gateway, dashboard, Prometheus, and Grafana. Put your `gateway.yaml` (with `auth.enabled: true`) in `./config`. The database and audit log live in the `gateway-data` volume, so they survive upgrades and container replacement; back up that volume. Open the dashboard at `http://<host>:5174`: it proxies the API itself, so it works from any machine.
- **More than one gateway process** — By default rate limits and `max_concurrent` slots are kept per process, so two processes would each allow the full limit. To share them, install the extra (`pip install 'devmesh-gateway[redis]'`; the Docker image already has it) and set `GATEWAY_REDIS_URL`. With Compose: `GATEWAY_REDIS_URL=redis://redis:6379/0 docker compose --profile ha up -d`. A single process needs none of this.

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
pytest tests/ -v              # ~915 tests
pytest tests/ --cov=gateway   # With coverage
```

PostgreSQL and Redis tests run when a server is reachable (`GATEWAY_TEST_PG_URL`, `GATEWAY_TEST_REDIS_URL`) and are skipped otherwise. CI runs them against real servers and fails if they're missing (`GATEWAY_TEST_REQUIRE_SERVICES=1`).

## License

MIT License — see [LICENSE](LICENSE) for details.
