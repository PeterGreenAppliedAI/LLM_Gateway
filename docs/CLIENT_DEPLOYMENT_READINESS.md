# Client Deployment Readiness Assessment

## What This Gateway Becomes in Client Infrastructure

A **local AI service mesh / API gateway** that sits between:
- Client apps/workflows (Zapier/Make replacements, internal tools, RAG apps, chat frontends)
- Their model runtimes (Ollama/vLLM/TGI/etc) and optionally external APIs

**The product isn't the dashboard. The product is:**
- Consistent API surface
- Routing + policy enforcement
- Observability
- Governance
- Reliability in their environment

---

## Deployment Modes

### Mode A: "Local-only"
- Endpoints are only internal runtimes
- No external egress allowed
- Policies default to "deny" for outbound
- **Current Support: READY** - Works today

### Mode B: "Hybrid"
- Local endpoints + fallback to external (OpenAI/Anthropic/etc)
- Strong PII scrubbing + allowlist domains + audit
- **Current Support: PARTIAL** - External providers work, but no PII scrubbing or egress controls

---

## Current State Assessment

### 1. State Model

| Item | Status | Location |
|------|--------|----------|
| Config file loading (YAML) | **EXISTS** | `src/gateway/config.py` |
| Database storage (audit, usage, keys) | **EXISTS** | `src/gateway/storage/schema.py` |
| Supported DBs | **EXISTS** | SQLite, PostgreSQL, MySQL |
| Migration framework | **MISSING** | No Alembic - uses auto-create |
| Config hot-reload | **MISSING** | Restart required |

### 2. Auth & Per-Key Limits

| Item | Status | Location |
|------|--------|----------|
| API key authentication | **EXISTS** | `routes/dependencies.py:152-225` |
| Bearer + X-API-Key header | **EXISTS** | Dual auth methods |
| Timing-safe comparison | **EXISTS** | `secrets.compare_digest()` |
| Per-key environment scoping | **EXISTS** | Keys link to dev/prod |
| Global rate limiting | **EXISTS** | `policy/rate_limiter.py` |
| Per-key rate limits | **PARTIAL** | Schema exists, enforcement missing |
| Per-key allowed_models | **PARTIAL** | Schema exists, not enforced |
| Per-key allowed_endpoints | **PARTIAL** | Schema exists, not enforced |
| Per-key quotas | **PARTIAL** | Schema exists, not enforced |
| Key rotation | **MISSING** | Manual config update |
| DB-backed key validation | **MISSING** | Only config-file keys work |

### 3. Policy Engine

| Item | Status | Location |
|------|--------|----------|
| Rate limiting | **EXISTS** | `policy/enforcer.py` |
| Token limits per request | **EXISTS** | `policy/token_limiter.py` |
| Task-provider authorization | **EXISTS** | Declarative in code |
| Policies in config file | **MISSING** | Hardcoded in Python |
| Policy hot-reload | **MISSING** | Requires restart |

### 4. Operability (Day 2)

| Item | Status | Location |
|------|--------|----------|
| Health endpoint | **EXISTS** | `GET /health` with provider status |
| Structured JSON logging | **EXISTS** | `observability/logging.py` |
| Configurable log level | **EXISTS** | `GATEWAY_LOG_LEVEL` env var |
| Prometheus metrics | **EXISTS** | `GET /metrics` |
| Grafana dashboards | **EXISTS** | `docker-compose.yaml` |
| Graceful shutdown | **EXISTS** | Lifespan context manager |
| Config hot-reload | **MISSING** | No watch/reload endpoint |
| Liveness probe | **EXISTS** | `/health` endpoint |
| Readiness probe | **EXISTS** | `/health` includes provider status |

### 5. Security Posture

| Item | Status | Location |
|------|--------|----------|
| API key auth | **EXISTS** | Format validation + timing-safe |
| Audit logs with trace IDs | **EXISTS** | UUID4 request_id in all logs |
| Input validation | **EXISTS** | Safe identifiers, Pydantic |
| SecretStr for sensitive fields | **EXISTS** | Prevents accidental logging |
| TLS/mTLS | **MISSING** | Must use reverse proxy |
| Secrets vault integration | **MISSING** | Plaintext in YAML config |
| Egress controls/allowlist | **MISSING** | No domain filtering |

### 6. Packaging

| Item | Status | Location |
|------|--------|----------|
| Dockerfile | **EXISTS** | `docker/Dockerfile` (non-root, healthcheck) |
| Docker Compose | **EXISTS** | Gateway + Prometheus + Grafana |
| Environment variable config | **EXISTS** | `GATEWAY_*` prefix, comprehensive |
| Helm charts | **MISSING** | No Kubernetes manifests |
| Install scripts | **MISSING** | Manual setup required |
| Health verification script | **MISSING** | No automated checks |

