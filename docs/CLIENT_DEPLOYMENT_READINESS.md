# Client Deployment Readiness Assessment

_Last reviewed: 2026-10-07, on `docs/readiness-gap-report` (opened as a PR into `main`)._
_Decisions behind each fix: [DECISIONS.md](DECISIONS.md)._

This report checks the code against what a production gateway needs to handle real-time
traffic. This revision follows an external review of `4390209`. The review found six real
defects (D-041 to D-044), Windows failures (D-045), and places where this report said
"fixed" more broadly than the evidence supported.

## How to read this report

A **Fixed** row names four things, so the claim can be checked rather than trusted:

- **Decision and commit:** the DECISIONS.md entry and the commit that made the change.
- **Requires:** configuration needed for the fix to apply. "Default" means none.
- **Proven by:** the test file that fails if the fix regresses.
- **Platform:** where that test runs. Unless a row says otherwise:
  - **All** means every CI job: Linux with Python 3.10, 3.11 and 3.12, and Windows with
    Python 3.10 and 3.12.
  - **SQLite + PG** means the test runs on SQLite and on PostgreSQL 16. PostgreSQL runs on
    Linux CI only.
  - **Redis** means against a real Redis 7, on Linux CI only.

  On Linux, CI fails rather than skips when PostgreSQL or Redis is missing
  (`GATEWAY_TEST_REQUIRE_SERVICES=1`, D-045).

**Test baseline:** 914 passed, 0 skipped, run locally with PostgreSQL and Redis required
(Linux, Python 3.13). The CI result on the PR is the authoritative run for 3.10 to 3.12 and
for Windows.

**Severity key:**
- **P0:** a guarantee the product claims does not hold.
- **P1:** it degrades or fails under real load.
- **P2:** hardening or polish.

## Verdict

**The gateway is ready for single-organization deployments** (Profiles A and B below):

- every request is authenticated and attributed;
- budgets and rate limits are enforced, including under concurrency and streaming;
- the audit trail survives crashes;
- PII is scrubbed before it reaches engines or storage, where scrubbing is configured.

**Not ready:**
- **Multi-tenant (Profile C):** per-tenant views and isolation are still open. D-039 is
  the proposed design.
- **Hybrid with external providers:** egress control is still open (section 4).

**Operations gaps still open** (section 4):
- `/health` never reports not-ready;
- no graceful drain for streams;
- no trusted-proxy support.

---

## 1. Policy and access control

### Fixed

| Gap | Decision, commit | Requires | Proven by | Platform |
|---|---|---|---|---|
| Keyless requests bypassed per-key policy, and keyless access was on by default | D-001 `bbfe4b5`, D-042 `b8e0228` | Default (keyless off). For keyless clients: `auth.anonymous.enabled` plus `allowed_networks` | `test_access_modes.py`, `test_ollama_routes.py::TestAuthSplit` | All |
| With no admin key set, any client key got operator access | D-042 `b8e0228` | `GATEWAY_ADMIN_API_KEY` when `auth.enabled`; admin routes return 403 `admin_key_required` without it | `test_access_modes.py::TestKeysMode` | All |
| Auth off accepted requests from anywhere | D-042 `b8e0228` | Default (this machine only); widen with `auth.anonymous.allowed_networks` | `test_access_modes.py::TestSoloMode` | All |
| Control-plane writes needed only a client key | D-002 `d7ed7dc` | `GATEWAY_ADMIN_API_KEY` | `test_dashboard_api.py`, `test_ollama_routes.py` | All, SQLite + PG |
| Dashboard reads showed every client's traffic to any key | D-002 `a0e31b8` | Same | `test_dashboard_api.py` | All, SQLite + PG |
| Endpoint allowlist checked the requested endpoint, not the one used; environments never applied | D-003 `0f70f4a` | Default | `test_resolution.py` | All |
| Budget check passed with `max_tokens` omitted, at exactly the limit, and under concurrency | D-043 `68f5968` | `token_budget.enabled` | `test_budget_reservations.py` | All |
| Budgets and dashboard tier changes reset on restart | D-037 `b6a9bb3` | Default (database) | `test_budget_persistence.py` | All, SQLite + PG |
| No per-key concurrency limit or batch class | D-034 `22ca369` | `max_concurrent`, `priority` on a key | `test_key_limits.py` | All, SQLite + PG |
| No way to try a config without minting keys | D-042 `b8e0228` | `GATEWAY_DEV_MODE=true` or `./start-gateway.sh --dev`; `GATEWAY_PROFILE=production` refuses it | `test_access_modes.py::TestTestMode`, `::TestProductionProfile` | All |

