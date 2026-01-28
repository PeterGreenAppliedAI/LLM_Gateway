# DevMesh Gateway - Next Steps & Roadmap

This document outlines the immediate next steps, deployment strategy, and future enhancements for DevMesh Gateway.

---

## 1. Deployment as a Proxy ("Sitting in the Middle")

### How It Works

The gateway acts as a **reverse proxy** for LLM APIs:

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Your Apps      │────▶│  DevMesh Gateway │────▶│  LLM Providers  │
│  (unchanged)    │     │  (the control    │     │  - Ollama       │
│                 │◀────│   plane)         │◀────│  - OpenAI       │
│  - Internal     │     │                  │     │  - Anthropic    │
│  - Claude Code  │     │  localhost:8000  │     │  - vLLM         │
│  - Cursor       │     │                  │     │  - etc          │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

### Configuration for Existing Tools

**Option A: Environment Variable (Most Tools)**

Most OpenAI-compatible tools support `OPENAI_BASE_URL`:

```bash
# Point tools at the gateway instead of OpenAI directly
export OPENAI_BASE_URL=http://gateway-vm:8000/v1
export OPENAI_API_KEY=your-gateway-api-key

# Now run your tool normally - requests go through gateway
cursor .
claude-code
python my_script.py
```

**Option B: SDK Configuration**

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://gateway-vm:8000/v1",
    api_key="your-gateway-api-key"
)

# Requests now routed through gateway
response = client.chat.completions.create(
    model="ollama/phi4:14b",  # or "openai/gpt-4"
    messages=[{"role": "user", "content": "Hello"}]
)
```

### Network Topology Options

**Option 1: Same Network (Simplest)**
```
[Dev Machine] ──────▶ [Gateway VM:8000] ──────▶ [Ollama Server]
                                       ──────▶ [OpenAI API]
```

**Option 2: VPN/Tailscale**
```
[Remote Laptop] ──VPN──▶ [Gateway VM:8000] ──────▶ [Internal Ollama]
                                           ──────▶ [Cloud APIs]
```

**Option 3: Reverse Proxy with TLS (Production)**
```
[Internet] ──HTTPS──▶ [nginx/caddy] ──▶ [Gateway:8000] ──▶ [Backends]
```

---

## 2. Cloud Provider Integration (OpenAI, Anthropic, etc.)

### Why This Works Out-of-the-Box

All major LLM providers use **OpenAI-compatible APIs**:

| Provider | API Compatibility | Base URL |
|----------|------------------|----------|
| OpenAI | Native | `https://api.openai.com` |
| Anthropic | OpenAI-compatible mode | `https://api.anthropic.com` |
| Google Gemini | OpenAI-compatible | `https://generativelanguage.googleapis.com` |
| Groq | OpenAI-compatible | `https://api.groq.com/openai` |
| Together AI | OpenAI-compatible | `https://api.together.xyz` |
| Fireworks | OpenAI-compatible | `https://api.fireworks.ai/inference` |

### Adding Cloud Providers

Create an `openai` adapter type (already architected for this):

```yaml
# config/providers.yaml
providers:
  # Local runtime
  - name: ollama
    type: ollama
    base_url: http://192.168.1.216:11434
    enabled: true

  # OpenAI
  - name: openai
    type: openai
    base_url: https://api.openai.com
    api_key: ${OPENAI_API_KEY}
    enabled: true
    models:
      - gpt-4
      - gpt-4-turbo
      - gpt-3.5-turbo

  # Anthropic (via OpenAI-compatible endpoint)
  - name: anthropic
    type: openai  # Uses same adapter!
    base_url: https://api.anthropic.com/v1
    api_key: ${ANTHROPIC_API_KEY}
    headers:
      anthropic-version: "2024-01-01"
    enabled: true
    models:
      - claude-3-opus
      - claude-3-sonnet

  # Groq (fast inference)
  - name: groq
    type: openai
    base_url: https://api.groq.com/openai
    api_key: ${GROQ_API_KEY}
    enabled: true
    models:
      - llama-3.1-70b-versatile
      - mixtral-8x7b-32768
```

### Implementation Required

