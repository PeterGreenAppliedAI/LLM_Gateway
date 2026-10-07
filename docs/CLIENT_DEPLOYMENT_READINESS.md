# Client Deployment Readiness Assessment

_Last reviewed: 2026-10-06, against `main` at `a17a0d8`. Test suite: 562 passing._
_Decisions behind the fixes: [DECISIONS.md](DECISIONS.md)._

This replaces the January 2026 assessment. Much of what that version listed as
missing has since shipped (see [What changed since January](#what-changed-since-january)).
This pass reviews the code against what a production gateway needs to process
real-time traffic, and folds in outside feedback on the operator dashboard.

## Verdict

The gateway works well as a **single-node, trusted-network gateway for a small team**
(client Profile A below). With the P0 fixes below it can back its policy and audit claims for a single
organization; multi-tenant use still needs per-tenant views, and real-time load needs the
section 2 capacity work. The P0 blockers were not missing features. They were places
where an existing feature looked enforced or recorded but could be bypassed or silently
skipped:

1. ~~**Per-key policy can be bypassed** by sending no key at all~~ (fixed; section 1).
2. ~~**Environments (dev/prod) are configured but never applied** to routing~~ (fixed; section 1).
3. ~~**Any client key can change budgets, delete alerts, and read every other client's traffic**~~ (fixed; section 1).
4. ~~**Streaming requests are not counted against token budgets**, and mid-stream
   failures are recorded as successes~~ (fixed; section 2).
5. ~~**The audit trail is best-effort.** Failed writes are dropped, disconnected streams leave no
   record, and the default start script stores raw PII in request bodies~~ (fixed; section 3).

All five are fixed on `docs/readiness-gap-report`. What remains is P1 capacity and
streaming hardening (section 2) and operations (section 4).

Severity key: **P0** means a guarantee the product claims does not hold. **P1** means it
degrades or fails under real load. **P2** is hardening or polish.

---

## 1. Policy and access control

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| ~~P0~~ | **Fixed:** keyless requests bypass per-key policy | `routes/dependencies.py`, `config.py` `AnonymousAccessConfig` | `auth.anonymous` now governs keyless traffic: disable it, or restrict its models, endpoints and RPM. Still allowed and unrestricted by default for stock Ollama clients; startup logs a warning in that state. |
| ~~P0~~ | **Fixed:** endpoint allowlist checked the requested endpoint, not the one used | `dispatch/dispatcher.py` `_permitted` | The dispatcher now enforces `InternalRequest.allowed_endpoints` on catalog routing, the default endpoint, fallback, streaming order and `endpoint/model` pins. |
| ~~P0~~ | **Fixed:** environments were never applied | `routes/dependencies.py` `resolve_access_scope` | Environment endpoints and approved models are now enforced, intersected with the key's allowlist. A key bound to an environment can't switch with `X-Environment`, and unknown names are refused. |
| ~~P0~~ | **Fixed:** control-plane writes needed only a client key | `routes/dashboard.py`, `routes/security_api.py` | Budget, aggregation, alert and labeling writes require the admin key. |
| ~~P0~~ | **Fixed (as operator-only):** no tenant scoping on dashboard reads | same | Dashboard and security reads now require the admin key, so client keys can't read other clients' traffic. Per-tenant self-service views (a client reading only its own usage) are still open for Profile C. |
| P1 | Admin falls back to any key | `routes/dependencies.py` `require_admin` | If `GATEWAY_ADMIN_API_KEY` is unset, any client key gets full operator access (keys, budgets, dashboard). Startup now logs a warning; consider failing startup instead when auth is on. |
| P1 | Config-file keys can't carry per-key limits | `config.py` `ApiKeyConfig` | Only DB-created keys support `allowed_models`, `allowed_endpoints` and `rate_limit_rpm`. YAML keys, which the README shows, get global limits only. |
| P1 | Budget pre-check passes when `max_tokens` is omitted | `policy/enforcer.py:193` | `estimated_tokens = max_tokens or 0`, so a key at 99% of its daily budget can still send unbounded requests. Estimate from prompt size plus a default completion size, or reject once the key is at its limit. |
| P1 | Per-key token budget override is a TODO | `policy/enforcer.py` | `daily_limit_override=None  # TODO`. Every key shares the tier limits. |
| P2 | CORS `*` with credentials | `main.py:203` | `allow_origins=["*"]` together with `allow_credentials=True`. Default to the dashboard origin. |

## 2. Real-time request processing

The request path does a lot right: separate connect and read timeouts, upstream
cancellation when the client disconnects (non-streaming), passing upstream 4xx
errors through instead of failing over, first-chunk peeking before committing to
a stream, an embedding admission queue, and TTFT and tokens-per-second metrics.
The gaps are in capacity management and in streaming correctness.

### Throughput and capacity

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| P1 | No load balancing across endpoints | `dispatch/dispatcher.py` `resolve_endpoint` | When several endpoints have the same model, every request goes to the first in priority order until it fails. Other GPUs sit idle while the first one queues. Add least-in-flight or weighted round-robin among healthy candidates, with priority as the tiebreaker. |
| P1 | No concurrency limit or queue for chat/generate | routes, dispatcher | Only embeddings are admission-controlled. Chat traffic is limited by request rate, not by how many requests are in flight, so a burst of long generations stacks up inside Ollama (`OLLAMA_NUM_PARALLEL`) or the httpx pool, where the gateway can't see it, and then times out. Add a per-endpoint in-flight cap with a short bounded wait, then fail fast with 429/503 and `Retry-After`. |
| P1 | httpx connection pool not configurable | `providers/ollama.py:60`, `providers/openai.py:119` | Default `Limits` (100 connections, 20 keep-alive) per endpoint, with the pool wait inheriting the read timeout (up to 1h). Above 100 concurrent requests, requests wait silently. Expose `max_connections` and `max_keepalive`, and set a short pool timeout. |
| P1 | Burst cap is global and can't be overridden | `policy/rate_limiter.py` | `burst_limit` (default 10 per 10s) applies to every key and ignores `rate_limit_rpm`. A key granted 600 RPM is effectively capped at 60. All keyless traffic shares the single `default` bucket, so one noisy keyless client throttles every keyless client. |
| P1 | Rate limits and budgets are in process memory | `policy/rate_limiter.py`, `policy/token_budget.py` | Counts reset on every restart and aren't shared between uvicorn workers or replicas. Budget tier changes made from the dashboard are lost on restart, even though the README says "no restart needed". Persist budget usage and assignments to the DB, and add a Redis-backed limiter before running more than one process. |
| P1 | Single worker | `start-gateway.sh`, `docker/Dockerfile` | One uvicorn process means one event loop, so CPU work on the hot path (regex injection scan, PII scan, JSON for large bodies) competes with streaming. Fine for evaluation. Production needs multiple workers, which depends on the in-memory state above. |
| P2 | `max_retries` is configured but unused | `providers/base.py` | Stored on every adapter and never read. Either implement retries for idempotent calls (embeddings, connection errors before the first byte) or remove the setting. |

### Hot-path latency

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| P1 | Two DB writes before the request can complete | `storage/keys.py:185`, `storage/audit.py` | Each DB-backed key check runs a SELECT plus an UPDATE and commit of `last_used_at`, with no cache. The audit insert is then awaited before the response returns, and before `[DONE]` on streams. On SQLite (`NullPool`, a new connection per write) these all contend for the single writer lock, adding latency and, past the default 5s lock wait, "database is locked" errors that drop audit rows. Cache validated keys for ~30–60s, batch `last_used_at`, and move audit writes to a bounded background writer. |
| P1 | PII audit write runs before dispatch | `routes/openai.py` (chat), `routes/ollama.py` | When PII is detected, `log_pii_events` is awaited before the upstream call starts, so DB latency is added directly to TTFT. |
| P1 | Unhealthy endpoint triggers a health check inside the request | `dispatch/dispatcher.py:431` | Every request routed to an endpoint marked unhealthy runs a blocking health check (up to 10s) first. With N concurrent requests that's N probes against a dead box. Replace with a circuit breaker: open after K failures, one half-open probe, then close. |

### Streaming correctness

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| ~~P0~~ | **Fixed:** streaming usage wasn't charged to token budgets | `routes/stream_recorder.py` | All streaming routes charge budgets through a shared `StreamRecorder`, including failed and abandoned streams (estimated from chunks when no usage arrives). |
| ~~P0~~ | **Fixed:** mid-stream failures recorded as success | same | `finish_reason=ERROR` is now audited as `status=error, error_code=stream_error`; Ollama streams send the client an error instead of a normal `done`. |
| ~~P1~~ | **Fixed:** client disconnect mid-stream left no audit record | same | Disconnects close the upstream stream and write a `client_disconnected` row with partial content and tokens, shielded against Starlette's cancel scope. |
| ~~P1~~ | **Fixed (D-012):** upstream stream not closed on failover | `dispatch/dispatcher.py` | Abandoned streams are closed, and closing the returned stream closes the upstream connection. |
| ~~P1~~ | **Fixed (D-012):** stream errors lost their cause | `providers/streaming.py` | Error chunks carry `error`/`error_code`; streaming failover now stops on upstream 4xx and the 503 includes the last cause. |
| ~~P1~~ | **Fixed (D-013):** pre-stream failures returned HTTP 200 | `routes/stream_recorder.py` `start` | Endpoint chosen and first chunk received before headers; failures return real 4xx/503. |
| ~~P1~~ | **Fixed (D-015):** tool calls disabled streaming on the OpenAI API | `routes/openai.py`, `models/openai.py` | Tool calls stream as complete-call deltas; OpenAI/vLLM adapters reassemble streamed fragments. |
| ~~P1~~ | **Fixed (D-014):** stalled stream could hang for up to an hour | `providers/streaming.py` | First chunk gets the endpoint timeout; then `stream_idle_timeout` (default 60s). |
| ~~P2~~ | **Fixed:** response text accumulated with `+=` | same | Now a list join. Still accumulated when bodies aren't stored. |

Also fixed in phase 1 (D-016): streams ignored endpoint priority, `target_endpoint` and
`fallback_allowed: false`; endpoint order changed between restarts (set ordering); and
`connect_timeout` was never passed to endpoint adapters.

### Request handling

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| P1 | OpenAI-route images silently dropped | `models/openai.py:66` `content_as_str` | Content-part arrays are flattened to text only, so `image_url` parts never reach the model. Vision works only through the Ollama API. The request should either pass images through or be rejected. |
| P2 | No request ID returned to the client | — | `request_id` is generated and audited but never sent in a response header (`X-Request-ID`), so clients can't correlate a failure with the audit log. Honor an inbound `X-Request-ID` too. |
| P2 | No request-size bounds | `models/openai.py`, `models/ollama.py` | No limit on message count, body size or image payload size. The regex and PII scans run synchronously over the whole body. |
| P2 | Proxy headers not trusted | `start-gateway.sh`, `Dockerfile` | Behind nginx or Caddy, without `--proxy-headers --forwarded-allow-ips`, `request.client.host` is the proxy's address, which breaks `scan_allowlist_ips` and audit `source_ip`. |

## 3. Audit and data handling

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| ~~P0~~ | **Fixed:** default start script stored raw PII | `security/pii.py` `redact`, `main.py` | With PII detection on, audit bodies **and `security_scans.messages`** (which stored every prompt regardless of body settings) are redacted before persisting, even in flag-only mode. Startup warns if bodies are stored with detection off. |
| ~~P0~~ | **Fixed:** audit writes failed open, silently | `storage/audit.py` | Audit and PII-event writes retry, then go to a fsynced spill file replayed on startup; `gateway_audit_write_failures_total{table,outcome}` counts spills and losses. Still open: an optional fail-closed mode, and moving writes off the request path (section 2). |
| ~~P0~~ | **Fixed:** gateway started without a database | `main.py` lifespan | Startup fails unless `GATEWAY_DB_REQUIRED=false`. `/health` still doesn't check the DB (section 4). |
| P1 | Retention cleanup is fragile | `main.py:153`, `settings.py:54` | The first cleanup runs 24h after boot, so a gateway restarted daily never cleans up. One exception kills the loop for good. The "0 = no cleanup" option is rejected by `ge=1`. `usage_daily` is never filled automatically because `aggregate_daily_usage` has no scheduler. |
| P1 | Security scans dropped silently under load | `security/analyzer.py` `queue_request` | When the queue is full the scan is dropped and only an internal counter goes up. Expose it as a metric and alert on it, since "every request is scanned" stops being true. |
| P1 | PII scrubbing truncates long messages | `security/pii.py` `scan` | Found during the fixes. Text is cut at 100k characters before scanning, and the scrubbed text is built from the cut copy, so with scrubbing on, the tail of a long prompt that contains PII never reaches the model. Scrub in windows, or reject oversized input explicitly. |
| P2 | No fallback or routing reason recorded | `storage/schema.py` `audit_log` | `DispatchResult.was_fallback` and `attempted_providers` exist but aren't stored. Needed for the operator view in section 5. |

## 4. Operability

| Pri | Gap | Where | Detail |
|-----|-----|-------|--------|
| P1 | `/health` never fails | `routes/health.py:71` | Always returns 200: "degraded" when every endpoint is down, with no DB check. Split it into `/livez` (process up) and `/readyz` (DB reachable and at least one healthy endpoint), and return 503 when not ready. |
| P1 | No graceful drain for streams | `Dockerfile`, `start-gateway.sh` | No `--timeout-graceful-shutdown`. Deploys cut active streams mid-generation. |
| P1 | No config hot-reload | — | Endpoints, keys and limits from YAML need a restart, which (see above) also resets rate limits and budgets. |
| P2 | `/metrics` unauthenticated | `routes/health.py` | Exposes client IDs and model names. Fine on a private network; document it or put it on a separate port. |
| P2 | Not yet in place from January | — | Helm chart, HA guide, backup/restore runbook, upgrade guide, secret rotation guide, Vault/KMS integration, egress allowlist for hybrid mode. |

## 5. Operator legibility (dashboard)

Outside feedback, from screenshots only (Oct 2026), raised two issues. Both hold up against the code:

1. **The dashboard shows state but not priority.** Each panel reports its own signals (endpoint
   Healthy/Unhealthy badge, unclassified-model count, "flagged only" PII card), but nothing
   collects them into "what needs attention first". Security alerts are the only signal with
   severity, and only inside their own tab.
2. **Signals lack meaning and a next action.** Request detail shows the model and endpoint used,
   but not why that endpoint was chosen, whether fallback happened, whether the request stayed
   in budget, or whether a PII or security finding needs review.

What it takes:

| Pri | Item | Backend prerequisite |
|-----|------|----------------------|
| P1 | **Needs Attention** strip on the Dashboard tab: unhealthy endpoints (with whether a fallback exists), unclassified models (with "default multiplier applied"), flagged-unscrubbed PII, critical security alerts, audit or scan drops, keys near budget | Mostly none. The data is already loaded in `App.tsx`. Audit and scan drop counters need exposing (section 3). |
| P1 | Request detail: routing reason, fallback path, budget impact, linked PII and scan findings | Store `was_fallback`, `attempted_providers`, a routing-reason enum and weighted tokens on `audit_log`. Join `pii_events` and `security_scans` by `request_id`. |
| P2 | Next-action affordances (classify model, label scan, scrub-enable route, adjust limit) next to each signal | Admin-only write endpoints (section 1). |
| P2 | PII "review required" workflow | Doesn't exist today. Flag-only is detection without scrubbing, not a queue. Build a review state on `pii_events` before the UI promises review. |

Do the section 1–3 fixes first. A triage layer that reports "within policy"
while keyless or streaming traffic bypasses policy would be wrong.

---

## What changed since January

| Item (January status) | Now |
|-----------------------|-----|
| Alembic migrations (missing) | **Done.** `alembic/versions/`, 2 revisions |
| DB-backed key validation (missing) | **Done.** `storage/keys.py`, SHA-256 hashed |
| Per-key allowed models, endpoints and RPM (schema only) | **Partly done.** Enforced for DB keys; endpoint check incomplete, keyless bypass (section 1) |
| Per-key quotas (schema only) | **Partly done.** Daily token budgets with cost tiers; no per-key override, streaming not counted |
| Key management API and UI (future) | **Done.** Create, list and revoke; no update (`PATCH`) or expiry setting in API |
| PII scrubbing hooks (missing) | **Done.** Detect and scrub per route, hashed audit |
| Prompt-injection defense | **Added.** Regex scanner plus async guard model, labeling and export |
| Embedding backpressure | **Added.** Admission queue instead of 429 bursts |
| Environment separation (listed as solid) | **Now enforced** (was not; section 1) |
| TLS, secrets vault, egress allowlist, Helm, hot-reload | Still missing |

## Recommended order

**Step 1: make policy and audit claims true (P0)**
1. ~~Require keys on inference when auth is enabled; run the endpoint allowlist check on the endpoint actually chosen; wire environments into dispatch and stop `X-Environment` overriding a key's environment.~~ Done.
2. ~~Admin-only control-plane writes; scope dashboard reads per client (admin sees all).~~ Done (dashboard is operator-only; per-tenant views still open).
3. ~~Charge streaming usage to budgets; record mid-stream errors and disconnects truthfully.~~ Done.
4. ~~Don't store unscrubbed bodies; durable audit writes; refuse to start or report not-ready without a DB.~~ Done (readiness probe still open).

**Step 2: real-time capacity (P1)**
5. Per-endpoint in-flight caps with fail-fast backpressure; least-in-flight balancing across endpoints.
6. Key-validation cache; background audit writer; circuit breaker instead of in-request health probes.
7. ~~Fix stream lifecycle: close abandoned iterators, carry error causes, return a real status before streaming starts, separate first-chunk and between-chunk timeouts, streaming tool calls on the OpenAI route.~~ Done (phase 1).
8. Persist budgets and assignments; Redis limiter; then multiple workers.

**Step 3: operations and operator view**
9. `/livez` and `/readyz`, graceful drain, proxy headers, `X-Request-ID`.
10. Needs Attention strip and routing/fallback detail in the dashboard.
11. Helm, HA, backup and upgrade docs, egress allowlist for hybrid mode.

---

## Deployment modes and client profiles

| | Status |
|-|--------|
| **Mode A, local-only** (internal runtimes only) | Ready for a single trusted team. Not ready where keys must isolate teams (section 1). |
| **Mode B, hybrid** (local plus external fallback) | Not ready. PII scrubbing exists, but there's no egress allowlist, and fallback can reach any endpoint regardless of key or environment restrictions. |
| **Profile A**: SMB, 1–2 GPU servers, Docker Compose, SQLite | Usable today with `PII_SCRUB_ENABLED=true` or body storage off. |
| **Profile B**: mid-market, Kubernetes, Postgres, per-team keys | Needs steps 1 and 2. |
| **Profile C**: MSP, multi-tenant | Needs steps 1–3 plus tenant scoping throughout. |

## What's solid

- Clean adapter boundary; OpenAI and Ollama API compatibility with full Ollama passthrough (`format`, `options`, `think`, tools, images).
- Endpoint pins honored; upstream 4xx passed through, not failed over (non-streaming).
- Upstream cancellation when non-streaming clients disconnect.
- Separate connect and read timeouts; per-endpoint read timeout.
- Hashed PII audit design; async guard model with a labeling loop.
- Structured logging, Prometheus histograms for latency, TTFT and tokens per second.
- 562 tests, CI with lint, format and coverage gates; non-root container.