### Still open

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| P1 | Config-file keys can't carry model, endpoint or RPM limits | `config.py` `ApiKeyConfig` | YAML keys support `max_concurrent` and `priority` (D-034) but not `allowed_models`, `allowed_endpoints` or `rate_limit_rpm`. DB-created keys support all of them. |
| P1 | No per-key token budget override | `policy/enforcer.py` (`daily_limit_override=None # TODO`) | Every key shares its tier's limit. |
| P1 | Budget reservations are per process | `policy/token_budget.py` | With several gateway processes sharing a budget, each can admit up to the remaining budget once (D-043, Scope). |
| P2 | CORS `*` with credentials | `settings.py` `cors_origins`, `main.py` | Default `["*"]` together with `allow_credentials=True`. Default it to the dashboard origin. Compose serves the dashboard on the same origin (D-044), so it doesn't need CORS. |
| P2 | All keyless traffic shares one rate-limit bucket | `policy/rate_limiter.py` | Per-client-IP buckets need trusted proxy headers (section 4). |

## 2. Real-time request processing

### Fixed

| Gap | Decision, commit | Requires | Proven by | Platform |
|---|---|---|---|---|
| Health checks ran inside requests to unhealthy endpoints | D-031 `bed2e42` | Default | `test_circuit.py` | All |
| No concurrency limit, queue or overflow across endpoints | D-032 `21a6cb9` | `max_concurrent` per endpoint; `resolution.strategy: least_loaded` optional | `test_admission.py`, `test_capacity_limits.py` | All |
| httpx pool not sized; pool waits counted as engine failures | D-033 `25413f0` | `max_concurrent` per endpoint | `test_capacity_limits.py` | All |
| Rate limits and slots per process only | D-035 `e251745` | `GATEWAY_REDIS_URL` and the `redis` extra; in-memory without it | `test_shared_state_redis.py` | Redis |
| Two DB writes before each authenticated request completed; `database is locked` 500s at 50 concurrent | D-038 `0c7148a`, D-040 `4390209` | Default (`GATEWAY_DB_AUDIT_DURABILITY=auto`, key cache 30 s) | `test_intent_log.py`, `test_key_cache.py` | All, SQLite + PG |
| Streaming not charged to budgets; mid-stream failures audited as success; disconnects left no record | D-004 `c41d764` | Default | `test_stream_recorder.py` | All |
| Stream errors lost their cause; failover on 4xx; abandoned upstream streams not closed | D-012 `abfa1c5` | Default | `test_streaming_phase1.py` | All |
| Pre-stream failures returned HTTP 200 | D-013 `abfa1c5` | Default | `test_streaming_phase1.py` | All |
| Stalled stream could hang for an hour | D-014 `abfa1c5` | Default (`stream_idle_timeout` 60 s) | `test_streaming_phase1.py` | All |
| Tool calls disabled streaming on the OpenAI route | D-015 `abfa1c5` | Default | `test_streaming_phase1.py` | All |
| Batch-shaped requests reduced to one (vLLM embeddings, prompt lists, `n`) | D-017 `0ec8d7b` | Default | `test_batch_requests.py` | All |
| `/v1/completions` ignored `stream: true` | D-018 `868947a` | Default | `test_completions_streaming.py` | All |
| Inline images dropped on the OpenAI route | D-011 `df17d9b` | Default | `test_wire.py` | All |
| Models on OpenAI-type endpoints not discovered; endpoint API keys not sent | D-036 `28cc42c` | `api_key` / `api_key_env` on the endpoint | `test_discovery.py` | All |

### Still open

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| P1 | Single worker by default | `start-gateway.sh`, `docker/Dockerfile` | One event loop: CPU work on the hot path (regex injection scan, PII scan, large JSON bodies) competes with streaming. Several workers are supported with `GATEWAY_REDIS_URL` (D-035), with budgets shared through the database (D-037). |
| P2 | `max_retries` configured but unused | `providers/base.py` | Stored on every adapter and never read. Implement retries for idempotent calls, or remove the setting. |
| P2 | No request ID returned to the client | — | `request_id` is audited and is the response `id`, but there's no `X-Request-ID` header, and an inbound one isn't honored. |
| P2 | No request-size bounds | `models/openai.py`, `models/ollama.py` | No limit on message count, body size or inline image size. The regex and PII scans run synchronously over the whole body. |
| P2 | No offline batch API | — | No `/v1/files` or `/v1/batches`. It would build on the batch priority class (D-034). |