```python
# src/gateway/adapters/openai.py (new file)
class OpenAIAdapter(BaseAdapter):
    """Adapter for OpenAI-compatible APIs (OpenAI, Anthropic, Groq, etc.)"""

    async def chat(self, request: InternalRequest) -> InternalResponse:
        # Forward request to cloud API
        # Handle auth headers
        # Normalize response
        pass
```

**Estimated effort**: 2-4 hours (straightforward since we already have the pattern)

---

## 3. Frontend Dashboard Options

### Option A: Grafana (Recommended for v0)

**Pros**:
- Already have Prometheus metrics
- Production-ready
- No custom code needed

**Setup**:
```yaml
# docker-compose.yaml addition
services:
  prometheus:
    image: prom/prometheus
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana
    ports:
      - "3000:3000"
    volumes:
      - ./monitoring/grafana/dashboards:/var/lib/grafana/dashboards
```

**Dashboard shows**:
- Requests per second by provider/model
- Latency histograms
- Token usage
- Error rates
- Provider health status

### Option B: Custom React Dashboard (v1+)

For a branded DevMesh experience:

```
┌─────────────────────────────────────────────────────────┐
│  DevMesh Gateway Dashboard                              │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ Requests/hr │  │ Avg Latency │  │ Token Usage │     │
│  │   12,456    │  │   234ms     │  │  1.2M       │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
│                                                         │
│  Provider Status                                        │
│  ┌─────────────────────────────────────────────────┐   │
│  │ ● Ollama      Healthy    2,341 req/hr           │   │
│  │ ● OpenAI      Healthy    8,923 req/hr           │   │
│  │ ○ Anthropic   Degraded   1,192 req/hr           │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  Recent Requests                  Cost Breakdown        │
│  ┌──────────────────────────┐    ┌─────────────────┐   │
│  │ ...                      │    │  OpenAI: $45.23 │   │
│  │ ...                      │    │  Groq:   $2.10  │   │
│  └──────────────────────────┘    └─────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

**Tech Stack**:
- React + TypeScript
- TailwindCSS
- Recharts/Tremor for visualizations
- API: Gateway `/metrics` + new `/v1/devmesh/stats` endpoint

### Option C: Terminal UI (Quick Win)

```bash
# Simple CLI dashboard
watch -n 1 'curl -s localhost:8000/health | jq'

