# DevMesh Gateway v0 - Implementation Plan

## Phased Approach

Each phase includes:
- Implementation
- Unit tests
- Verification before proceeding

---

## Phase 1: Project Scaffolding

**Goal:** Establish project structure, dependencies, Docker setup, and config loading.

### Deliverables
```
llm_gateway/
├── src/
│   └── gateway/
│       ├── __init__.py
│       ├── main.py              # FastAPI app entry
│       ├── config.py            # Config loading (YAML + env vars)
│       └── settings.py          # Pydantic settings
├── tests/
│   ├── __init__.py
│   └── test_config.py
├── config/
│   ├── gateway.yaml             # Main config template
│   └── providers.yaml           # Provider definitions
├── docker/
│   └── Dockerfile
├── docker-compose.yaml
├── pyproject.toml
├── requirements.txt
└── README.md
```

### Tests
- [ ] Config loads from YAML
- [ ] Environment variables override config
- [ ] Missing required config raises clear error
- [ ] FastAPI app starts and /health returns 200

---

## Phase 2: Core Models

**Goal:** Define normalized internal request/response format and OpenAI-compatible schemas.

### Deliverables
```
src/gateway/
├── models/
│   ├── __init__.py
│   ├── internal.py              # Internal normalized format
│   ├── openai.py                # OpenAI-compatible schemas
│   └── common.py                # Shared types (TaskType, etc.)
```

### Internal Model Fields (per PRD)
- request_id, task, input/messages, max_tokens, temperature
- client_id, user_id
- Optional: preferred_provider, fallback_allowed, schema

### Tests
- [ ] Internal request model validates required fields
- [ ] OpenAI ChatCompletion request parses correctly
- [ ] OpenAI -> Internal conversion works
- [ ] Internal -> OpenAI response conversion works
- [ ] Invalid requests raise ValidationError

---

## Phase 3: Provider Adapter Interface

**Goal:** Abstract provider interface + working Ollama adapter.

### Deliverables
```
src/gateway/
├── providers/
│   ├── __init__.py
│   ├── base.py                  # Abstract ProviderAdapter
│   ├── ollama.py                # Ollama implementation
│   ├── vllm.py                  # vLLM implementation (stub)
│   ├── trtllm.py                # TRT-LLM stub
│   └── sglang.py                # SGLang stub
```

### Interface Methods (per PRD)
- `health() -> HealthStatus`
- `list_models() -> List[ModelInfo]`
- `chat(request) -> ChatResponse`
- `generate(request) -> GenerateResponse` (optional)
- `embeddings(request) -> EmbeddingsResponse` (optional)

### Adapter Metadata
- Capabilities, max_context_length, streaming_support, limitations

### Tests
- [ ] Abstract base enforces interface contract
- [ ] Ollama adapter implements all required methods
- [ ] health() returns correct status
- [ ] list_models() returns model info with capabilities
- [ ] chat() handles request/response correctly (mocked)
- [ ] Adapter declares capabilities properly

---

## Phase 4: Routing Engine

**Goal:** Config-driven routing with fallback support.

### Deliverables
```
src/gateway/
├── routing/
│   ├── __init__.py
│   ├── engine.py                # Router implementation
│   ├── rules.py                 # Routing rule definitions
│   └── selector.py              # Provider selection logic
config/
└── routing.yaml                 # Routing rules config
```

### Routing Inputs (per PRD)
- Task type
- Model capability requirements
- Provider health
- Client overrides

### Routing Outputs
- Selected provider
- Selected model
- Fallback chain

### Tests
- [ ] Routes task to correct provider per config
- [ ] Respects client preferred_provider override
- [ ] Falls back when primary provider unhealthy
- [ ] Returns error when no healthy provider available
- [ ] Routing config loads and validates

---

## Phase 5: Policy Enforcement

**Goal:** Rate limiting and token limits.

### Deliverables
```
src/gateway/
├── policies/
│   ├── __init__.py
│   ├── engine.py                # Policy enforcement engine
│   ├── rate_limit.py            # Rate limiting (global + per-user)
│   └── token_limit.py           # Max tokens per request
config/
└── policies.yaml                # Policy definitions
```

### Required Policies (per PRD)
- Max tokens per request
- Requests per minute (global and per user)
- Allowed providers per task
- Block if provider unhealthy

### Tests
- [ ] Blocks request exceeding max_tokens
- [ ] Enforces global rate limit
- [ ] Enforces per-user rate limit
- [ ] Blocks disallowed provider for task
- [ ] Policy config loads correctly

---

## Phase 6: Observability

**Goal:** Structured JSON logging + Prometheus metrics.

### Deliverables
```
src/gateway/
├── observability/
│   ├── __init__.py
│   ├── logging.py               # Structured JSON logger
│   └── metrics.py               # Prometheus metrics
```

### Log Fields (per PRD)
- request_id, client_id, user_id, provider, model
- task, latency_ms, token counts, error type

### Metrics (per PRD)
- requests_total{provider,model,task,status}
- request_latency_ms (histogram)
- tokens_prompt_total, tokens_completion_total
- provider_errors_total
- active_requests{provider}

### Tests
- [ ] Logs are valid JSON
- [ ] All required fields present in logs
- [ ] Prometheus metrics exposed correctly
- [ ] Latency histogram records correctly
- [ ] Token counters increment properly

---

## Phase 7: API Endpoints

**Goal:** OpenAI-compatible API + DevMesh extensions.

### Deliverables
```
src/gateway/
├── api/
│   ├── __init__.py
│   ├── routes.py                # Route registration
│   ├── chat.py                  # POST /v1/chat/completions
│   ├── completions.py           # POST /v1/completions
│   ├── models.py                # GET /v1/models
│   ├── health.py                # GET /health
│   ├── metrics.py               # GET /metrics
│   └── devmesh.py               # POST /v1/devmesh/route
├── middleware/
│   ├── __init__.py
│   ├── auth.py                  # API key authentication
│   └── request_id.py            # Request ID injection
```

### External API (OpenAI-compatible)
- POST /v1/chat/completions
- POST /v1/completions
- POST /v1/embeddings (optional)

### DevMesh Extensions
- GET /health
- GET /metrics
- GET /v1/models
- POST /v1/devmesh/route

### Tests
- [ ] /health returns 200 with status
- [ ] /v1/chat/completions accepts OpenAI format
- [ ] /v1/models returns available models
- [ ] /metrics returns Prometheus format
- [ ] Auth middleware rejects invalid API key
- [ ] Request ID generated and propagated
- [ ] Full integration test: request -> route -> provider -> response

---

## Final Integration

After all phases complete:
- [ ] docker-compose up starts full stack
- [ ] End-to-end test with real Ollama
- [ ] Grafana dashboard shows metrics
- [ ] Deploy documentation complete

---

## Tech Stack

- **Framework:** FastAPI
- **Config:** Pydantic Settings + YAML (PyYAML)
- **HTTP Client:** httpx (async)
- **Metrics:** prometheus-client
- **Testing:** pytest + pytest-asyncio + httpx (TestClient)
- **Container:** Docker + Docker Compose
