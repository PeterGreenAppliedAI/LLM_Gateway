# Decisions & Fixes Log

A running record of **what works, what doesn't, and how we fixed it**: problems we hit,
approaches we tried or rejected, the fix we shipped, and how we know it works. Read the
relevant entry before changing behavior it covers, so we don't repeat a dead end.

**Rules**
- Newest entries last. Never rewrite history: if a fix is reversed or replaced, add a new
  entry and mark the old one *Superseded by D-0xx*.
- Record failures too. An approach that didn't work is as valuable as one that did.
- "What works now" should point to the test that proves it.

**Statuses:** Proposed (needs sign-off) → Accepted (agreed, not built yet) → Implemented →
Superseded.

**Entry template**
```
## D-0xx: <short title>
- Status / date / commits
- Problem: what was broken or missing, and how it showed up
- What didn't work: rejected options and attempts that failed, with the reason
- Fix: what we did
- What works now: verified behavior and the test that proves it
- Trade-offs: what it costs
- Revisit when: the signal that should reopen this
```

Related: [CLIENT_DEPLOYMENT_READINESS.md](CLIENT_DEPLOYMENT_READINESS.md) (gap report),
[ARCHITECTURE_ROADMAP.md](ARCHITECTURE_ROADMAP.md) (Phase 7: why prompt-injection scanning
is observe-only).

---

## D-001: Keyless requests bypassed per-key policy

- **Status:** Implemented, 2026-10-06, `bbfe4b5`
- **Problem:** With auth on, a request with no key ran as client `default` with no allowlists and
  no per-key RPM. Any client could drop its key to escape its own restrictions.
- **What didn't work:**
  - *Requiring a key on every inference request:* rejected. LocalClaw and other stock
    Ollama clients deliberately send no key, and the existing tests assert that. It would
    break them on upgrade.
  - *Leaving it as "documented behavior":* rejected. Per-key limits that can be bypassed
    aren't limits.
- **Fix:** A new `auth.anonymous` block: `enabled`, `allowed_models`, `allowed_endpoints`,
  `rate_limit_rpm`. Keyless traffic gets that policy. Startup warns while it is unrestricted.
- **What works now:** keyless requests are refused when disabled, and a keyless request for a model
  outside the allowlist gets 403 (`tests/test_ollama_routes.py::TestAuthSplit`).
- **Trade-offs:** The default is still open. Closing it is a config change the operator has to make.
- **Revisit when:** No keyless clients remain. Then default `enabled: false`.

## D-002: Any client key could run the control plane

- **Status:** Implemented, 2026-10-06, `d7ed7dc`, `a0e31b8`
- **Problem:** Budget tiers and assignments, alert deletion and scan labeling accepted any client
  key. Dashboard reads showed every client's prompts and responses to any key.
- **What didn't work:**
  - *Switching writes to `require_admin` alone:* that exposed an existing bug. With
    `GATEWAY_ADMIN_API_KEY` set, the admin key **failed** on the dashboard's read endpoints,
    because they only accepted client keys. The dashboard sends one key for everything, so
    either reads or writes were always broken.
  - *Filtering dashboard reads per tenant:* deferred. It touches 16 endpoints and their
    aggregate queries, which is a feature of its own.
- **Fix:** The admin key is accepted everywhere a client key is, and all dashboard, security,
  budget and key endpoints require admin. With no admin key set, any valid key still works,
  and startup warns.
- **What works now:** a client key gets 401 on reads and writes when an admin key is set; the
  admin key gets 200 (`TestAuthSplit::test_control_plane_writes_require_admin`).
- **Trade-offs:** Clients have no self-service usage view.
- **Revisit when:** Multi-tenant or MSP deployments need per-client views.

## D-003: Endpoint and environment restrictions were checked in the wrong place

- **Status:** Implemented, 2026-10-06, `0f70f4a`
- **Problem:**
  - The key's endpoint allowlist was only compared against `preferred_provider`. Model-based
    routing, priority order, fallback and `endpoint/model` pins could all reach excluded
    endpoints.
  - Environments were never applied: `get_environment()` existed but nothing called it.
  - An unknown `X-Environment` fell through to "no environment", meaning no restrictions.
- **What didn't work:** checking restrictions in the route or the enforcer before dispatch. The
  endpoint is only chosen inside the dispatcher, and changes again on fallback, so any check
  before that point misses the real choice.