# Or with rich TUI
pip install rich
python -m gateway.cli dashboard
```

---

## 4. Implementation Roadmap (Prioritized)

### Phase 1: Cloud Provider Support (OpenAI Adapter) ✅ COMPLETE
**Priority: HIGH | Effort: 2-4 hours**

- [x] Create `src/gateway/providers/openai.py` adapter
- [x] Support OpenAI, Anthropic, Groq, Together AI, OpenRouter (all OpenAI-compatible)
- [x] Handle API key forwarding and custom headers
- [x] Auto-detect API keys by provider name
- [x] Update config examples
- [x] 28 tests in `tests/test_openai_adapter.py`

**Completed**: Full OpenAI-compatible adapter with streaming support.

### Phase 2a: Dashboard REST APIs ✅ COMPLETE
**Priority: HIGH | Effort: 4-6 hours**

- [x] `GET /api/stats` - Overall usage statistics
- [x] `GET /api/requests` - Recent requests (paginated, filterable)
- [x] `GET /api/requests/{id}` - Single request details
- [x] `GET /api/usage/daily` - Daily aggregated usage
- [x] `GET /api/models/usage` - Per-model breakdown
- [x] `GET /api/endpoints/usage` - Per-endpoint breakdown
- [x] `POST /api/usage/aggregate` - Manual aggregation trigger
- [x] Usage aggregation job (audit_log → usage_daily)
- [x] 17 tests in `tests/test_dashboard_api.py`

**Completed**: Full REST API for dashboard with filtering, pagination, and aggregation.

### Phase 2b: Custom Frontend Dashboard ✅ PARTIAL
**Priority: HIGH | Effort: 1-2 weeks**

- [x] React + TypeScript + TailwindCSS + Vite
- [x] Real-time request monitoring (polling)
- [x] Provider health status
- [x] Usage metrics display
- [x] Request detail viewer (clickable rows with TTFT, tokens/sec)
- [ ] Error log viewer
- [ ] **API Key Management UI** (see below)
- [ ] Cost tracking display
- [ ] Usage charts (Recharts/Tremor)

**Why custom over Grafana**: Simpler deployment, better UX for non-technical users, branded experience, single deployable unit.

#### API Key Management UI (Priority Feature)

**Goal**: Generate API keys from the dashboard that route to specific endpoints/models with auto-generated usage instructions.

**User Flow**:
1. Click "Create API Key" in dashboard
2. Configure:
   - Application name (client_id): e.g., "electrical-estimator"
   - Target endpoint (optional): e.g., "gpu-node-3060"
   - Allowed models (optional): e.g., "phi4:*", "llama3.2:*"
   - Rate limits / quotas (optional)
3. Generate key
4. Display usage instructions with copy buttons:
   - Environment variable setup
   - cURL example
   - Python OpenAI SDK snippet
   - Shows which endpoint/models this key routes to

**Backend Requirements**:
- [ ] `POST /api/keys` - Create new API key (returns key + instructions)
- [ ] `GET /api/keys` - List keys (key masked, shows client_id, target_endpoint)
- [ ] `DELETE /api/keys/{key_id}` - Revoke key
- [ ] `PATCH /api/keys/{key_id}` - Update key config (not the key itself)
- [ ] Keys stored in `api_keys` table (schema exists)

**Frontend Requirements**:
- [ ] Key creation modal with form
- [ ] Key list table with actions
- [ ] Usage instructions generator (templated based on key config)
- [ ] Copy-to-clipboard for all code snippets

### Phase 3: Database & Audit Logging ✅ COMPLETE
**Priority: HIGH | Effort: 8-10 hours**

- [x] SQLAlchemy Core schema (database-agnostic)
- [x] Audit log table (every request with full metrics)
- [x] Usage aggregates table (daily rollups) - schema ready
- [x] API keys table (DB-managed keys) - schema ready
- [x] SQLite default, PostgreSQL production-ready
- [x] AuditLogger integration with all routes
- [x] Privacy controls (store_request_body/store_response_body)
- [x] 27 tests in `tests/test_storage.py`

**Files created**:
- `src/gateway/storage/schema.py` - Database schema
- `src/gateway/storage/engine.py` - SQLite/PostgreSQL engine
- `src/gateway/storage/audit.py` - AuditLogger with async support

**Completed**: Full audit logging with query methods (get_recent_requests, get_stats).

### Phase 4: Production Hardening
**Priority: MEDIUM | Effort: 4-8 hours**

- [ ] Docker Compose (gateway + frontend + db)
- [ ] TLS termination guide (nginx/caddy)
- [ ] Request body size limits
- [ ] Prompt redaction option
- [ ] Health check improvements

### Phase 5: Prompt Injection Defense ✅ PARTIAL
**Priority: HIGH | Effort: Ongoing**

Security module implemented in `src/gateway/security/`:

**Completed (Zero-Latency Defenses):**
- [x] Unicode sanitization - strips invisible chars (zero-width, directional, BOM)
- [x] Content wrapping - marks untrusted content with trust-level tags
- [x] Pattern detection - logs suspicious injection patterns
- [x] Async analyzer - background analysis without blocking requests
- [x] 31 tests in `tests/test_security.py`

**Not Yet Integrated:**
- [ ] Wire sanitization into request pipeline
- [ ] Wire async analyzer into routes
- [ ] Add security alerts API endpoint
- [ ] Dashboard security alerts view

---

## 5. Prompt Injection Defense - Design Decisions

### The Problem

Attackers hide malicious instructions in:
- PR diffs and code reviews
- Documents being summarized
- User-provided content
- External API responses

### Defense Options Comparison

| Approach | Latency | Effectiveness | Bypass Difficulty | Implemented |
|----------|---------|---------------|-------------------|-------------|
| Pattern blocklist | ~1ms | Low | Easy (rephrase) | ✅ Logging only |
| Unicode sanitization | ~0ms | Low | Medium | ✅ Yes |
| Content wrapping | ~0ms | Medium | Medium | ✅ Yes |
| Fast classifier | ~10ms | Medium-High | Hard | ❌ Future |
| Guard LLM | ~500ms | High | Very Hard | ❌ Future |
| Async analysis | 0ms* | Visibility | N/A | ✅ Yes |

*Async = doesn't block request

### Why Pattern Matching Alone Fails

| You Block | Attacker Uses |
|-----------|---------------|
| "ignore previous instructions" | "let's start fresh with new context" |
| "disregard" | "pretend the above was a test" |
| English patterns | Instructions in other languages |
| Direct commands | Roleplay scenarios, fictional framing |

**Conclusion**: Pattern matching is whack-a-mole. It's useful for **logging/alerting** but not as primary defense.

### Recommended Layered Approach

```
┌─────────────────────────────────────────────────────┐
│  Layer 1: Unicode Sanitization (0ms)                │
│  - Strip zero-width, directional, control chars     │
│  - Catches encoding tricks                          │
├─────────────────────────────────────────────────────┤
│  Layer 2: Content Wrapping (0ms)                    │
│  - Mark untrusted content with trust-level tags     │
│  - Structural defense, relies on model compliance   │
├─────────────────────────────────────────────────────┤
│  Layer 3: Pattern Logging (1ms)                     │
│  - Detect known patterns, LOG don't block           │
│  - Build dataset of attack attempts                 │
├─────────────────────────────────────────────────────┤
│  Layer 4: Async Analysis (0ms request latency)      │
│  - Background scanning after request completes      │
│  - Alert on suspicious patterns                     │
├─────────────────────────────────────────────────────┤
│  Layer 5: Guard LLM (optional, +500ms)              │
│  - Semantic injection detection                     │
│  - Opt-in for high-risk use cases only              │
└─────────────────────────────────────────────────────┘
```

### Future: Guard LLM Integration

For high-security use cases (processing untrusted PRs, external documents):

```python
# Opt-in via API key policy
api_keys:
  - key: "pr-reviewer-key"
    client_id: pr-reviewer
    policies:
      injection_guard: true  # Enable guard LLM
      guard_model: "llama3.2:3b"  # Fast local model
