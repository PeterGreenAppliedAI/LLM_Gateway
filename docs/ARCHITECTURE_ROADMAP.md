# DevMesh AI Fabric - Architecture Roadmap

## Strategic Position

This gateway is **infrastructure**, not a product surface.

It's the **AI operating system layer** that sits below:
- RAG pipelines
- Agents
- Automations
- Bots
- Apps
- Workflows
- Orchestration
- Research pipelines
- Client automations

**Core responsibilities:**
- Model abstraction
- Compute abstraction
- Policy enforcement
- Routing & resolution
- Observability
- Governance
- Security
- Trust boundaries

Everything else becomes **applications on top of it**, not features inside it.

---

## Current State (v1.0)

**Completed:**
- [x] Multi-endpoint routing (Ollama, extensible to vLLM/TRT/SGLang)
- [x] OpenAI-compatible API (`/v1/chat/completions`)
- [x] Ollama-compatible API (`/api/chat`, `/api/generate`)
- [x] Model discovery & catalog
- [x] Environment separation (dev/prod)
- [x] Basic resolution (explicit targeting, model defaults, priority)
- [x] Audit logging with performance metrics
- [x] Health monitoring
- [x] Dashboard (observability UI)
- [x] Per-client endpoint routing (via API key `target_endpoint`)
- [x] Security: Prompt injection defense (observe mode - sanitize, detect, alert)

**Implicit/Hardcoded:**
- Policy logic embedded in code
- `client_id = "default"` (no real identity)
- Resolution is a simple lookup, not a multi-factor decision
- No formal interception points

---

## Gap Analysis

### 1. Policy Layer (Explicit)

**Current:** Policy is implicit in code.

**Target:** Policy objects + rule engine with resolution rules as data.

```yaml
# config/policies.yaml
policies:
  - name: pii_guard
    applies_to:
      endpoints: [external_*]
    triggers:
      - pre_route
      - post_response
    actions:
      - scrub_pii
      - redact_output

  - name: internal_fast
    applies_to:
      endpoints: [gpu-node-3060]
    rules:
      max_tokens: 2048
      max_latency_ms: 3000

  - name: cost_control
    applies_to:
      trust_level: [standard, limited]
    rules:
      max_tokens_per_request: 4096
      max_requests_per_minute: 60
      blocked_models: ["gpt-oss:120b"]

  - name: prod_guardrails
    applies_to:
      environment: prod
    rules:
      require_approved_models: true
      require_audit: true
      block_experimental: true
```

### 2. Identity & Trust Boundaries

**Current:** `client_id = "default"`

**Target:** Full identity hierarchy enabling quotas, billing, segmentation.

```yaml
# config/identity.yaml
organizations:
  - org_id: devmesh
    trust_level: owner
    projects:
      - project_id: research
        environments: [dev, prod]
        users:
          - user_id: pete
            role: admin
          - user_id: bot-discord
            role: service
            trust_level: standard

trust_levels:
  owner:
    max_tokens_per_day: unlimited
    allowed_endpoints: all
    allowed_models: all
  admin:
    max_tokens_per_day: 10_000_000
    allowed_endpoints: all
    allowed_models: all
  standard:
    max_tokens_per_day: 1_000_000
    allowed_endpoints: [gpu-node, gpu-node-3060]
    blocked_models: [gpt-oss:120b]
  limited:
    max_tokens_per_day: 100_000
    allowed_endpoints: [gpu-node-3060]
```

### 3. Resolution Engine Formalization

**Current:**
```python
model_map[model_name] -> endpoint  # Simple lookup
```

**Target:**
```python
(model, task, client, policy, load, health, cost, trust_level) -> endpoint
```

This is a **resolver**, not a router.

