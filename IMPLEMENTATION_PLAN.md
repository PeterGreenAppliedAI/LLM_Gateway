# DevMesh Gateway - Implementation Plan

## Status: v0+ (Beyond Original PRD)

The gateway has exceeded the original v0 PRD scope. This plan tracks the original phases and their current status, plus new phases for features added beyond v0.

---

## Original Phases (v0 PRD)

### Phase 1: Project Scaffolding -- COMPLETE
- FastAPI app, config loading, settings, Docker structure

### Phase 2: Core Models -- COMPLETE
- Internal normalized format, OpenAI-compatible schemas, Ollama schemas

### Phase 3: Provider Adapter Interface -- COMPLETE
- Abstract `ProviderAdapter`, Ollama adapter (full), OpenAI adapter (full)
- vLLM, TRT-LLM, SGLang stubs

### Phase 4: Routing Engine -- COMPLETE
- Config-driven routing via `Dispatcher`
- Health-aware provider selection with fallback
- Per-client endpoint pinning via API keys

### Phase 5: Policy Enforcement -- COMPLETE
- Rate limiting (global + per-user)
- Token limits
- Provider health gating

### Phase 6: Observability -- COMPLETE (with gaps)
- Structured JSON logging
- Prometheus metrics (OpenAI routes only — Ollama routes missing)
- Request context tracking

### Phase 7: API Endpoints -- COMPLETE
- OpenAI-compatible: `/v1/chat/completions`, `/v1/embeddings`
- Ollama-native: `/api/chat`, `/api/generate`, `/api/embeddings`, `/api/tags`
- DevMesh: `/health`, `/v1/devmesh/catalog`, management APIs

---

## Post-v0 Phases (Added Features)

### Phase 8: Async Database & Audit Logging -- COMPLETE
- Async SQLAlchemy with SQLite (default) and PostgreSQL support
- Audit logger with optional request/response body storage
- Usage statistics and daily aggregation

### Phase 9: Security Module -- COMPLETE (needs hardening)
- Unicode sanitization (zero latency)
- Regex-based injection pattern detection
- Content wrapping with trust markers
- Async background analysis (zero request latency)

### Phase 10: Guard Model Integration -- COMPLETE
- IBM Granite Guardian 3.2 support (jailbreak detection)
- Llama Guard 3 support (1b/8b)
- Factory pattern for auto-detection from model name
- Shadow mode (log only, no blocking)
- Confidence parsing for Granite Guardian

### Phase 11: API Key Management -- COMPLETE (needs auth hardening)
- Database-backed key storage (SHA256 hashed)
- Key creation, listing, revocation via API
- Per-client endpoint routing via key config

### Phase 12: Dashboard -- COMPLETE
- React + TypeScript + Tailwind
- Security monitor with regex vs guard comparison
- Audit log viewer
- Model catalog
- API key management UI
- Endpoint health display

### Phase 13: Model Discovery -- COMPLETE
- Auto-discovers models across all Ollama endpoints
- Periodic refresh (60s interval)
- Catalog with model metadata (size, family, quantization)

### Phase 14: Vision/Image Support -- COMPLETE
- Image passthrough in Ollama chat/generate routes
- Base64 image preservation through sanitization pipeline
- Debug logging for image pipeline

---

## Known Issues (from 2026-03-11 Code Review)

### Blockers
- [ ] Streaming errors leak raw exception messages to clients
- [ ] OpenAI route sanitization bypass (`body.to_internal()` uses unsanitized)
- [ ] No circuit breaker for guard model
- [ ] No input length cap before regex scanning
- [ ] Key management endpoints unauthenticated
- [ ] Key expiration never checked
- [ ] No migration framework (Alembic)

### High Risk
- [ ] httpx client TOCTOU race in provider adapters
- [ ] Dispatcher `_try_provider` swallows exceptions silently
- [ ] Ollama routes have zero Prometheus metrics
- [ ] No per-chunk timeout in OpenAI streaming
- [ ] Zero test coverage: guard clients, analyzer, KeyManager

### See COMPLIANCE_AUDIT.md for full findings.

---

## Future Phases

### Phase 15: Open Source Readiness
- [ ] Fix all blockers from code review
- [ ] Add Alembic migrations
- [ ] docker-compose.yaml with monitoring stack
- [ ] Grafana dashboard template
- [ ] Contributing guide
- [ ] License file verification

### Phase 16: Enforcement Mode
- [ ] Guard model inline (parallel with LLM inference)
- [ ] Stream kill on guard flag
- [ ] Configurable block vs shadow per category

### Phase 17: Cloud Provider Adapters
- [ ] OpenAI API (direct)
- [ ] Anthropic API
- [ ] AWS Bedrock
- [ ] Google Vertex AI

---

## Tech Stack

- **Framework:** FastAPI
- **Config:** Pydantic Settings + YAML
- **HTTP Client:** httpx (async)
- **Database:** SQLAlchemy (async) — SQLite default, PostgreSQL ready
- **Metrics:** prometheus-client
- **Security:** Custom injection detector + Granite Guardian / Llama Guard
- **Dashboard:** React + TypeScript + Vite + Tailwind
- **Testing:** pytest + pytest-asyncio
- **Container:** Docker (compose not yet created)
