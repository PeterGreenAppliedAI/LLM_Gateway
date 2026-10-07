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

- **Status:** Accepted, 2026-10-07 (phase 5)
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

---

## Phase 1: Streaming correctness

## D-012: Stream failures carry their cause; failover follows the non-streaming rules

- **Status:** Implemented, 2026-10-07, `abfa1c5`
- **Problem:** Every adapter turned any stream failure into an anonymous error chunk. The
  dispatcher therefore couldn't tell "model not found" (404) from a dead box. It failed a
  404 over to every endpoint, then returned "all providers unavailable" with no reason.
  Streams it gave up on were never closed.
- **What didn't work:**
  - *Patching each adapter separately:* three copies of the line reader and two of the SSE
    parser had already drifted. vLLM ignored `finish_reason: "tool_calls"`, and neither
    OpenAI-style adapter parsed streamed tool calls. Replaced with shared helpers in
    `providers/streaming.py`.
  - *Treating every in-band runtime error as non-retryable:* an Ollama error line such as "model
    requires more system memory" is a server-side failure another box may handle. Code
    `upstream_error` is now retryable. Only upstream 4xx stops failover.
- **Fix:** Error chunks carry `error` and `error_code` (`http_<status>`, `timeout`,
  `connection_error`, `upstream_error`, `unknown_error`). `dispatch_stream` applies
  `_is_retryable_error` like non-streaming dispatch does: 4xx raises `ProviderError` with the
  upstream status, and a pin never fails over. The final 503 includes the last cause. Abandoned
  streams are closed, and closing the returned stream closes the upstream connection.
- **What works now:** `tests/test_streaming_phase1.py::TestStreamFailover`,
  `::TestAdapters`, `::TestUpstreamHttpError`.

## D-013: The status code is decided before the first byte

- **Status:** Implemented, 2026-10-07, `abfa1c5`
- **Problem:** Streaming routes returned `200 OK` headers, *then* dispatched. "No endpoint
  available" or "model not found" arrived as an error event inside a 200, which load balancers
  and SDK retry logic can't see.
- **What didn't work:** *Sending the error event with a non-200 status:* impossible, because the
  status line is already sent when the generator starts.
- **Fix:** `StreamRecorder.start()` picks the endpoint and waits for the first chunk *before*
  the `StreamingResponse` is created. Failures raise and the normal exception handlers return
  4xx/503 JSON (audited first). The wait can be long during a cold model load, so it runs
  under `run_unless_disconnected`: a client that leaves cancels the upstream request. After the
  first byte, failures are sent in-band: OpenAI `{"error": …}` frame, Ollama `{"error": …,
  "done": true}`.
- **What works now:** `TestPreStreamStatus` (503 and pass-through 404 on both APIs).
- **Trade-offs:** Response headers wait for the first token. That's the same time-to-first-
  token, only the headers move.

## D-014: Separate first-chunk and between-chunk stream timeouts

- **Status:** Implemented, 2026-10-07, `abfa1c5`
- **Problem:** The gap allowed between chunks was `max(120s, endpoint timeout)`. With timeouts
  allowed up to 3600s, a stalled stream could hold a GPU slot and a client for an hour.
- **What didn't work:** *One shorter timeout for every chunk:* cold model loads legitimately take
  minutes before the first token.
- **Fix:** The first chunk may take the endpoint `timeout`. After that, a new per-endpoint
  `stream_idle_timeout` (default 60s) applies. Stalls are classified as `timeout`, which is
  retryable.
- **What works now:** `TestTimeouts`.

## D-015: OpenAI route streams tool calls

- **Status:** Implemented, 2026-10-07, `abfa1c5`
- **Problem:** `tools` + `stream: true` was silently converted to one buffered response
  wrapped in fake SSE, so for agent workloads time-to-first-token equaled total latency.
- **What didn't work:** *Re-fragmenting arguments into OpenAI-style partial deltas:* unnecessary.
  The spec allows a complete call in one delta, and SDKs assemble deltas by `index` the same way.
- **Fix:** Each tool call is emitted as one delta (`index` running across the response,
  `id` = upstream id or `call_<n>`, arguments as a JSON string). The finish reason is
  `"tool_calls"` whenever tool calls were sent, since Ollama reports `"stop"`.