```python
# src/gateway/resolution/resolver.py
class ResolverContext:
    model: str
    task: TaskType
    client: ClientIdentity
    policies: list[Policy]
    endpoint_health: dict[str, HealthStatus]
    endpoint_load: dict[str, LoadMetrics]
    trust_level: TrustLevel

class Resolver(Protocol):
    def resolve(self, ctx: ResolverContext) -> ResolvedEndpoint: ...

# Pluggable strategies
class StaticResolver(Resolver): ...      # Current behavior
class LoadBalancedResolver(Resolver): ... # Least-loaded endpoint
class LatencyOptimizedResolver(Resolver): ... # Fastest response time
class CostOptimizedResolver(Resolver): ...    # Cheapest option
class TrustAwareResolver(Resolver): ...       # Respects trust boundaries
class CompositeResolver(Resolver): ...        # Chain of strategies
```

### 4. Policy Interception Points

**Current:** No formal hooks.

**Target:** Explicit pipeline with named interception points.

```
ingress
   ↓
auth (identity extraction)
   ↓
policy_pre (input validation, PII scrubbing, rate limits)
   ↓
resolution (multi-factor endpoint selection)
   ↓
policy_route (final routing rules, load balancing)
   ↓
execution (actual LLM call)
   ↓
policy_post (output filtering, redaction, cost tracking)
   ↓
egress
```

```python
# src/gateway/pipeline/hooks.py
class PipelineHook(Protocol):
    async def execute(self, ctx: RequestContext) -> RequestContext | Response: ...

class Pipeline:
    hooks: dict[HookPoint, list[PipelineHook]]

    async def process(self, request: InternalRequest) -> Response:
        ctx = RequestContext(request)

        for hook in self.hooks[HookPoint.AUTH]:
            ctx = await hook.execute(ctx)

        for hook in self.hooks[HookPoint.POLICY_PRE]:
            ctx = await hook.execute(ctx)
            if ctx.is_rejected:
                return ctx.rejection_response

        ctx.endpoint = await self.resolver.resolve(ctx)

        for hook in self.hooks[HookPoint.POLICY_ROUTE]:
            ctx = await hook.execute(ctx)

        ctx.response = await self.execute(ctx)

        for hook in self.hooks[HookPoint.POLICY_POST]:
            ctx = await hook.execute(ctx)

        return ctx.response
```

---

## Implementation Phases

### Phase 1: Declarative Configuration (Foundation)

**Goal:** Move from code configs to declarative YAML configs.

**Files:**
```
config/
├── endpoints.yaml      # Physical endpoints (exists)
├── environments.yaml   # dev/prod separation
├── models.yaml         # Model metadata, aliases, constraints
├── policies.yaml       # Policy rules
├── resolution.yaml     # Resolution strategies
├── identity.yaml       # Orgs, projects, users, trust levels
```

**Tasks:**
- [ ] Create Pydantic models for each config type
- [ ] YAML loader with validation
- [ ] Hot-reload support for config changes
- [ ] Config versioning/migration

---

### Phase 2: Identity Expansion

**Goal:** Replace `client_id = "default"` with full identity hierarchy.

**Schema:**
```python
class ClientIdentity(BaseModel):
    org_id: str
    project_id: str
    user_id: str | None
    service_id: str | None
    trust_level: TrustLevel
    environment: str

class TrustLevel(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    STANDARD = "standard"
    LIMITED = "limited"
    ANONYMOUS = "anonymous"
```

**Tasks:**
- [ ] Identity extraction from API keys
- [ ] Identity extraction from JWT tokens (future)
- [ ] Trust level inheritance (org → project → user)
- [ ] Per-identity quotas table
- [ ] Rate limiting by identity

---

### Phase 3: Policy Engine

**Goal:** Declarative policy rules with predicate → action.

**Core:**
```python
class PolicyRule(BaseModel):
    name: str
    applies_to: PolicySelector  # endpoints, models, trust_levels, environments
    triggers: list[HookPoint]   # pre_route, post_response
    predicates: list[Predicate] # conditions that must match
    actions: list[Action]       # what to do when matched

class Predicate(BaseModel):
    field: str      # "request.model", "client.trust_level"
    operator: str   # "equals", "contains", "greater_than"
    value: Any

class Action(BaseModel):
    type: str       # "block", "allow", "modify", "log", "rate_limit"
    params: dict
```