```

**Pros:**
- Understands semantic meaning, not just patterns
- Much harder to bypass
- Can use small, fast local model

**Cons:**
- Adds ~200-500ms latency per request
- Consumes GPU resources
- Not foolproof (guard can be fooled too)

**Recommendation**: Make it opt-in per API key, not global.

### Future: Fast Classifier

Train a small BERT-style classifier specifically for injection detection:

```python
from transformers import pipeline
classifier = pipeline("text-classification", model="injection-detector")
result = classifier("Ignore previous instructions")
# {"label": "INJECTION", "score": 0.97} in ~10ms
```

**Pros:**
- Much faster than LLM (~10ms vs ~500ms)
- Purpose-built for the task
- Can run on CPU

**Cons:**
- Requires training data (attack examples)
- May need periodic retraining as attacks evolve
- Less flexible than LLM

### Current Implementation Details

**Files:**
- `src/gateway/security/sanitizer.py` - Unicode sanitization
- `src/gateway/security/injection.py` - Pattern detection + content wrapping
- `src/gateway/security/analyzer.py` - Async background analysis

**Pattern Categories Detected:**
- `instruction_override` - "ignore previous instructions"
- `delimiter_attack` - fake `<|system|>` tags
- `roleplay_escape` - "you are now DAN"
- `encoding_tricks` - base64/rot13 indicators
- `context_manipulation` - "real instructions are..."

**Content Wrapper Usage:**
```python
from gateway.security import ContentWrapper

wrapper = ContentWrapper()

# For PR diffs
safe = wrapper.wrap_pr_diff(diff, pr_info={"number": 123})

# For documents
safe = wrapper.wrap_document(doc, source="external-api")