- **Also fixed:** the stream choice `index` counted chunks (0, 1, 2, …). It is now always 0,
  because there is one choice.
- **What works now:** `TestOpenAIToolStreaming`, `TestParseOpenAISSE`.

## D-016: Bugs found while doing phase 1

- **Status:** Implemented, 2026-10-07, `abfa1c5`
- **Stream endpoint order ignored routing config.** Streams tried endpoints from the model
  catalog first, and the resolved primary (which reflects pins, `target_endpoint`, per-model
  defaults and priority) only after them. They also ignored `fallback_allowed: false`.
  Found because a failover test hit the wrong endpoint first. *Fix:* primary first, then other
  endpoints with the model, then the fallback chain; fallback disabled means primary only.
  `test_stream_honors_priority_and_target_endpoint`.
- **Endpoint choice changed between restarts.** `ModelCatalog.get_endpoints_for_model` built its
  list from a `set`, so order depended on the per-process hash seed. *Fix:* order-preserving
  dedup. `test_catalog_order_is_deterministic`.
- **`connect_timeout` was ignored for endpoints.** The registry never passed it to the adapter,
  so it was always 3s. *Fix:* passed through, along with `stream_idle_timeout`.


---

## D-017: Batch-shaped requests were silently reduced to one

- **Status:** Implemented, 2026-10-07, `0ec8d7b`
- **Problem:** Found while reviewing how the gateway uses continuous batching:
  - **vLLM embeddings didn't exist.** The vLLM adapter had no `embeddings` method. The base
    class raised `NotImplementedError`, the dispatcher treated it as an endpoint failure, and
    clients saw "all providers unavailable", while the README listed vLLM embeddings as
    "Full".
  - **Prompt lists on `/v1/completions` used only `prompt[0]`.** A client sending 10 prompts got
    one answer and no error.
  - **`n` was ignored.** Unknown request fields are dropped without an error, so `n: 3` returned
    one choice.
- **What didn't work:**
  - *Passing `n` and prompt lists through to the engine:* vLLM supports both, Ollama supports
    neither, and the internal response carries one output. Making every adapter and the
    internal model multi-choice would touch the whole pipeline.
  - *Batching requests inside the gateway:* pointless with continuous-batching engines. They
    batch whatever is in flight, so the gateway's job is to send concurrent requests, not to
    batch them itself.
- **Fix:** Fan out in the route (`routes/fanout.py`). Each requested choice (prompts × n,
  prompt-major order like OpenAI) is its own upstream generation, dispatched concurrently and
  merged into one response with summed usage. This works the same on every engine.
  - Capped at 8 concurrent upstream requests per client request, and 64 choices per request.
  - `n > 1` with `stream: true` is rejected (422), because interleaving choices in one
    stream isn't worth the complexity yet.
  - All-or-nothing: one failed choice fails the request and cancels the rest.
  - One audit row and one budget charge per client request; one metrics sample per upstream
    generation, so per-endpoint metrics stay accurate.
  - vLLM embeddings added via `/v1/embeddings` (the whole list in one call). Embeddings from
    OpenAI-style upstreams are sorted by `index`, so output *i* always matches input *i*.
- **What works now:** `tests/test_batch_requests.py`.
- **Trade-offs:** With vLLM, `n` as separate requests forgoes vLLM's shared-prompt
  optimization for `n`. Usage reports the prompt once per choice, which matches the compute
  actually done. Prompt lists still count as one request against rate limits.
- **Revisit when:** Phase 2 lands. The fan-out should then take slots from the per-endpoint
  admission queue, and batch keys get a lower priority class. An offline `/v1/batches` API
  would sit on top of that.
- **Known issue found, not fixed here:** `/v1/completions` ignores `stream: true` and returns
  plain JSON, which breaks clients expecting server-sent events.

## D-018: Completions streaming, raw-prompt streams, and unscrubbed completion prompts