## 3. Audit and data handling

### Fixed

| Gap | Decision, commit | Requires | Proven by | Platform |
|---|---|---|---|---|
| Raw PII in audit bodies | D-005 `27645da` | PII detection on, or body storage off (the default) | `test_pii.py`, `test_storage.py` | All, SQLite + PG |
| Embeddings sent unscrubbed text to the engine | D-041 `df17d9b` | Scrubbing on for the route | `test_wire.py` | All |
| Security scans kept every prompt, forever | D-041 `df17d9b` | Default (`GATEWAY_SECURITY_STORE_MESSAGES=none`, `GATEWAY_SECURITY_RETENTION_DAYS=90`) | `test_security_store.py` | All |
| Audit writes failed silently; gateway started without a database | D-006 `eb0e274` | Default (`GATEWAY_DB_REQUIRED=true`) | `test_audit_durability.py` | All, SQLite + PG |
| Audit rows lost or duplicated on a crash | D-038 `0c7148a`, D-040 `4390209` | Default. `grouped` or `sync` durability also covers power loss (D-038) | `test_intent_log.py` (includes racing drainers) | All, SQLite + PG |
| Audit intent log broke on Windows (file locks, deleting open files) | D-045 `5b7e7e3` | Default | `test_intent_log.py` on the Windows job | Windows, SQLite |
| Retention: first cleanup 24 h after boot; one error stopped it for good | D-041 `df17d9b` | Default | — (loop runs at startup and logs errors) | — |
| Retention: `GATEWAY_DB_RETENTION_DAYS=0` ("keep") was rejected | this revision | Default | `test_settings.py` | All |
| Compose lost all state on `down`/`up` | D-044 `e065815` | `GATEWAY_ADMIN_API_KEY` (Compose refuses to start without it) | Manual: image build plus `down`/`up` (D-044). Not in CI. | Docker on Linux |

### Still open

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| P1 | Security scans dropped silently under load | `security/analyzer.py` `queue_request` | A full queue drops the scan and increments an internal counter only. Expose it as a metric and alert on it. |
| P1 | PII scrubbing truncates long messages | `security/pii.py` `scan` | Text is scanned up to 100k characters, and the scrubbed text is built from the cut copy. With scrubbing on, the tail of a longer prompt never reaches the model. Stored copies are marked `[TRUNCATED: not scanned for PII]`. Scrub in windows, or reject oversized input. |
| P1 | `usage_daily` never filled automatically | `storage/audit.py` `aggregate_daily_usage` | No scheduler calls it. |
| P2 | Fallback and routing reason not stored | `storage/schema.py` `audit_log` | `was_fallback` and `attempted_providers` exist on `DispatchResult` but aren't persisted. The operator view needs them (section 5). |

## 4. Operability

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| P1 | `/health` never fails | `routes/health.py` | It always returns 200, "degraded" when every endpoint is down. It reports the database, audit backlog, circuits and access mode, but never as a failing status. Split it into `/livez` and `/readyz`, with 503 when not ready. |
| P1 | No graceful drain for streams | `docker/Dockerfile`, `start-gateway.sh` | No `--timeout-graceful-shutdown`, so deploys cut active streams. The audit intent log and budget sync do flush on a clean shutdown. |
| P1 | No trusted-proxy support | `security/access.py` | A request with proxy headers never counts as local (D-042), which is safe. But behind a proxy, `source_ip`, `allowed_networks` and `scan_allowlist_ips` all see the proxy's address. Compose relies on the admin key for this reason (D-044). |
| P1 | No config hot-reload | — | YAML endpoints, keys and limits need a restart. Budgets survive it (D-037). In-memory rate-limit windows reset unless Redis is configured (D-035). |
| P2 | `/metrics` unauthenticated | `routes/health.py` | It exposes client IDs and model names. Fine on a private network; otherwise put it on a separate port. |
| P2 | Not yet in place | — | Helm chart, HA guide, backup and restore runbook, upgrade guide, secret rotation, Vault/KMS, egress allowlist for hybrid mode. |