# Generic
safe = wrapper.wrap(content, trust_level="UNTRUSTED")
```

---

### Future (v1.0+)

- [ ] **Plugin architecture** for request/response transformation
  - PII scrubbing (SSN, credit cards, emails, names, phone numbers)
  - NER detection and redaction
  - Prompt injection detection
  - Content filtering
  - Response validation
  - Config-driven plugin chain
- [ ] Hybrid routing (cheap tasks → local, expensive → cloud)
- [ ] Cost estimation per request
- [ ] Multi-tenant support
- [ ] Webhook notifications
- [ ] Request/response caching
- [ ] Model evaluation harness

---

## 9. Audit Log Persistence (Database)

### Why Add a Database?

Currently the gateway is fully stateless - logs go to stdout and disappear. For production use, you need:
- Request history / audit trail
- Usage analytics over time
- Compliance reporting
- Cost tracking per user/team

### Database-Agnostic Design

Use SQLAlchemy Core (not ORM) for maximum portability:

```python
# src/gateway/storage/schema.py
from sqlalchemy import (
    MetaData, Table, Column, String, Integer, Float,
    DateTime, Text, JSON, Index, create_engine
)
from datetime import datetime

metadata = MetaData()

# Audit log - every request
audit_log = Table(
    'audit_log', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('request_id', String(64), unique=True, nullable=False),
    Column('timestamp', DateTime, default=datetime.utcnow, nullable=False),

    # Who
    Column('client_id', String(128), nullable=False),
    Column('user_id', String(128), nullable=True),

    # What
    Column('task', String(32), nullable=False),  # chat, completion, embeddings
    Column('model', String(128), nullable=False),
    Column('provider', String(64), nullable=False),

    # How it went
    Column('status', String(16), nullable=False),  # success, error, rate_limited
    Column('error_code', String(64), nullable=True),
    Column('error_message', Text, nullable=True),

    # Performance metrics
    Column('latency_ms', Float, nullable=True),           # Total request time
    Column('time_to_first_token_ms', Float, nullable=True),  # TTFT for streaming
    Column('tokens_per_second', Float, nullable=True),    # Generation throughput

    # Token usage
    Column('prompt_tokens', Integer, default=0),
    Column('completion_tokens', Integer, default=0),
    Column('total_tokens', Integer, default=0),

    # Cost (if configured)
    Column('estimated_cost_usd', Float, nullable=True),

    # Optional: store request/response (configurable, off by default)
    Column('request_body', JSON, nullable=True),
    Column('response_body', JSON, nullable=True),

    # Indexes for common queries
    Index('ix_audit_timestamp', 'timestamp'),
    Index('ix_audit_client', 'client_id'),
    Index('ix_audit_user', 'user_id'),
    Index('ix_audit_model', 'model'),
    Index('ix_audit_provider', 'provider'),
)

# Usage aggregates (for dashboards, computed periodically)
usage_daily = Table(
    'usage_daily', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('date', DateTime, nullable=False),
    Column('client_id', String(128), nullable=False),
    Column('user_id', String(128), nullable=True),
    Column('provider', String(64), nullable=False),
    Column('model', String(128), nullable=False),

    Column('request_count', Integer, default=0),
    Column('error_count', Integer, default=0),
    Column('total_prompt_tokens', Integer, default=0),
    Column('total_completion_tokens', Integer, default=0),
    Column('total_cost_usd', Float, default=0.0),
    Column('avg_latency_ms', Float, nullable=True),
    Column('p95_latency_ms', Float, nullable=True),
    Column('avg_ttft_ms', Float, nullable=True),          # Avg time to first token
    Column('avg_tokens_per_second', Float, nullable=True), # Avg throughput

    Index('ix_usage_date', 'date'),
    Index('ix_usage_client_date', 'client_id', 'date'),
)

# API keys (if managing keys in DB instead of config)
api_keys = Table(
    'api_keys', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('key_hash', String(128), unique=True, nullable=False),  # SHA256 hash, not plaintext
    Column('name', String(128), nullable=False),
    Column('client_id', String(128), nullable=False),
    Column('created_at', DateTime, default=datetime.utcnow),
    Column('expires_at', DateTime, nullable=True),
    Column('is_active', Integer, default=1),  # Use Integer for SQLite compatibility
    Column('rate_limit_rpm', Integer, nullable=True),
    Column('allowed_models', JSON, nullable=True),  # ["ollama/*", "openai/gpt-4"]

    Index('ix_apikeys_client', 'client_id'),
)
```

### Connection Configuration

```yaml
# config/gateway.yaml
database:
  # SQLite (default, zero config)
  url: "sqlite:///./data/gateway.db"

  # PostgreSQL
  # url: "postgresql://user:pass@localhost:5432/gateway"

  # MySQL/MariaDB
  # url: "mysql+pymysql://user:pass@localhost:3306/gateway"

  # Connection pool settings (ignored for SQLite)
  pool_size: 5
  max_overflow: 10

  # What to store
  store_request_body: false   # Privacy: don't store prompts by default
  store_response_body: false  # Privacy: don't store completions by default