- **Fix:** Each request carries `allowed_endpoints` (key allowlist ∩ environment endpoints),
  computed by `resolve_access_scope`. The dispatcher filters every candidate against it. A
  key bound to an environment can't switch it, and unknown environments get 403.
- **What works now:** the four routing tests in `tests/test_resolution.py::TestAllowedEndpointsEnforced`
  fail on the old dispatcher and pass now; `tests/test_ollama_routes.py::TestEnvironmentScope`
  covers environments.
- **Trade-offs:** Keys without an environment can still pick one with `X-Environment`.
- **Revisit when:** Environments become a security boundary for keyless clients.

## D-004: Streams escaped budgets and reported failures as success

- **Status:** Implemented, 2026-10-06, `c41d764`
- **Problem:**
  - Streamed usage was never charged to budgets.
  - A provider error chunk mid-stream was audited as `success`.
  - A client disconnect left no audit row, because `CancelledError`/`GeneratorExit` skip
    `except Exception`.
- **What didn't work, for the record:**
  - *Fixing each of the three stream generators in place:* the same accounting was copied
    three times, which is how they drifted apart. Replaced with one `StreamRecorder`.
  - *Testing the disconnect path with a plain asyncio cancel:* it passed with or without the
    shield. asyncio delivers a cancel once, so later awaits run. Starlette (ASGI < 2.4)
    cancels through an **anyio cancel scope**, which re-cancels every await. Only an
    anyio-scope test reproduces production.
  - *Testing with a bare `AsyncMock` audit logger:* it also passed without the shield. The
    mock never suspends, so the cancellation is never re-delivered, and `await_count` counts a
    write that *started* even if it was cancelled. The fake writer now sleeps like a real DB
    call and records completion.
- **Fix:** `routes/stream_recorder.py` records audit, metrics and budget exactly once on every
  exit path. Disconnects close the upstream stream and write `client_disconnected` inside
  `anyio.CancelScope(shield=True)`. Without upstream usage, completion tokens are estimated as one
  per chunk.
- **What works now:** `tests/test_stream_recorder.py`. The cancel-scope tests fail when the shield is
  removed and pass with it.
- **Trade-offs:** Token estimates are approximate when a stream is cut short.

## D-005: Raw PII was stored despite hashed PII events