**Built-in Actions:**
- `block` - Reject request with error
- `allow` - Explicitly permit (override other rules)
- `modify_request` - Transform input
- `modify_response` - Transform output
- `scrub_pii` - Remove PII from request/response
- `rate_limit` - Apply rate limiting
- `log` - Enhanced audit logging
- `alert` - Trigger notification

**Tasks:**
- [ ] Policy rule parser
- [ ] Predicate evaluator
- [ ] Action executor
- [ ] Policy priority/ordering
- [ ] Policy testing framework

---

### Phase 4: Resolution Abstraction

**Goal:** Pluggable resolver strategies.

**Interface:**
```python
class Resolver(Protocol):
    async def resolve(self, ctx: ResolverContext) -> ResolvedEndpoint: ...

class ResolvedEndpoint(BaseModel):
    endpoint: str
    model: str
    reason: str  # Why this endpoint was chosen
    alternatives: list[str]  # Other valid options
```

**Strategies:**
- [ ] `StaticResolver` - Current behavior (model defaults, priority list)
- [ ] `LoadBalancedResolver` - Distribute across endpoints
- [ ] `LatencyOptimizedResolver` - Route to fastest endpoint
- [ ] `CostOptimizedResolver` - Route to cheapest option
- [ ] `TrustAwareResolver` - Respect trust boundaries
- [ ] `FailoverResolver` - Automatic failover on errors
- [ ] `CompositeResolver` - Chain multiple strategies

**Tasks:**
- [ ] Resolver interface definition
- [ ] Strategy implementations
- [ ] Real-time metrics collection for load/latency
- [ ] Resolver configuration in YAML
- [ ] A/B testing support for strategies

---

### Phase 5: Pipeline Formalization

**Goal:** Explicit request pipeline with hook points.

**Hook Points:**
```python
class HookPoint(str, Enum):
    INGRESS = "ingress"
    AUTH = "auth"
    POLICY_PRE = "policy_pre"
    RESOLUTION = "resolution"
    POLICY_ROUTE = "policy_route"
    EXECUTION = "execution"
    POLICY_POST = "policy_post"
    EGRESS = "egress"
```

**Tasks:**
- [ ] Pipeline executor
- [ ] Hook registration
- [ ] Context propagation
- [ ] Error handling at each stage
- [ ] Metrics per hook point
- [ ] Hook timeout enforcement

---

## Future Phases (Post-Foundation)

### Phase 6: Quotas & Billing
- Token budgets per identity
- Cost tracking per request
- Usage reports
- Overage policies

### Phase 7: Advanced Security

**Prompt Injection Defense (Integrated):**

| Layer | Status | Latency | Description |
|-------|--------|---------|-------------|
| Unicode sanitization | ✅ Integrated | ~0ms | Strip invisible chars on all routes |
| Content wrapping | ✅ Done | ~0ms | Trust-level tags (available for use) |
| Pattern logging | ✅ Integrated | ~1ms | Detect & log (not block) on all routes |
| Async analysis | ✅ Integrated | 0ms* | Background alerting on all routes |
| Dashboard alerts | ✅ Done | N/A | Security monitor in dashboard UI |
| Security API | ✅ Done | N/A | `/api/security/alerts`, `/api/security/stats` |
| Alert actions | ❌ Future | N/A | Block, webhook, rate limit, auto-ban |
| Guard LLM | ❌ Future | +500ms | Semantic detection (opt-in) |
| Fast classifier | ❌ Future | +10ms | Trained ML model |

*Async doesn't block requests

**Current Mode: Observe Only** - All requests are scanned in the background. Suspicious patterns generate alerts visible in the dashboard. No requests are blocked. This allows understanding normal vs. suspicious traffic before enabling enforcement.

**Why Pattern Matching Alone Fails:**
- Attackers rephrase: "ignore instructions" → "let's start fresh"
- Use other languages, roleplay scenarios, encoding tricks
- Useful for **logging**, not as primary defense

**Future Alert Actions (When Ready):**
- Block CRITICAL threats (configurable per-client or globally)
- Webhook notifications (Slack/Discord/PagerDuty)
- Rate limit clients with repeated alerts
- Auto-ban repeat offenders (temporary block by client_id)
- Quarantine mode (hold request for manual review)