## 5. Operator legibility (dashboard)

Outside feedback (screenshots only, Oct 2026) raised two issues, and both hold up against the
code:

1. **The dashboard shows state but not priority.** Nothing collects the panels' signals into
   "what needs attention first". It now shows banners for test mode, solo mode and a
   missing admin key (D-042), and circuit state per endpoint (D-031). There's still no
   overall triage view.
2. **Signals lack meaning and a next action.** Request detail doesn't say why an endpoint was
   chosen, whether fallback happened, or whether a PII or security finding needs review.

| Pri | Item | Backend prerequisite |
|-----|------|----------------------|
| P1 | **Needs Attention** strip: open circuits (and whether overflow exists), unclassified models, flagged-unscrubbed PII, critical security alerts, audit backlog or scan drops, keys near budget | Mostly none. The scan-drop counter needs exposing (section 3). |
| P1 | Request detail: routing reason, fallback path, budget impact, linked PII and scan findings | Store `was_fallback`, `attempted_providers`, a routing reason and weighted tokens on `audit_log`. Join `pii_events` and `security_scans` by `request_id`. |
| P2 | Next-action buttons next to each signal (classify model, label scan, enable scrubbing, adjust limit) | Admin-only write endpoints exist (D-002). |
| P2 | PII "review required" workflow | Doesn't exist. Flag-only mode is detection, not a queue. |

---

## Deployment modes and client profiles

| | Status | Requires |
|-|--------|----------|
| **Solo**: one person, one machine | Ready. | `auth.enabled: false` (local only by default) |
| **Mode A, local-only** (internal runtimes) | Ready for a single organization. | `auth.enabled: true`, `GATEWAY_ADMIN_API_KEY`, keys per client |
| **Mode B, hybrid** (local plus external fallback) | Not ready: no egress allowlist. Fallback does respect key and environment endpoint restrictions (D-003). | — |
| **Profile A**: SMB, 1–2 GPU servers, Compose, SQLite | Ready. | `GATEWAY_ADMIN_API_KEY`; scrubbing on for routes that carry PII |
| **Profile B**: mid-market, several workers, PostgreSQL, per-team keys | Ready, with the section 4 operations gaps. | `GATEWAY_DB_URL` (PostgreSQL), `GATEWAY_REDIS_URL` for more than one process |
| **Profile C**: MSP, multi-tenant | Not ready. | Tenant isolation (D-039, proposed), per-tenant views |

**Upgrading from before D-042:** keyless clients on other machines (LocalClaw, stock Ollama
clients) need `auth.anonymous.enabled: true`, with their address in
`auth.anonymous.allowed_networks`. With `auth.enabled: true`, admin routes need
`GATEWAY_ADMIN_API_KEY`.

## Recommended order

1. ~~**P0: make policy and audit claims true.**~~ Done (D-001 to D-006, D-041 to D-043).
2. ~~**Real-time capacity:** circuit breaker, admission and overflow, pool sizing, per-key
   limits, shared state, persisted budgets, intent log, key cache.~~ Done (D-031 to D-040).
3. **Operations:** `/livez` and `/readyz`, graceful drain, trusted proxies, `X-Request-ID`,
   the scan-drop metric, windowed PII scrubbing.
4. **Operator view:** the Needs Attention strip, then routing and fallback detail.
5. **Tiers:** decide D-039 (Server mode, tenant isolation), then Helm, HA, backup and upgrade
   docs, and the egress allowlist for hybrid mode.

## What's solid

- Clean adapter boundary; OpenAI and Ollama API compatibility, with full Ollama passthrough
  (`format`, `options`, `think`, tools, images).
- Capacity:
  - circuit breakers and admission control with overflow;
  - per-key concurrency and a batch class;
  - optional Redis for several processes.
- Audit trail:
  - the exactly-once intent log, crash-tested with `kill -9` (D-038);
  - hashed PII events;
  - an async guard model with a labeling loop.
- Secure defaults, with a test mode and a production profile that refuses unsafe settings.
- Structured logging; Prometheus metrics for latency, TTFT, tokens per second, audit
  backlog and circuit state.
- CI: lint, format, coverage, PostgreSQL and Redis on Linux, and SQLite on Windows. Non-root
  container.