- **Status:** Implemented, 2026-10-06, `27645da`
- **Problem:** In flag-only mode (detection on, scrubbing off, which is the start script's default),
  PII was hashed in `pii_events` but stored in plaintext in `audit_log` bodies. It was also stored
  in `security_scans.messages`, which saves every prompt whatever the body-storage setting.
- **What didn't work:** *Turning scrubbing on in the start script:* rejected. That changes what the
  model receives. Scrubbing before the model and scrubbing at rest are separate choices.
- **Fix:** `PIIScrubber.redact()` runs on everything persisted (audit bodies and scan messages)
  whenever detection is on, regardless of scrubbing. Text beyond the scan limit is dropped rather
  than stored unscanned, and if redaction fails nothing is stored.
- **What works now:** `tests/test_pii.py::TestRedactForStorage`,
  `tests/test_storage.py::...test_bodies_redacted_before_storage`,
  `tests/test_security_store.py::test_messages_redacted_before_storage`.
- **Trade-offs:** Stored bodies and guard-model training data contain placeholders (`[EMAIL]`).
- **Known issue found along the way:** with scrubbing *on*, `scan()` cuts text at 100k characters, so
  the end of long prompts never reaches the model. Logged as a P1 in the readiness report and
  not yet fixed.

## D-006: Audit writes failed silently; the gateway ran without a database

- **Status:** Implemented, 2026-10-06, `eb0e274`
- **Problem:** A failed audit insert was logged and dropped. If database init failed, the gateway
  served traffic with no audit trail and `/health` stayed green.
- **What didn't work:** *Retrying everything:* duplicate rows (`IntegrityError`) can never succeed,
  so retrying or replaying them would loop forever. Those are now counted as `rejected`
  and dropped.
- **Fix:** Retry briefly, then append to a fsynced JSONL spill file that is replayed on
  startup. A crashed replay is picked up next time. The metric is
  `gateway_audit_write_failures_total{table,outcome}`. Startup fails without a database unless
  `GATEWAY_DB_REQUIRED=false`.
- **What works now:** `tests/test_audit_durability.py` covers outage → spill → replay, duplicates
  not spilled, failed replay keeping its rows, PII events spilling with hashes only, and startup
  refusal.
- **Trade-offs:** If both the database and the disk are unavailable, rows are lost (`outcome="lost"`,
  logged at critical level).

---

## Capacity plan decisions (phases 1–5 in progress)

## D-007: Audit writes move off the request path

- **Status:** Accepted, 2026-10-07 (phase 3)
- **Problem:** The audit insert is awaited before every response and before `[DONE]` on streams.
  On SQLite those inserts fight over the write lock under load.
- **What didn't work:** *Keeping writes synchronous and just tuning SQLite:* it keeps database
  latency in time-to-first-token and doesn't remove lock contention between concurrent requests.
- **Fix (planned):** A bounded in-memory queue and one batching writer task, using D-006's retry
  and spill. Flushed on shutdown; when the queue is full, rows spill to disk.
  `GATEWAY_AUDIT_MODE=sync` keeps the old behavior.
- **Trade-offs:** A hard crash loses rows still in the queue, typically milliseconds' worth.

## D-008: Routing default is priority with overflow; least-loaded is opt-in

- **Status:** Accepted, 2026-10-07 (phase 2)
- **Problem:** All traffic goes to the first priority endpoint that has the model, until it fails.
  Other GPUs sit idle while it queues.
- **What didn't work:** *Making least-loaded the default:* rejected. Operators use priority on
  purpose (for example, to prefer the big GPU), and changing how an idle system routes would
  surprise them.
- **Fix (planned):** `resolution.strategy: priority` (default) moves down the list only when an
  endpoint is full. `least_loaded` is opt-in. Pins stay hard, and D-003 restrictions apply first.
- **Revisit when:** Adding model residency (`/api/ps`) for Ollama fleets.

## D-009: Admission control lives in the gateway

- **Status:** Accepted, 2026-10-07 (phase 2)
- **Problem:** No limit on chat requests in flight. Overflow queues invisibly inside Ollama or
  the httpx pool, then times out after minutes.
- **What didn't work:** *Relying on Ollama's queue (`OLLAMA_MAX_QUEUE`):* it's invisible to the
  gateway, can't overflow to another box, and fails slowly.
- **Fix (planned):** A per-endpoint `max_concurrent` limit, a FIFO wait of up to `max_queue_wait`
  (default 5s), then 503 + `Retry-After`. Streams hold their slot until done. When unset,
  endpoints are unlimited (today's behavior).
- **Trade-offs:** Limits are per endpoint, not per model. Per-key concurrency limits and priority
  classes are deferred.

## D-010: Single gateway process; Redis later behind one interface

- **Status:** Accepted, 2026-10-07 (phases 2–4 build the interface; Redis is a later phase)
- **Problem:** Rate limits, budgets and (soon) in-flight slots live in one process's memory.
  Running two processes or replicas would give each its own copy.
- **What didn't work:** *Adding Redis now:* rejected for this stage.
  - It adds a service every on-prem or air-gapped customer has to run, secure and back up.
  - It adds a new failure mode: does the gateway fail open or closed when Redis is down?
  - Sharing in-flight slots across replicas needs crash-safe leases, so it's a real phase of
    work, not a config flag.
  - The GPUs saturate long before one gateway process does.
- **Fix:** One process per deployment, active/passive for high availability. All shared state
  goes behind one interface with an in-memory implementation, so Redis later is one setting
  (`GATEWAY_REDIS_URL`) plus `docker compose --profile ha`, with no caller changes.
- **Revisit when:** Load tests show gateway CPU saturating before the GPUs, a customer requires
  active/active, or a Kubernetes deployment needs replicas > 1.

## D-011: Inline images pass through; image URLs are rejected

- **Status:** Proposed, 2026-10-07 (phase 5)
- **Problem:** The OpenAI route drops all image parts silently, so vision models answer as if no
  image was sent.
- **What didn't work:** *Downloading `http(s)` image URLs in the gateway:* rejected.
  - Any client could make the gateway fetch internal addresses (GPU boxes, admin pages,
    cloud metadata).
  - It would make arbitrary outbound requests, breaking air-gapped and egress-controlled sites.
  - The audit trail would hold only a URL whose content can change.
- **Fix (planned):** Pass inline `data:` images through. Reject image URLs with 400 and a message
  saying to send the image inline.
- **Revisit when:** A customer needs URL images. Then add opt-in fetching with a domain allowlist,
  private addresses blocked, and size and time caps.