```

### Implementation

```python
# src/gateway/storage/engine.py
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool, NullPool
from gateway.storage.schema import metadata

def create_db_engine(config):
    """Create database engine with appropriate pooling."""
    url = config.database.url

    # SQLite doesn't support connection pooling
    if url.startswith('sqlite'):
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=NullPool
        )
    else:
        engine = create_engine(
            url,
            poolclass=QueuePool,
            pool_size=config.database.pool_size,
            max_overflow=config.database.max_overflow,
        )

    # Create tables if they don't exist
    metadata.create_all(engine)
    return engine
```

```python
# src/gateway/storage/audit.py
from sqlalchemy import insert
from gateway.storage.schema import audit_log

class AuditLogger:
    """Async-compatible audit logger."""

    def __init__(self, engine, store_bodies: bool = False):
        self.engine = engine
        self.store_bodies = store_bodies

    async def log_request(
        self,
        request_id: str,
        client_id: str,
        user_id: str | None,
        task: str,
        model: str,
        provider: str,
        status: str,
        latency_ms: float,
        time_to_first_token_ms: float | None,
        tokens_per_second: float | None,
        prompt_tokens: int,
        completion_tokens: int,
        error_code: str | None = None,
        error_message: str | None = None,
        request_body: dict | None = None,
        response_body: dict | None = None,
        estimated_cost_usd: float | None = None,
    ):
        """Log a request to the audit table."""
        stmt = insert(audit_log).values(
            request_id=request_id,
            client_id=client_id,
            user_id=user_id,
            task=task,
            model=model,
            provider=provider,
            status=status,
            latency_ms=latency_ms,
            time_to_first_token_ms=time_to_first_token_ms,
            tokens_per_second=tokens_per_second,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            error_code=error_code,
            error_message=error_message,
            request_body=request_body if self.store_bodies else None,
            response_body=response_body if self.store_bodies else None,
            estimated_cost_usd=estimated_cost_usd,
        )

        # Run in thread pool to not block async
        with self.engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()
```

### Migration Path

SQLite → PostgreSQL/MySQL is straightforward:

```bash
# 1. Export from SQLite
sqlite3 data/gateway.db .dump > backup.sql

# 2. Update config
database:
  url: "postgresql://user:pass@localhost:5432/gateway"

# 3. Restart gateway (tables auto-created)

# 4. Import data (adjust SQL syntax as needed)
psql gateway < backup.sql
```

Or use a migration tool like Alembic for schema versioning.

### Estimated Effort

| Task | Effort |
|------|--------|
| Schema + engine setup | 2-3 hrs |
| AuditLogger integration | 2-3 hrs |
| Config changes | 1 hr |
| Tests | 2-3 hrs |
| **Total** | ~8-10 hrs |

---

## 5. VM Deployment Checklist

```bash
# On your VM

# 1. Install Docker
curl -fsSL https://get.docker.com | sh

# 2. Clone the repo
git clone <repo-url> /opt/devmesh-gateway
cd /opt/devmesh-gateway

# 3. Configure providers
cp config/providers.yaml.example config/providers.yaml
vim config/providers.yaml  # Add your provider URLs and API keys

# 4. Set secrets
echo "OPENAI_API_KEY=sk-..." > .env
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
echo "GATEWAY_API_KEY=your-secure-key" >> .env

# 5. Start the stack
docker-compose up -d

# 6. Verify
curl http://localhost:8000/health

# 7. Configure firewall (allow from internal network only)
ufw allow from 192.168.1.0/24 to any port 8000

# 8. Point your apps at http://vm-ip:8000/v1
```

---

## 6. Testing the Proxy Setup

```bash
# 1. Start gateway locally
GATEWAY_CONFIG_PATH=config/gateway.yaml \
GATEWAY_PROVIDERS_CONFIG_PATH=config/providers.yaml \
uvicorn gateway.main:app --host 0.0.0.0 --port 8000

