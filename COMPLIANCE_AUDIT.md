# DevMesh Gateway - Compliance Audit

Full audit of the implementation against design principles (rule.md), PRD (gatewayprd.md), and Universal Code Review Rubric.

**Audit Date**: 2026-03-11 (Updated)
**Previous Audit**: 2025-12-22
**Status**: **REVIEW REQUIRED** - Blockers identified in new modules

---

## Audit Scope

Since the 2025-12-22 audit, the following major features were added:
- Ollama-native API routes (`/api/chat`, `/api/generate`, `/api/embeddings`, `/api/tags`)
- Security module (sanitizer, injection detector, guard model integration)
- Guard model support (Llama Guard 3, IBM Granite Guardian 3.2)
- Async SQLite/PostgreSQL audit logging with request body storage
- API key management (creation, revocation, validation)
- Dashboard (React + TypeScript) with security monitoring
- Model discovery service
- Per-client endpoint routing
- Vision/image passthrough

---

## Risk Tier Classification: Tier 3 (Critical)

The gateway is a policy/routing/enforcement system handling untrusted input from public-facing services. Security controls, auth, and persistence place this at Tier 3.

---

## Gate Scores

| Gate | Score | Status | Notes |
|------|-------|--------|-------|
| G1: Intent & Scope | 2/2 | Pass | Clean separation of concerns |
| G2: Correctness | 1/2 | Issues | Sanitization bypass in OpenAI route; schema/query mismatch |
| G3: Failure Semantics | 1/2 | Issues | No circuit breaker; streaming error leaks |
| G4: Security | 1/2 | Issues | Unauthenticated key management; no input length caps |
| G5: Data Integrity | 1/2 | Issues | No migration framework; non-atomic aggregation |
| G6: Concurrency | 1/2 | Issues | httpx client TOCTOU race; connection leaks |
| G7: Observability | 1/2 | Issues | Ollama metrics gap; silent audit failures |
| G8: Tests | 1/2 | Issues | Zero coverage on guard, analyzer, KeyManager |
| G9: Maintainability | 1/2 | Acceptable | Route duplication; single-file dashboard |
| **Total** | **10/18** | | Below Tier 3 threshold (16/18) |

---

## Blockers

### ~~B1: Streaming error info disclosure~~ — FIXED
- **Location**: `src/gateway/routes/ollama.py`
- **Fix**: Replaced `str(e)` with generic `"Stream interrupted"` message in both chat and generate stream error handlers

### ~~B2: OpenAI route sanitization bypass~~ — FIXED
- **Location**: `src/gateway/routes/openai.py`
- **Fix**: Sanitized content is now applied back to `body.messages` before `to_internal()` conversion

### ~~B3: No circuit breaker for guard model~~ — FIXED
- **Location**: `src/gateway/security/guard.py`
- **Fix**: Added `CircuitBreaker` class (closed→open after 5 consecutive failures, half-open after 60s cooldown). Wired into both `LlamaGuardClient` and `GraniteGuardianClient`.

### ~~B4: No input length cap before regex scanning~~ — FIXED
- **Location**: `src/gateway/security/injection.py`
- **Fix**: Added configurable `max_input_length` (default 100KB) that truncates input before regex scanning

### ~~B5: Key management endpoints unauthenticated~~ — FIXED
- **Location**: `src/gateway/routes/devmesh.py`, `src/gateway/routes/dependencies.py`
- **Fix**: Added `require_admin` dependency using `GATEWAY_ADMIN_API_KEY` env var. Key management endpoints now require admin auth. Falls back to standard auth when admin key is not configured.

### ~~B6: Key expiration never checked~~ — FIXED
- **Location**: `src/gateway/storage/keys.py`
- **Fix**: Added `expires_at` filter to `get_key_by_hash()` query. Also fixed deprecated `datetime.utcnow()` → `datetime.now(timezone.utc)`.

### ~~B7: No migration framework~~ — FIXED
- **Location**: `alembic/`, `alembic.ini`
- **Fix**: Adopted Alembic with batch mode for SQLite support. Initial migration created. Existing databases stamped at head. `render_as_batch=True` enables SQLite ALTER TABLE via batch operations.

---

## High Risk Items

| ID | Location | Issue |
|----|----------|-------|
| ~~H1~~ | `providers/ollama.py`, `openai.py` | ~~TOCTOU race~~ — **FIXED**: Added `asyncio.Lock` to `_get_client()` |
| ~~H2~~ | `dispatch/dispatcher.py` | ~~Silent exception swallowing~~ — **FIXED**: Added structured logging with provider/model/error |
| ~~H3~~ | `routes/ollama.py` | ~~Zero Prometheus metrics~~ — **FIXED**: Added `metrics.record_request()` to chat, generate, embeddings |
| ~~H4~~ | `security/injection.py` | ~~Incomplete tag escaping~~ — **FIXED**: Now escapes both opening and closing tags |
| ~~H5~~ | `security/guard.py`, `analyzer.py` | ~~Zero test coverage~~ — **FIXED**: 30 tests for guard clients (LlamaGuard, Granite, CircuitBreaker, factory) + 19 tests for analyzer |
| ~~H6~~ | `storage/keys.py` | ~~Zero test coverage~~ — **FIXED**: 18 tests for KeyManager (CRUD, validation, revocation, hash helpers) |
| ~~H7~~ | `storage/audit.py` | ~~Silent audit failures~~ — **FIXED**: Added structured error logging + metric |
| ~~H8~~ | `storage/audit.py` | ~~literal_column f-string~~ — **FIXED**: Replaced with `bindparam()` |
| ~~H9~~ | `providers/openai.py` | ~~No per-chunk timeout~~ — **FIXED**: Added `_iter_lines_with_timeout()` (120s) |