**Future Guard LLM (Opt-in per API key):**
```yaml
api_keys:
  - key: "pr-reviewer-key"
    policies:
      injection_guard: true
      guard_model: "llama3.2:3b"
```
Adds ~500ms but provides semantic understanding.

**Infrastructure Security (TODO):**
- Request signing
- mTLS between gateway and endpoints
- Secrets management integration (Vault/KMS)
- Audit log encryption at rest

### Phase 8: Multi-Region / Federation
- Cross-datacenter routing
- Edge caching
- Geo-aware resolution

### Phase 9: SDK & Client Libraries
- Python SDK
- TypeScript SDK
- CLI improvements

### Phase 10: Dashboard Self-Service
- **API Key Management UI**
  - Create keys with target endpoint, allowed models, quotas
  - Auto-generate usage instructions (cURL, Python, env vars)
  - Copy-to-clipboard for easy onboarding
  - Key rotation and revocation
- Config editor (endpoints, environments, resolution)
- Real-time endpoint health visualization
- Usage analytics and cost projections

---

## File Structure (Target)

```
src/gateway/
├── config/
│   ├── loader.py           # YAML config loading
│   ├── models.py           # Config Pydantic models
│   └── validation.py       # Config validation
├── identity/
│   ├── extractor.py        # Identity from request
│   ├── models.py           # ClientIdentity, TrustLevel
│   └── quotas.py           # Quota tracking
├── policy/
│   ├── engine.py           # Policy evaluation
│   ├── models.py           # PolicyRule, Predicate, Action
│   ├── actions/            # Built-in actions
│   │   ├── block.py
│   │   ├── scrub_pii.py
│   │   └── rate_limit.py
│   └── predicates.py       # Predicate evaluation
├── resolution/
│   ├── resolver.py         # Resolver interface
│   ├── context.py          # ResolverContext
│   └── strategies/
│       ├── static.py
│       ├── load_balanced.py
│       ├── latency_optimized.py
│       └── composite.py
├── pipeline/
│   ├── executor.py         # Pipeline execution
│   ├── hooks.py            # Hook interface
│   └── context.py          # RequestContext
├── security/               # ✅ EXISTS
│   ├── __init__.py         # Module exports
│   ├── sanitizer.py        # Unicode sanitization
│   ├── injection.py        # Pattern detection + content wrapping
│   └── analyzer.py         # Async background analysis
├── catalog/                # (exists)
├── dispatch/               # (exists, will integrate with resolution)
├── routes/                 # (exists)
├── storage/                # (exists)
└── models/                 # (exists)
```

---

## Success Criteria

**Phase 1 Complete When:**
- All configuration is in YAML files
- Config changes don't require code changes
- Config validation catches errors at startup

**Phase 2 Complete When:**
- Every request has a full ClientIdentity
- Quotas can be set per org/project/user
- Rate limiting works by identity

**Phase 3 Complete When:**
- Policies are defined in YAML
- New policies can be added without code
- Policy violations are logged and blocked

**Phase 4 Complete When:**
- Multiple resolver strategies exist
- Strategy can be changed via config
- Resolution decisions are logged with reasoning

**Phase 5 Complete When:**
- Full pipeline with all hook points
- Custom hooks can be registered
- Metrics available for each pipeline stage

---

## Guiding Principles

1. **Configuration over code** - Behavior changes shouldn't require deployments
2. **Explicit over implicit** - All policies and rules visible in config
3. **Composable** - Small pieces that combine, not monolithic features
4. **Observable** - Every decision logged with reasoning
5. **Secure by default** - Deny unless explicitly allowed
6. **Backward compatible** - Old configs continue to work

---

## Not In Scope (For Now)

- Multi-tenant SaaS packaging
- Public billing/payments
- User-facing auth (OAuth, etc.)
- Workflow orchestration
- Agent frameworks
- RAG pipelines

These are **applications on top**, not features inside the fabric.

---

## Related Documents

- **[Client Deployment Readiness](./CLIENT_DEPLOYMENT_READINESS.md)** - Current state assessment, gaps analysis, and implementation priorities for shipping to clients