# 2. Test direct curl
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"ollama/phi4:14b","messages":[{"role":"user","content":"hi"}]}'

# 3. Test with OpenAI SDK pointing at gateway
export OPENAI_BASE_URL=http://localhost:8000/v1
export OPENAI_API_KEY=test-key
python -c "
from openai import OpenAI
c = OpenAI()
r = c.chat.completions.create(model='ollama/phi4:14b', messages=[{'role':'user','content':'hi'}])
print(r.choices[0].message.content)
"

# 4. Check metrics
curl http://localhost:8000/metrics | grep gateway_
```

---

## 7. Questions to Answer

Before proceeding, decide:

1. **Where will the gateway VM live?** (Same network as Ollama? DMZ? Cloud?)
2. **Do you need TLS?** (Internal network might not, external access does)
3. **Which cloud providers to add first?** (OpenAI most common)
4. **Grafana vs custom dashboard?** (Grafana faster, custom more branded)
5. **Multi-user?** (Need auth key management)

---

## 8. Integration with Open WebUI, LibreChat, etc.

### The Gateway Works as a Drop-in Proxy

Any tool that supports OpenAI-compatible APIs (which is almost everything) can use the gateway:

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Open WebUI     │────▶│  DevMesh Gateway │────▶│  Ollama         │
│  localhost:3000 │     │  localhost:8000  │     │  192.168.1.216  │
│                 │◀────│                  │◀────│                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                              │
                              └────────────────▶ OpenAI/Anthropic/etc
```

### Open WebUI Configuration

```bash
# In your Open WebUI docker-compose.yaml or .env
OLLAMA_BASE_URL=http://gateway-ip:8000   # Point at gateway, not Ollama directly
```

Or if using docker-compose:
```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    environment:
      - OLLAMA_BASE_URL=http://gateway:8000
    depends_on:
      - gateway
```

### LibreChat Configuration

```yaml
# librechat.yaml
endpoints:
  custom:
    - name: "DevMesh Gateway"
      apiKey: "${GATEWAY_API_KEY}"
      baseURL: "http://gateway-ip:8000/v1"
      models:
        default: ["ollama/phi4:14b", "openai/gpt-4"]
```

### What This Enables

1. **Unified Metrics**: All requests from Open WebUI flow through gateway - visible in one place
2. **Model Switching**: Users can request `ollama/phi4:14b` or `openai/gpt-4` - gateway routes appropriately
3. **Rate Limiting**: Prevent runaway costs even from chat UIs
4. **Fallback**: If Ollama is down, automatically route to cloud provider

### Example Full Stack

```yaml
# docker-compose.yaml
services:
  gateway:
    build: .
    ports:
      - "8000:8000"
    environment:
      - GATEWAY_CONFIG_PATH=/config/gateway.yaml
      - OPENAI_API_KEY=${OPENAI_API_KEY}
    volumes:
      - ./config:/config

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    ports:
      - "3000:8080"
    environment:
      - OLLAMA_BASE_URL=http://gateway:8000
    depends_on:
      - gateway

  prometheus:
    image: prom/prometheus
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana
    ports:
      - "3001:3000"
```

---

## Summary

| Phase | Task | Effort | Priority | Status |
|-------|------|--------|----------|--------|
| **1** | OpenAI adapter (cloud providers) | 2-4 hrs | **HIGH** | ✅ DONE |
| **2a** | Dashboard REST APIs | 4-6 hrs | **HIGH** | ✅ DONE |
| **2b** | Custom React frontend | 1-2 weeks | **HIGH** | ⏳ Pending |
| **3** | Database + audit logging | 8-10 hrs | **HIGH** | ✅ DONE |
| **4** | Docker Compose + production hardening | 4-8 hrs | Medium | ⏳ Pending |
| - | Open WebUI / LibreChat integration | 15 min | As needed | Ready to use |
| - | Hybrid routing | 4-8 hrs | Future | ⏳ Pending |
| - | Multi-tenant | 1-2 weeks | v1.0 | ⏳ Pending |