---

## Medium Items

| ID | Location | Issue |
|----|----------|-------|
| M1 | `security/injection.py:273` | Multimodal content arrays skip injection scanning |
| M2 | `storage/audit.py` | `datetime.utcnow()` deprecated in Python 3.12+ |
| M3 | `routes/ollama.py`, `openai.py` | ~200 lines duplicated across route handlers |
| M4 | `storage/audit.py` | No data retention policy — audit_log grows unbounded |
| M5 | `storage/keys.py:20` | Unsalted SHA256 for key hashing |
| M6 | `storage/keys.py` | No key lifecycle logging (create, revoke, validate) |
| M7 | `dispatch/registry.py` | `datetime.utcnow()` deprecated |
| M8 | `routes/devmesh.py` | 1273 lines in single file — should split into sub-routers |

---

## Original Audit Items - Status Update

### Previously Identified Gaps

| Gap (2025-12) | Status | Resolution |
|----------------|--------|------------|
| No docker-compose.yaml | Still open | Dashboard added but no compose file |
| No TLS | Still open | Intended for reverse proxy |
| No request redaction | **Resolved** | `redact_prompts` setting + body storage off by default |
| No API key rotation | **Partially resolved** | Key creation/revocation via API, no auto-rotation |
| No request body size limit | Still open | See B4 |
| Provider URLs not validated | Still open | Config-only URLs (low risk) |

---

## PRD Compliance Update

### New Features vs PRD

| Feature | PRD Status | Implemented | Notes |
|---------|------------|-------------|-------|
| Ollama-native API | Not in v0 PRD | Yes | Extension beyond v0 scope |
| Security scanning | Not in v0 PRD | Yes | Content moderation was "future" in PRD |
| Guard model | Not in v0 PRD | Yes | Shadow mode only |
| Dashboard | Not in v0 PRD | Yes | Listed as "future extension" |
| Audit logging DB | Not in v0 PRD | Yes | PRD said "no data persistence" |
| API key management | Not in v0 PRD | Yes | PRD had basic API key auth only |

The gateway has significantly exceeded v0 PRD scope. The PRD's "future extensions" section listed cloud adapters, hybrid routing, memory modules, eval harness, and advanced dashboards — several of these are now partially implemented.

### PRD v0 Requirements Still Unmet

| Requirement | Status |
|-------------|--------|
| Prometheus metrics on all routes | Partial — missing on Ollama routes |
| Grafana dashboard template | Not created |
| docker-compose.yaml | Not created |

---

## Design Principles Compliance (rule.md)

| Principle | Score | Notes |
|-----------|-------|-------|
| Secure by Design | Partial | Guard fail-open behavior needs explicit documentation; key mgmt auth gap |
| SOLID - Single Responsibility | Pass | Clean module separation |
| SOLID - Open/Closed | Pass | New guard backends via factory pattern |
| SOLID - Liskov Substitution | Pass | Guard clients share `classify()` interface |
| SOLID - Interface Segregation | Pass | Minimal provider adapter interface |
| SOLID - Dependency Inversion | Pass | Routes depend on abstractions |
| DRY | Partial | Route handler duplication (ollama/openai) |
| YAGNI | Pass | All features serve real use cases |
| KISS | Pass | Clear data flow |
| Replaceability | Pass | Guard model, providers, DB all swappable |
| Contracts | Partial | Missing contract tests for guard model interface |

---

## Test Coverage

```
365 tests collected
- test_config.py: Config loading, validation
- test_models.py: All data models
- test_dispatch.py: Routing, fallback
- test_policy.py: Rate limiting, enforcement
- test_observability.py: Logging, metrics
- test_providers.py: Adapter contracts
- test_routes.py: API endpoints
- test_security.py: Sanitizer, injection detector (NOT analyzer/guard)
- test_storage.py: Audit logger, schema (NOT KeyManager)
- test_settings.py: Top-level settings (NOT nested)
- test_dashboard_api.py: Dashboard endpoints
- test_main.py, test_resolution.py, test_openai_adapter.py
```

### Critical Test Gaps

| Component | Lines of Code | Test Coverage |
|-----------|--------------|---------------|
| `security/guard.py` | 330 | **None** |
| `security/analyzer.py` | 446 | **None** |
| `storage/keys.py` | ~150 | **None** |
| `routes/ollama.py` | 570 | **Minimal** |

---

## Recommended Fix Priority

### Phase 1: Security Blockers (before open source)
1. Fix streaming error info disclosure (B1)
2. Fix OpenAI sanitization bypass (B2)
3. Add auth to key management endpoints (B5)
4. Add key expiration check (B6)
5. Add input length cap for security scanning (B4)

### Phase 2: Reliability
6. Add circuit breaker for guard model (B3)
7. Fix httpx client TOCTOU race (H1)
8. Add logging to `_try_provider` (H2)
9. Add Ollama metrics (H3)
10. Add per-chunk streaming timeout (H9)

### Phase 3: Test Coverage
11. Tests for guard clients (H5)
12. Tests for async analyzer (H5)
13. Tests for KeyManager (H6)

### Phase 4: Production Readiness
14. Adopt Alembic for migrations (B7)
15. Add data retention/cleanup for audit log (M4)
16. Add key lifecycle logging (M6)