- **Status:** Implemented, 2026-10-07, `868947a` (scrubbing fix landed earlier in `0ec8d7b`)
- **Problem:**
  - `/v1/completions` ignored `stream: true` and returned plain JSON to clients expecting
    server-sent events.
  - **Every stream went through each engine's chat endpoint**, which applies the model's chat
    template. Non-streaming completions use the raw completion endpoint (`/v1/completions`,
    Ollama `/api/generate`). So base models and fill-in-the-middle prompts behaved differently
    streamed vs not, and Ollama `/api/generate` streams lost `system`/`template`/`context`
    handling.
  - **Found while fixing this:** before `0ec8d7b`, `/v1/completions` sent the **raw client prompt**
    to the model. The Unicode sanitizer and PII scrubber wrote into a `sanitized_prompt`
    variable, but the internal request was built from `body.prompt`. With PII scrubbing on,
    completion prompts reached the model unscrubbed.
- **What didn't work:** *Streaming completions by turning the prompt into a chat message:* the
  quick fix, and the wrong semantics. It would apply a chat template to a raw-text prompt.
- **Fix:**
  - Adapters gain `generate_stream`: vLLM and OpenAI stream `/v1/completions` (OpenAI falls back
    to chat on 404, like its `generate()`), Ollama streams `/api/generate`. The base class falls
    back to a single chunk from `generate()`. The dispatcher uses `generate_stream` for
    completion and generate tasks.
  - The route streams `text_completion` frames under the same contract as chat (D-013).
    Several prompts or `n > 1` with streaming is rejected (422).
  - The model receives the sanitized and scrubbed prompts, now pinned by a regression test.
  - vLLM completions now pass `stop` sequences, which they had silently dropped.
- **What works now:** `tests/test_completions_streaming.py`, including
  `test_scrubbed_prompt_is_what_the_model_gets` for both streaming and non-streaming.

## D-019: PII scrubbing is configurable from the dashboard

- **Status:** Implemented, 2026-10-07
- **Problem:** Scrubbing was set only by `GATEWAY_PII_SCRUB_ENABLED` / `GATEWAY_PII_SCRUB_ROUTES`,
  so changing it meant editing the environment and restarting the gateway.
- **What didn't work / rejected:**
  - *Making detection togglable from the dashboard as well:* rejected. Turning detection off also
    stops PII being redacted from stored data (D-005), so a single click could start storing
    raw PII. Detection stays an environment setting; the dashboard shows it read-only.
  - *Keeping the change in memory only:* rejected. A restart would silently undo an operator's
    compliance decision.
  - *Exposing the API's "empty route list = all routes" directly as checkboxes:* unchecking
    everything would mean "scrub everything". The UI has an explicit *All routes / Selected
    routes* choice instead, and Save is disabled with nothing selected.
  - *First UI pass:* the main checkbox label rendered offset to the right, because the Vite
    template's `#root { text-align: center }` centered the shorter first line. Caught in a
    browser screenshot; fixed with `text-left`.
- **Fix:**
  - Admin-only `GET/PUT /api/pii/config` (D-002). Changes apply to the next request.
  - Saved in a new `runtime_settings` table (key, value, updated_at, updated_by; Alembic
    `b4e2d9a71c35`) and loaded at startup over the environment defaults. An unreadable saved
    value falls back to the environment default rather than failing startup.
  - Route names are validated against the routes that run PII detection, so a typo can't leave
    a route unscrubbed. Changes are refused (422) when detection is off.
  - Each change is logged at WARNING with before/after values and who made it.
  - The dashboard card shows the policy in effect, where it came from (environment or
    dashboard, by whom and when), and asks for confirmation before turning scrubbing off.
    It warns when there's no database to save to.
- **What works now:** `tests/test_pii_config.py`, covering admin-only access, live effect,
  selected routes, validation, the detection requirement, persistence across restart, and a
  bad saved value. Also verified end to end against a running gateway and dashboard in headless
  Chromium: save, cancel on the confirm dialog, confirm, and the setting surviving a restart.
  Migration checked upgrade → downgrade → upgrade on SQLite.
- **Trade-offs:** Only the current value is stored, not a history of changes. The change history
  lives in the WARNING log lines.
- **Revisit when:** Compliance needs a queryable change history. Then add a
  `runtime_settings_history` table. The same store can hold other runtime settings
  (budgets, D-010).