---

## The 5 Non-Negotiables for Client Shipping

### 1. Tenancy and Identity ✅ PARTIAL
Even single-tenant needs internal separation:
- [x] org/project keys (schema exists)
- [ ] per-key quotas and limits (schema exists, enforcement missing)
- [ ] per-key allowed models/endpoints (schema exists, enforcement missing)
- [ ] per-key retention rules (not implemented)

### 2. Policy Engine as Config ❌ MISSING
Clients will demand:
- "this app can't call external models"
- "this user can't use model X"
- "redact/strip PII before any egress"
- "log prompts but not responses"

These must be declarative and auditable. Currently hardcoded in Python.

### 3. Operability Package (Day 2) ✅ MOSTLY READY
- [x] Logs/metrics (Prometheus, structured JSON)
- [x] Health checks
- [x] Graceful shutdown
- [ ] Config hot-reload
- [ ] Upgrade path documentation
- [ ] Secret rotation guide
- [ ] HA deployment guide
- [ ] Backup/restore procedures

### 4. Security Posture ✅ PARTIAL
- [x] authn/authz (API keys)
- [ ] TLS/mTLS (reverse proxy required)
- [ ] outbound allowlist and egress controls
- [x] audit logs with trace IDs
- [ ] secret management (env vars only, no Vault/KMS)

### 5. Packaging + Install Story ✅ PARTIAL
- [x] Docker Compose for small shops
- [ ] Helm chart for Kubernetes shops
- [ ] Single binary option
- [ ] One-command health verification script

---

## Critical Gaps (Must Fix Before Production)

| Priority | Gap | Effort | Impact |
|----------|-----|--------|--------|
| **P0** | Per-key limit enforcement | Medium | Security, multi-tenancy |
| **P0** | TLS documentation/config | Low | Security compliance |
| **P0** | Secrets management | Medium | Security compliance |
| **P1** | Config hot-reload endpoint | Low | Operability |
| **P1** | DB-backed key validation | Medium | Key management |
| **P1** | Egress allowlist | Medium | Hybrid mode security |
| **P2** | Helm chart | Medium | K8s deployment |
| **P2** | Alembic migrations | Low | Schema versioning |
| **P2** | Policy config file | High | Flexibility |

---

## Recommended Implementation Order

### Week 1: Security & Auth Hardening
1. **Per-key enforcement** - Wire up `allowed_models`/`allowed_endpoints` checks in routes
2. **DB-backed key validation** - Use existing schema in `api_keys` table
3. **TLS documentation** - Document reverse proxy setup (nginx/Caddy)

### Week 2: Operability
4. **Config hot-reload** - Add `POST /admin/reload` endpoint
5. **Health verification script** - Check gateway, endpoints, models, latency
6. **Upgrade guide** - Document versioning and migration path

### Week 3: Hybrid Mode
7. **Egress allowlist** - Domain/IP filtering for external providers
8. **PII scrubbing hooks** - Pre-route policy action
9. **External provider adapters** - OpenAI, Anthropic, etc.

### Week 4: Packaging
10. **Helm chart skeleton** - Deployment, Service, ConfigMap, Secret
11. **Install automation** - Setup scripts for common environments
12. **Alembic migrations** - Schema versioning for upgrades

---

## Product Split Recommendation

### Gateway Core (must be stable)
- API surface (OpenAI-compatible)
- Policy enforcement
- Resolver
- Telemetry
- Endpoint adapters

### UI/Console (optional addon)
- Dashboards
- Request explorer
- Config editor (eventually)

Many clients will run **headless** and only care about metrics + logs.

---

## Target Client Profiles

### Profile A: SMB with 1-2 GPU servers
- Docker Compose deployment
- SQLite database
- Local-only mode
- Single API key per app
- Minimal ops overhead

### Profile B: Mid-market with Kubernetes
- Helm chart deployment
- PostgreSQL database
- Hybrid mode with external fallback
- Per-team API keys with quotas
- Prometheus/Grafana integration

### Profile C: MSP-managed
- Multi-tenant considerations
- Strict isolation requirements
- Audit log retention policies
- SLA monitoring
- Custom integrations

---

## What's Actually Solid Today

✅ Request pipeline with policy hooks
✅ Observability (metrics, logging, audit)
✅ Multi-endpoint routing + discovery
✅ Environment separation (dev/prod)
✅ Health monitoring
✅ OpenAI + Ollama API compatibility
✅ Streaming support with TTFT metrics
✅ Non-root Docker container
✅ Structured logging with request context

The foundation is strong. The gaps are mostly:
- "Wire up what's already in the schema"
- "Externalize what's currently in code"
- "Document what's already possible"
