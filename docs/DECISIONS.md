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
- **Update:** the default is now `enabled: false`, restricted to `allowed_networks` (D-042).

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

- **Status:** Accepted, 2026-10-07. **Amended by D-035:** the interface and an optional Redis backend now exist. In-memory stays the default.
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

- **Status:** Implemented, 2026-10-07, `df17d9b` (planned for phase 5; brought forward by the
  external review, see D-041)
- **Problem:** The OpenAI route drops all image parts silently, so vision models answer as if no
  image was sent.
- **What didn't work:** *Downloading `http(s)` image URLs in the gateway:* rejected.
  - Any client could make the gateway fetch internal addresses (GPU boxes, admin pages,
    cloud metadata).
  - It would make arbitrary outbound requests, breaking air-gapped and egress-controlled sites.
  - The audit trail would hold only a URL whose content can change.
- **Fix:**
  - Text parts are sanitized in place; inline `data:` images pass through (Ollama gets them in
    its native `images` field).
  - Image URLs and unsupported part types are refused (validation error) with a message
    saying to send the image inline.
  - Stored audit bodies replace image bytes with a short description (type and size), in
    line with D-023. Media is redacted before the body is stored.
- **What works now:** `tests/test_wire.py` checks the engine receives the image.
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

---

## Media: voice, images and video (research: [MEDIA_ENGINES.md](MEDIA_ENGINES.md))

## D-020: Contract first; engines are described by data, not code

- **Status:** Accepted, 2026-10-07
- **Problem:** Adding text-to-speech (TTS), speech-to-text (STT), image and video generation. The
  first draft gave every engine its own adapter type (`kokoro`, `speaches`, `vllm`,
  `whispercpp`, `comfyui`, …), which ties the gateway to today's engines and needs a release for
  each new one.
- **What didn't work:** *An adapter per engine:* rejected on review (the user asked "does it have
  to be specifically those?"). Most engines already speak one contract.
- **Fix:**
  - **Clients always see OpenAI's API:** `/v1/audio/speech`, `/v1/audio/transcriptions`,
    `/v1/audio/translations`, `/v1/images/generations` and `/v1/videos`.
  - **An endpoint's `type` is its request style, not its brand.** `openai` covers Kokoro-FastAPI,
    speaches, vLLM, vLLM-Omni, LocalAI, Chatterbox, stable-diffusion.cpp, and Ollama ≤0.32.5
    images. `comfyui` covers every ComfyUI model, because a new model is a new workflow
    template (data). Translators (whisper.cpp, Piper) are built only when someone runs them.
  - **`capabilities`** (`tts`, `stt`, `image`, `video`) say what an endpoint does.
  - **What the dashboard shows (voices, languages, ranges) comes from data, in order:**
    1. Ask the engine (`/v1/models`, `/v1/audio/voices`, ComfyUI `/object_info`).
    2. **Profiles:** small config files describing an engine family's quirks (e.g. Kokoro: the
       first letter of a voice ID is its language, speed 0.25–4.0).
    3. Admin-declared values for engines that report nothing.
- **Trade-offs:** An engine without a profile gets generic controls until someone writes one.
- **Revisit when:** An important engine needs behavior a profile can't describe.

## D-021: Media usage is metered in its own units, converted to token budgets

- **Status:** Accepted, 2026-10-07 (stated default, not objected to)
- **Decision:** Record each request's native units: TTS characters, STT audio seconds, image
  count × megapixels × steps, video seconds × resolution. Budgets convert these to weighted
  tokens with a per-tier multiplier, so one budget system covers everything.
- **Why:** It reuses the existing budgets and dashboard instead of a parallel system.
- **Revisit when:** Operators need separate media quotas (e.g. "10 videos/day").

## D-022: Unsupported audio formats are rejected, not transcoded

- **Status:** Accepted, 2026-10-07 (stated default, not objected to)
- **Decision:** If an engine can't produce or accept a format (several are WAV-only), return
  400 listing the formats it supports. No ffmpeg in the gateway in v1.
- **Why:** Transcoding adds a native dependency, CPU load on the request path, and a
  media-parsing attack surface. It also hides capability gaps that should be visible.
- **Revisit when:** A must-have client can't choose its format. Video thumbnails (D-024) may
  need ffmpeg anyway; if it arrives for that, reconsider.

## D-023: Generated and uploaded media are never stored in the audit trail

- **Status:** Accepted, 2026-10-07 (stated default, not objected to)
- **Decision:** Audit rows record metadata only: format, duration, size, dimensions, voice,
  model and a content hash. TTS input text and image/video prompts go through PII
  detection like chat prompts; transcripts are redacted before storage (D-005).
- **Why:** Audio and images can carry PII (voices, faces) that hash-only rules can't redact.

## D-024: Generated video (and image links) live in a gateway media store

- **Status:** Accepted, 2026-10-07 (stated default, not objected to)
- **Problem:** Video takes minutes and produces MB-sized files, so it can't return inline.
  OpenAI's video API is job-based (create → poll → download content).
- **Decision:**
  - The gateway copies outputs out of the engine (e.g. ComfyUI's output folder) into its own
    store, keyed by job ID, with owner and `expires_at` (default 7 days, configurable).
  - Downloads only through the gateway, by the creating key or an admin. Video supports Range
    requests for playback.
  - Never expose engine routes such as ComfyUI `/view` or `/upload`.
  - Images stay synchronous base64 (OpenAI has no image job API). The gateway holds the request
    open with keepalives while ComfyUI works, and stores an image only if a client asks for a link.
- **Revisit when:** Storage needs to be S3 or other object storage instead of local disk.

## D-025: Build order: voice, then capacity (phase 2), then images and video

- **Status:** Accepted, 2026-10-07 (stated default, not objected to)
- **Why:** Voice is mostly OpenAI-compatible pass-through and useful immediately. Images and
  video need per-endpoint concurrency limits first, or requests queue invisibly for minutes
  behind a busy GPU.

## D-026: Ollama image generation is supported only as a generic OpenAI endpoint

- **Status:** Accepted, 2026-10-07
- **Finding:** Ollama shipped experimental image generation (macOS / Apple Silicon, MLX runner,
  Z-Image Turbo and FLUX.2 Klein) in v0.14.0 and **removed it in v0.32.6** (2026-08-04).
  Current Ollama returns 400 for image models on every platform. An earlier research pass read
  only current code and concluded it had never existed, until the user's Mac proved otherwise.
- **Decision:** No Ollama-specific image adapter. A Mac pinned to ≤0.32.5 works as a `type: openai`
  image endpoint (D-020).
- **Lesson:** Check release history, not just current main, before declaring a feature absent.

## D-027: Voice routes (M1a)

- **Status:** Implemented, 2026-10-07
- **What:** `POST /v1/audio/speech`, `/v1/audio/transcriptions` and `/v1/audio/translations`
  over any OpenAI-compatible engine with `capabilities: [tts]` / `[stt]` (D-020). Media gets
  its own small dispatcher, because binary bodies don't fit the text request model. It follows
  the same rules as text:
  - capability, then key/environment scope (D-003), then pins and `target_endpoint`, then
    healthy and priority order;
  - the status is known before the first byte (D-013);
  - upstream 4xx passes through; 5xx, 429 and connection failures fail over (D-012).

  Speech audio is relayed as it arrives. TTS input text goes through PII detection and
  scrubbing (`/v1/audio/speech` is in the scrub route list). Audit rows carry `media_usage`
  metadata only (D-023, migration `c7d1e5f20a84`). Budgets charge characters × 0.25, or audio
  seconds × 10 (both configurable, D-021). Disconnects are recorded and charged, shielded as
  in D-004.
- **What didn't work:**
  - *httpx's own multipart encoder for the upload:* FastAPI gives the route a synchronous
    spooled file, httpx turns it into a sync stream, and the `AsyncClient` refuses to send it
    ("Attempted to send a sync request with an AsyncClient"). **Every real transcription would
    have crashed.** Caught by the first test run. Fixed by building the multipart body as an
    async stream that reads the spool in chunks, with an exact `Content-Length`. A test now
    parses it on a real FastAPI engine (repeated fields, file bytes, a malicious filename).
  - *Reusing the OpenAI adapter's HTTP client:* its default `Content-Type: application/json`
    would override the multipart header. Media calls get a separate client with auth only.
  - *Expecting 503 when no endpoint serves a capability:* the gateway's convention is
    `400 no_provider` ("nothing can serve this request"). Kept it consistent.
- **Also found:** `python-multipart` was installed but never declared in `pyproject.toml` or
  `requirements.txt`. FastAPI needs it for any form route, so a clean install would have
  failed. Now declared.
- **Metering limits:** audio duration comes from the engine (`verbose_json`) or the WAV header.
  For other formats it's unknown, and the budget falls back to the transcript's approximate
  token count (`duration_source` records which was used).
- **Not yet (M1b):** voice/model discovery, engine profiles, validating voice and speed against
  the registry, `GET /v1/audio/voices`. Today an unknown voice is rejected by the engine (400
  passed through) or silently replaced, depending on the engine.
- **What works now:** `tests/test_audio_routes.py` (18 tests).

## D-028: Voice registry: discovery, profiles, voice-aware routing (M1b)

- **Status:** Implemented, 2026-10-07
- **Problem:** After M1a, an unknown voice was rejected by some engines and silently swapped by
  others, and nothing could tell the dashboard what voices or ranges exist.
- **Fix:**
  - **Profiles** are YAML in `config/profiles/` (shipped: `kokoro`, `speaches`, `vllm-omni`,
    from source-verified research). An endpoint opts in with `profile:`. A profile describes:
    - where the voice list lives;
    - how to read language and gender from voice IDs (Kokoro's `af_` → American English,
      female);
    - whether blends are allowed;
    - per-setting types and ranges;
    - Whisper language codes for dropdowns.

    Profiles are validated at startup. A broken profile, or an endpoint naming a missing one,
    fails startup.
  - **Media catalog** polls every media endpoint (60 s) for `/v1/models` and its voice list.
    It normalizes every voice-list shape seen in research (Kokoro objects or strings,
    speaches `{name, language, gender}`, vLLM-Omni `uploaded_voices`, Piper `{id: config}`)
    and falls back to admin-declared `voices:`.
  - **Validation is routing.** Each candidate endpoint is checked: unknown voice (each
    component of a blend), or a setting outside the profile's range. Requests go only to
    endpoints that can serve them, so a voice on box B routes to B. When none can, the
    response is 422 with the reason and the available voices.
  - **Listings:** `GET /v1/audio/voices` returns the union across the endpoints the caller's
    key may use, with language, gender and serving endpoints. Admin `GET /api/media/catalog`
    and `POST .../refresh` return everything the dashboard needs.
- **What didn't work / rejected:**
  - *Rejecting requests to engines we know nothing about:* an engine with no voice list and no
    profile is never blocked, because unknown is not invalid.
  - *Using each model's own speed range:* where limits differ by model inside one engine
    (speaches: Kokoro 0.5–2.0, Piper 0.25–4.0), the profile uses the widest range so the
    gateway never rejects what the engine would accept.
  - *Validating STT language codes:* engines support languages beyond any list we ship. The
    list feeds the dashboard only.
- **What works now:** `tests/test_media_catalog.py` (19 tests: shapes, profile enrichment,
  declared voices, routing by voice, blend validation, ranges, unknown engines, scoped
  listing, admin catalog, startup failure).
- **Trade-offs:** The voice list can be up to 60 s stale. A just-added voice may be rejected
  until the next poll; `POST /api/media/catalog/refresh` forces one.

## D-029: Dashboard voice controls (M2)

- **Status:** Implemented, 2026-10-07
- **What:** A **Voice** tab built entirely from the media catalog (D-028):
  - **Engines view:** health, capabilities, profile, voice and model counts, last refresh,
    discovery errors, and a refresh button.
  - **Text-to-speech:** endpoint ("Auto" lets the gateway route; picking one pins via
    `endpoint/model`), model, language and gender filters, voice picker, Kokoro **blender**
    (voices plus weight sliders with a preview of the string sent), speed slider within the
    profile's range, browser-playable formats only, then play and download.
  - **Speech-to-text:** model, transcribe or translate, language (or auto-detect) from the
    profile, output format, and upload or **in-browser recording** (MediaRecorder; needs HTTPS
    or localhost).
- **Decision:** the playground calls the real public routes, so its use is audited, metered and
  budgeted like any client. For that, **the admin key now works on inference routes too**,
  audited as client `admin`. This completes D-002's "admin key is a superset".
- **What didn't work:**
  - *The first browser run:* transcription failed with `Endpoint 'Systran' does not serve stt`.
    That exposed D-030, a bug well beyond the playground.
- **What works now:** verified end to end against the real gateway and dashboard with fake
  Kokoro- and speaches-shaped engines in headless Chromium:
  - the British English filter, then `bm_george` at speed 1.25 → a 1.2 s playable WAV;
  - the blend `bm_george(2)+bf_emma` → reaches the engine verbatim;
  - transcription of a WAV upload with `Systran/faster-whisper-small`;
  - audit rows recorded as client `admin` with `media_usage`, including duration from the WAV
    header.

  Test: `test_admin_key_works_on_inference_routes`.

## D-030: Model names containing "/" were misread as endpoint pins

- **Status:** Implemented, 2026-10-07
- **Problem:** `endpoint/model` pins were parsed purely by syntax, so any model name with a slash
  became a pin to a nonexistent endpoint: Hugging Face IDs on vLLM and speaches
  (`meta-llama/Llama-3.1-8B-Instruct`, `Qwen/Qwen2.5-7B`, `Systran/faster-whisper-small`) and
  Ollama namespaced models (`user/model`). **Chat requests to such models failed** with
  "provider unavailable", and resolution raised `EndpointNotFoundError`. Found by the M2
  browser run, not by any test: the existing transcription test used an explicit pin.
- **What didn't work:** *The old unit tests:* they asserted the syntactic behavior
  (`"nonexistent/phi4" → EndpointNotFoundError`), so they encoded the bug. Two route-debug
  tests also passed only by accident. They patch `get_dispatcher`, but FastAPI captured the
  real dependency at import, so the patch never applied and the real dispatcher ran on a
  bare mock registry.
- **Fix:** a prefix is a pin **only if it names a configured endpoint**
  (`Dispatcher.split_pin`, registry-aware). Otherwise the whole string is the model name. Fixed
  at every call site (text dispatch, streaming, media, environment model approval, stream
  budget metering).
- **Trade-off:** If an endpoint is named the same as a Hugging Face org (e.g. `Qwen`), its
  prefix is treated as a pin. Don't name endpoints after model orgs.
- **What works now:** `test_slash_in_model_name_is_not_a_pin`,
  `test_step1_unknown_prefix_is_part_of_model_name`, and the M2 end-to-end transcription.

## D-031: Circuit breaker replaces in-request health probes (phase 2a)

- **Status:** Implemented, 2026-10-07
- **Problem:** When an endpoint was marked unhealthy, every request routed to it ran a blocking
  health check (up to 10 s) *inside the request*. Ten queued requests meant ten probes against
  a dead box. Each one added latency before failover, and they all piled onto an endpoint that
  was already struggling. **Streaming didn't check health at all:** a dead endpoint got the full
  connect timeout on every stream. Request failures weren't remembered either, so the next
  request walked into the same failure.
- **Decision:** a per-endpoint circuit breaker (`dispatch/circuit.py`), fed by **both** request
  outcomes and the background health loop:
  - **closed:** requests flow. Consecutive *retryable* failures count up. Retryable means
    connection errors, timeouts, 5xx, and in-band upstream errors.
  - **open:** reached at `failure_threshold` (default 5). Requests skip the endpoint with no
    network call ("circuit open" appears in the failover reasons).
  - **half-open:** after `cooldown_seconds` (default 15), exactly one probe request is let
    through. Success closes the circuit; failure reopens it for another full cooldown.
  - **Health loop:** a healthy check closes the circuit; an unhealthy check, timeout or
    exception opens it. Recovery is noticed even with no traffic.
  - **Wired into:** `Dispatcher._try_provider`, `dispatch_stream` (judged on the first chunk),
    and `MediaDispatcher.send`.
- **What counts as what:**
  - Upstream **4xx counts as a success.** The engine is alive; the request was wrong. Counting
    it would let one client's bad requests open the circuit for everyone.
  - A **media 429** releases the probe without a verdict. The engine is busy, not broken.
  - A **cancelled request** (the client left mid-probe) also releases the probe.
  - Any probe that never reports back expires after one more cooldown, so a leak can't strand
    an endpoint half-open.
- **Alternatives considered:**
  - *Keep the in-request probe but cache its result:* still blocks the first request after
    each cache expiry, and still knows nothing about request failures.
  - *Error-rate window (e.g. 50 % over 30 s):* better at catching flapping endpoints, but on
    low-traffic home labs a handful of requests swings the rate wildly. Consecutive-failure
    counting is predictable at any traffic level. This can be revisited if flapping shows up.
- **State is in-process** (one breaker per gateway worker). Under the D-010 shared-state
  interface it can move to Redis later. Per-worker state is acceptable for now: each worker
  learns within `failure_threshold` requests.
- **Visibility:**
  - `circuit` appears per provider on `/health` and per endpoint on `/v1/devmesh/catalog`.
  - The dashboard endpoint card shows a "Circuit open" or "Probing" badge.
  - Prometheus exposes `*_circuit_state{endpoint}` (0 closed, 1 half-open, 2 open).
- **Config:** `circuit_breaker.failure_threshold`, `circuit_breaker.cooldown_seconds`.
- **What didn't work:**
  - *First wiring:* the `CircuitBreakerConfig` model sat in `dispatch/circuit.py`, which made
    a circular import (config → dispatch → config). Moved the model into `config.py`.
  - *A search-and-replace that routed health-loop trips through the new `registry.trip()`:*
    it also rewrote `trip()`'s own body into a call to itself (infinite recursion). The tests
    caught it immediately.
  - *Three existing tests went red,* as expected. They expressed "unhealthy" only through
    `ProviderHealth`, so they now hit the network. Updated them to trip the breaker, which is
    now the thing that gates requests.
- **What works now:** `tests/test_circuit.py` (17 tests):
  - the state machine, including the single half-open probe and probe expiry;
  - failover skips an open endpoint without calling it;
  - 4xx doesn't trip;
  - a cancelled probe is released;
  - streams trip the breaker;
  - the health loop opens and closes the circuit;
  - `/health` and `/metrics` report the state.

  Also `test_open_circuit_skips_engine` (voice).


## D-032: Admission control and overflow routing (phase 2b/2c)

- **Status:** Implemented, 2026-10-07
- **Problem:** The gateway didn't know how busy an endpoint was. Chat traffic was limited by
  request *rate*, not by requests *in flight*. Ten long generations sent to an Ollama box with
  `OLLAMA_NUM_PARALLEL=4` meant six queued invisibly inside Ollama. Then they timed out or
  blew TTFT, while a second GPU with the same model sat idle. Every request went to the first
  endpoint by priority until that endpoint *failed*.
- **Decision:**
  - **Per-endpoint `max_concurrent`** on `endpoints[]` (unset = unlimited, but still counted).
    It is a gateway-side mirror of the engine's own parallel slots (`OLLAMA_NUM_PARALLEL`,
    vLLM `--max-num-seqs`).
  - **Priority with overflow (default):** candidates keep their resolved order: pin,
    target_endpoint, model default, priority, then the fallback chain. A request takes the
    first one with a free slot, so a full primary overflows to the next box *before* anything
    fails. Pins and `fallback_allowed: false` never overflow.
  - **`resolution.strategy: least_loaded` (opt-in):** candidates are ordered by in-flight ÷
    `max_concurrent`, with priority as the tiebreaker. Use it for equal boxes you want evenly
    used. Priority stays the default: on a home lab the "primary" is usually the better GPU.
  - **Bounded FIFO wait:** when every candidate is full, the request waits for whichever slot
    frees first, up to `admission.max_queue_wait_seconds` (default 5 s). It then gets
    **503 `capacity_exceeded`** with `Retry-After` (≈ the wait, 1–30 s). A freed slot passes
    *directly* to the oldest waiter, so a newcomer can't jump the queue.
  - **The slot lasts as long as the work:**
    - non-streaming: until the response returns;
    - streams: until the stream closes (in `_chain`'s `finally`);
    - voice: until the audio has been relayed (`UpstreamMedia.aclose()`).

    Failover releases the failed endpoint's slot before taking the next. Fan-out (`n`>1,
    prompt lists) goes through `dispatch()`, so each choice takes its own slot.
  - **Interface, not implementation:** `ConcurrencyBackend` has an `InMemoryConcurrency`
    implementation. Counts are per process. With several workers, either divide
    `max_concurrent` by the worker count or wait for the Redis backend (D-010). The
    interface is the seam for it.
- **Why 503, not 429:** 429 tells the client *it* sent too much (that's the per-key rate
  limiter). Here the gateway's capacity is full regardless of who asked. 503 + Retry-After is
  what OpenAI SDKs and proxies already retry.
- **Alternatives considered:**
  - *Round-robin:* ignores how long requests run. One 2k-token generation and one short
    reply count the same.
  - *Rely on the engine's own queue:* that's the status quo; the gateway can't overflow what
    it can't see.
  - *Unbounded gateway queue:* moves the timeout somewhere else and hides overload from
    clients.
  - *Weighting by GPU speed:* nothing to measure it from yet. `least_loaded` with honest
    `max_concurrent` values gets most of the benefit.
- **Safety nets:**
  - A lease dropped without being closed (a stream the client abandoned before its body
    started) is released when it's garbage-collected. This is logged at debug.
  - A cancelled waiter passes on a slot handed to it in the same instant.
  - Releases are idempotent.
- **Visibility:**
  - The catalog reports `in_flight` and `max_concurrent`; the dashboard endpoint card shows
    "2/4 busy".
  - Prometheus: `*_endpoint_in_flight`, `*_endpoint_max_concurrent`,
    `*_admission_queue_depth`, `*_admission_wait_seconds` (only requests that waited) and
    `*_admission_rejected_total`.
- **What didn't work:**
  - *Legacy `providers:` configs* get converted into `endpoints` by a config validator that
    copies fields one by one. The new `max_concurrent` was silently dropped, and the first
    overflow tests went to the primary as if it were unlimited. Added the field to the
    conversion.
  - *Two tests mutated `config.providers` after construction:* that has no effect, because
    the endpoints were already derived. Rewrote them to build the config with the capacity.
  - *`except TimeoutError` around `asyncio.wait_for`:* only correct on Python 3.11+. The
    project supports 3.10, where it raises `asyncio.TimeoutError`, so it now catches that
    alias.
- **What works now:** `tests/test_admission.py` (20 tests):
  - the backend: FIFO order, no queue-jumping, wait-for-any, timeout, cancellation, GC
    release;
  - overflow and the primary-first path;
  - waiting then proceeding, and no overflow when fallback is disabled (503);
  - a slot held for the request's duration, and released on failover;
  - `least_loaded`;
  - streams holding and releasing the slot;
  - 20 concurrent requests never exceeding the cap on either endpoint;
  - 503 + Retry-After over HTTP.

  Also `test_full_engine_overflows_and_slot_is_released` (voice).

## D-033: Connection pool sizing and per-key burst scaling (phase 2d/2e)

- **Status:** Implemented, 2026-10-07
- **Problem 1, the pool:** every endpoint's httpx client used the default pool (100
  connections, 20 kept alive). Its pool wait inherited the read timeout, which can be up to an
  hour. Two consequences:
  - Past 100 concurrent requests, requests queued silently *inside httpx*, invisible to
    admission control and metrics.
  - With only 20 keep-alive connections, a burst of 40 paid TCP setup for half of them on
    every wave.
- **Fix 1:**
  - With `max_concurrent` set, the pool is `max_concurrent + 4`, all kept alive. Admission
    (D-032) already keeps requests at or below `max_concurrent`; the +4 covers health checks
    and model discovery.
  - Unlimited endpoints keep httpx's 100/20, so nothing changes without opting in.
  - **Pool wait is 5 s.**
  - A `PoolTimeout` becomes error code `pool_timeout`, distinct from `timeout`. It is
    retryable (another endpoint has its own pool) but gives the circuit breaker no verdict:
    a full gateway pool says nothing about the engine. Counting it would let the gateway's own
    saturation open circuits on healthy GPUs.
  - **Why no `max_connections` knob:** a second number that must agree with `max_concurrent`
    is a misconfiguration waiting to happen. Derive it, and add a knob only if someone needs a
    different value.
- **Problem 2, the burst limit:** `burst_limit` (10 per 10 s) and `requests_per_hour` (1000)
  were global and ignored a key's `rate_limit_rpm`. A key granted 600 RPM was refused after its
  11th request in 10 s, so it got 60 RPM in practice, and it was cut off after 1000 per hour.
- **Fix 2:** a per-key RPM override scales burst and hourly limits by the same factor
  (`RateLimiter.limits_for`):
  - 600 RPM → 100 per 10 s and 10 000 per hour;
  - 3 RPM → burst 1;
  - keys without an override are unchanged.
- **Alternatives considered:** per-key `burst` and `rph` overrides on the key. They give
  more knobs, but every key with a raised RPM would also need two more numbers set correctly.
  Proportional scaling makes the common case right with no extra config. Explicit overrides
  can be added later if a key needs a different shape.
- **Still open:** keyless clients share one `default` bucket. Bucketing them per client IP
  needs trusted proxy headers (behind a reverse proxy, every client has the proxy's IP), so
  it's deferred to the request-handling phase rather than shipped wrong.
- **What didn't work:** nothing broke this round.
- **What works now:** `tests/test_capacity_limits.py` (14 tests):
  - limits derived from `max_concurrent` and checked on the live httpx pool for the Ollama,
    vLLM and OpenAI adapters;
  - the 5 s pool wait;
  - `pool_timeout` classification;
  - 10 pool timeouts fail over without opening the circuit;
  - burst and hourly scaling, including the 600 RPM regression;
  - `check()` reporting the scaled limits.

## D-034: Per-key concurrency limits and the batch priority class

- **Status:** Implemented, 2026-10-07
- **Problem:** Endpoint admission (D-032) protects the GPUs, but not one client from another.
  An eval script firing 50 parallel requests filled every slot. Interactive users then queued
  behind it or got 503s, and nothing marked the script's work as able to wait. Per-key RPM
  doesn't help: 50 requests in one second is within 600 RPM.
- **Decision, part 1: per-key `max_concurrent`** (config keys, DB keys and keyless traffic):
  - **More than that many requests in flight → 429 `concurrency_limit_exceeded`**, with
    `Retry-After: 1`. It is refused immediately, not queued: the client is over *its own*
    allowance, so 429 is right, and it's what OpenAI does for concurrency.
  - **Counted per request, not per upstream call.** A fan-out (`n`=4) is one request for the
    key and takes four endpoint slots. The fan-out's own cap (8) bounds how much of an
    endpoint one request can occupy.
  - **Held for the whole response.** The slot lives in a FastAPI yield dependency
    (`get_inference_auth`). Since FastAPI 0.118 (the project requires 0.124+), its teardown
    runs *after* the response body is sent, so a stream holds its slot until it ends or the
    client leaves. This was checked empirically before relying on it.
  - **Only inference routes take a slot.** Listing routes (`/v1/audio/voices`,
    `/api/tags`) don't.
- **Decision, part 2: priority class `interactive` | `batch`:**
  - **Set on the key** (`priority: batch`). A client can also downgrade a single request with
    `X-DevMesh-Priority: batch`. The header can only lower priority: a batch key can't claim
    interactive. Unknown values → 422.
  - **Batch is served after interactive.** A freed endpoint slot goes to the oldest waiting
    interactive request first, then the oldest batch request.
  - **Batch has a capacity reserve against it:** `admission.batch_max_share`, default 0.75.
    Batch may hold at most that share of an endpoint's slots (at least 1). Ordering alone
    isn't enough: a batch job already holding *every* slot with long generations can't be
    preempted, so interactive requests would still wait minutes. The reserve keeps
    `1 − share` of each endpoint for interactive traffic.
  - **Batch waits longer for a slot:** `admission.batch_max_queue_wait_seconds`, default
    60 s vs 5 s. Batch callers want completion, not low latency, so a 503 after 5 s just
    makes them retry.
- **Alternatives considered:**
  - *Separate endpoint pools for batch:* wastes GPUs when there's no batch traffic.
  - *Strict priority without a reserve:* the starvation case above.
  - *Weighted fair queuing per key:* fairer among many tenants, but more machinery than a
    home-lab or small-team gateway needs. Revisit with the shared-state backend.
- **Per process**, like the rate limiter (the D-010 shared backend covers both).
- **Dashboard:** the Create Key form has *Requests / minute*, *Max concurrent* and an
  *Interactive / Batch* toggle with a one-line explanation. The keys table has a *Limits*
  column (e.g. "600 rpm · 1 concurrent · batch"). DB migration `d8e2f3a91b57` adds
  `api_keys.max_concurrent` and `api_keys.priority`.
- **Metrics:** `*_admission_wait_seconds` and `*_admission_rejected_total` now carry a
  `priority` label.
- **What didn't work:**
  - *My first stream-holding test* used httpx's ASGI transport, which buffers the whole
    response, so it deadlocked waiting for a body the test was holding open. The test now
    drives the ASGI app directly.
  - *A batch-share test asserted the wrong threshold.* "At most 2 of 4" means batch may
    start only while *fewer than 2* are in flight. The code was right; the test's arithmetic
    wasn't.
  - *The new key form inherited Vite's centered `#root` text*, the same issue as the PII
    card. Fixed with `text-left` on the form and the endpoint card.
- **What works now:**
  - `tests/test_key_limits.py` (15 tests):
    - the batch share;
    - interactive served before older batch waiters;
    - no handoff into the reserve;
    - batch overflows past a full share and waits longer;
    - 429 then recovery;
    - a stream holding its slot to the end;
    - keys counted separately;
    - header downgrade-only and validation;
    - priority reaching dispatch;
    - DB keys storing both fields.
  - Alembic upgrade and downgrade on a fresh SQLite DB.
  - **End-to-end run** (real gateway + SQLite + dashboard + a 1.5 s fake engine with
    `max_concurrent: 2`):
    - a key created in the dashboard with 600 rpm / 1 concurrent / batch shows those limits
      in the table;
    - two parallel requests on it → 200 + 429;
    - five parallel admin requests → 2 × 200, 3 × 503 `capacity_exceeded` with Retry-After;
      the engine saw a peak concurrency of exactly 2, and the endpoint card read "2/2 busy"
      mid-burst;
    - a batch request behind two interactive ones waited 3.0 s and succeeded (an
      interactive request would have hit 503 at 0.5 s);
    - a bad priority header → 422.

## D-035: Shared state: in-memory by default, Redis as an opt-in route

- **Status:** Implemented, 2026-10-07. Amends D-010.
- **Decision (Peter):** Redis is never required. Nobody should have to stand up another
  service to use the gateway. But anyone who runs several gateway processes or replicas should
  have a route to shared limits.
- **What's shared:**
  - rate-limit windows (burst, minute, hour per key);
  - endpoint concurrency slots (D-032);
  - per-key concurrency slots (D-034).
- **What stays per process:**
  - circuit breakers (D-031): each process learns within a few requests, and the health loop
    runs per process anyway;
  - token budgets: these belong in the database, not Redis (budget persistence phase).
- **How it's configured:**
  - **Unset `GATEWAY_REDIS_URL` (default):** in-memory, exactly as before. No new dependency:
    `redis` is an optional extra (`pip install 'devmesh-gateway[redis]'`). The Docker image
    includes it so `--profile ha` works without a rebuild.
  - **Set it:** every process using that Redis (and the same `GATEWAY_REDIS_PREFIX`) shares
    the counts, so `max_concurrent: 4` means 4 across all of them.
  - **Compose:** `GATEWAY_REDIS_URL=redis://redis:6379/0 docker compose --profile ha up -d`.
    Redis runs with no persistence: it only holds short-lived counts and leases.
- **One interface, so callers don't care** (`gateway/state/`):
  - `ConcurrencyBackend` and `RateWindowStore`, each with in-memory and Redis
    implementations;
  - `create_shared_state()` picks one at startup.
  - **This required making the callers async now:** the rate limiter, `PolicyEnforcer.enforce`,
    slot acquire and release, and least-loaded ordering. A Redis call is network I/O; a sync
    interface would block the event loop or force a second rewrite later. About 100 call
    sites changed, mostly tests, with no behavior change on the in-memory path.
- **How the Redis backend works:**
  - **Atomic Lua scripts.** Two processes can't both take the last slot or the last request
    in a window. Times come from the Redis server's clock (`TIME`), so clock skew between
    gateway hosts doesn't matter.
  - **Crash-safe slots.** A slot is a sorted-set entry with an expiry (lease TTL 30 s). A
    heartbeat renews the leases of live requests every 10 s, so long streams keep their slot.
    If a gateway crashes, its slots free themselves within 30 s instead of being held forever.
  - **Waiting across processes.** A release publishes on a channel, and waiters in every
    process retry immediately. Polling every 0.5 s is the backstop for a missed message.
    Order is exact within a process (interactive first, then arrival order); across
    processes, the first to retry after a release wins. The batch share (D-034) still
    applies across all processes.
  - **Release finishes even during cancellation.** Releases often run while the client is
    disconnecting. The Redis call runs as a shielded task, so it completes instead of
    leaving the slot held until the TTL.
- **If Redis is unreachable:** each process falls back to its own in-memory counts, so limits
  stay enforced, per process, as if Redis weren't configured.
  - One error is logged, and `/health` shows `shared_state: {backend: redis, status:
    degraded}`.
  - Redis is skipped for a 5 s cooldown, then retried. Socket timeouts are 0.5 s. So a dead
    Redis costs at most one short timeout per cooldown, not one per request.
  - *Alternatives considered:*
    - *Fail closed (refuse traffic):* a cache outage would take inference down.
    - *Fail open (no limits):* the GPUs lose their protection exactly when something is
      already wrong.
  - A misconfigured URL also lands in "degraded" rather than blocking startup, and it's
    visible on `/health`.
- **What didn't work:**
  - *The first draft imported the circuit breaker into the state package.* That would have
    been a circular import (`gateway.dispatch` imports the state package). The store health
    tracker got its own small cooldown instead.
  - *A regex converter for the async test changes* prefixed `await` onto list comprehensions
    (`await [x.try_acquire() ...]`). It also made one nested helper `async` because it
    shared a block with the calls. Both were caught by the first test run and fixed by hand.
  - *The embedding queue* took a sync callable. It now accepts sync or async (the enforcer is
    async), so other callers don't break.
- **What works now:**
  - `tests/test_shared_state_redis.py` (12 tests against a real Redis 7; skipped when none is
    reachable, so CI without Redis stays green):
    - two clients share one cap;
    - a release in one process wakes a waiter in another in under 1 s, with polling set to
      5 s (so the notification did it);
    - the batch share holds across processes;
    - a "crashed" process's slots expire;
    - the heartbeat keeps a long request's slot past 2.5 TTLs;
    - a release interrupted by cancellation still lands;
    - shared rate windows, including scaled limits;
    - fallback when Redis is unreachable, and recovery when it returns;
    - two full dispatchers, 12 requests, engine peak exactly 2.
  - All 759 existing tests pass on the in-memory default.
  - **End-to-end run:** two real gateway processes behind one Redis and a fake engine with
    `max_concurrent: 2`:
    - 8 requests split across both → all 200, engine peak 2;
    - a key with `max_concurrent: 1` hit on both gateways at once → 200 + 429;
    - Redis killed → all requests still served under per-process limits, `/health`
      degraded on both, one error line per process, no log spam;
    - Redis restarted → both back to `ok`.

## D-036: Model discovery on OpenAI-compatible endpoints; endpoint credentials

- **Status:** Implemented, 2026-10-07
- **Problem 1, discovery:** discovery knew Ollama, vLLM, TRT-LLM and SGLang. `type: openai`
  endpoints were skipped with "Unknown endpoint type": LM Studio, llama.cpp server, LocalAI,
  OpenAI. Their models never reached the catalog, the dashboard showed "0 models", and a
  request naming one of their models couldn't be routed to them by name. Found during the
  D-034 end-to-end run.
- **Problem 2, credentials (found while fixing 1):** keys never reached the upstream.
  - The registry built adapters from `endpoints:` entries without `api_key_env`.
  - The legacy `providers:` format is converted to endpoints at load time, and that
    conversion dropped `api_key` and `headers`.
  - So a keyed cloud or LAN server got no `Authorization` header in either format.
  - The vLLM adapter had no auth support at all, so a vLLM started with `--api-key` was
    unusable.
- **Fix:**
  - **Discovery:** one OpenAI-compatible routine (`GET /v1/models`) serves vLLM, SGLang and
    `openai` endpoints. It sends the endpoint's credentials.
  - **Credentials:**
    - Endpoints accept `api_key` (literal or `${ENV}`), `api_key_env` and `headers`.
    - The legacy conversion and the registry carry all three through.
    - Key resolution lives in one place (`providers/auth.py`), used by the OpenAI and vLLM
      adapters and by discovery.
  - **Media models stay out of the chat catalog:** a Kokoro server lists `kokoro`/`tts-1`, a
    Whisper server lists `faster-whisper-small`, and neither is a chat model.
    - An endpoint that declares media `capabilities` contributes no text models by default.
    - A mixed server (LocalAI, OpenAI cloud) sets `serves_text: true`.
    - Either way, models labeled with a media `task` (speaches does this) are skipped.
- **Alternatives considered:** *guessing media models by name* ("whisper", "tts", "kokoro"…).
  It's fragile (new engines, renamed models) and silently wrong when it misses. The explicit
  rule costs one line of config for the uncommon mixed server.
- **Behavior change to note:** a vLLM endpoint that declares `capabilities: [stt]` (vLLM
  serving Whisper) no longer adds its Whisper model to the chat catalog. Before, a chat request
  for it was routed there and failed.
- **What works now:** `tests/test_discovery.py` (11 tests; there were no discovery tests
  before):
  - openai-type discovery, with routing by model name;
  - Bearer key and extra headers sent, and no header without a key;
  - one failing endpoint doesn't block others;
  - vLLM unchanged;
  - media endpoints skipped, and the mixed-server opt-in with task filtering;
  - credentials surviving both config formats;
  - vLLM sending its key.

  On a real gateway, the D-034 fake engine now lists `fake-7b`, chat routes to it by name,
  and the startup warning is gone.

## D-037: Token budgets persisted to the database

- **Status:** Implemented, 2026-10-07
- **Problem:**
  - Daily budget usage lived only in process memory. A restart (deploy, crash, config change)
    gave every key a fresh budget mid-day.
  - Tiers and model assignments changed from the dashboard were lost on restart, although
    the README says "no restart needed".
  - With several gateway processes, each tracked only its own share, so a key could spend
    its budget once per process.
- **Decision:** the database, not Redis. Budgets are durable accounting that operators
  report on, and the database is already required (D-008). Redis stays optional (D-035).
- **How it works:**
  - **Checks never wait on the database.** The tracker still counts in memory: a persisted
    *baseline* (today's totals as read from the database) plus *pending* usage recorded since
    the last write.
  - **Every 2 s, a background `BudgetSync` writes the pending usage** as atomic increments
    (`INSERT … ON CONFLICT DO UPDATE SET n = n + excluded.n`, on SQLite and Postgres), then
    re-reads today's totals.
    - Several processes can add to the same row safely.
    - Each process sees the others' spend within about one interval.
  - **Table:** `budget_usage(day, client_id, tier → weighted_tokens, raw_tokens, requests)`.
    Weighted tokens count against the key's budget; raw tokens count against the tier's
    global cap. Rows are pruned on the same retention as audit logs.
  - **Crash cost:** a clean shutdown writes the last batch. A crash loses at most one
    interval (~2 s) of usage.
  - **A batch being written stays counted.** Without that, a budget check during the write
    would see neither the old pending entry nor the new baseline, and could let a key spend
    one batch's worth twice. If the write fails, the batch returns to pending and is retried.
  - **Tiers and assignments** are saved as one document in `runtime_settings`
    (`budget.catalog`) on every dashboard change, which also records who made it. A saved
    document overrides `gateway.yaml`'s tiers, the same rule as other dashboard settings (the
    PII toggle). Other processes pick up a change within ~10 s.
  - **The policy enforcer is built at startup**, not on the first request, so saved state
    loads before traffic arrives.
  - **Without a database** (tests, `GATEWAY_DB_REQUIRED=false`), budgets behave as before,
    and past days are dropped instead of accumulating.
- **Alternatives considered:**
  - *Write on every request:* exact, but it adds a write to every response on SQLite's
    single writer. The audit row already costs one.
  - *Rebuild usage from the audit log at startup:* no new table, but audit writes are
    best-effort (spill file) and don't record tier weights. It also doesn't solve sharing
    across processes.
  - *Per-key budget rows in the API keys table:* doesn't hold tier totals, and keys from
    config have no row.
- **Trade-off:** with several processes, a key can overshoot by what the other processes
  spent in the last ~2 s. That's acceptable for a *daily* budget; a strict per-request check
  needs a database round-trip on every request.
- **What didn't work:**
  - *A cleanup condition written before today's entry existed:* a lone stale day was never
    dropped (caught by a test).
  - *Comparing the saved catalog's timestamp:* SQLite returns naive datetimes, which can't
    be compared with aware ones. Normalized to UTC.
  - *The dashboard usage endpoint* read the tracker's private `_usage` dict. It now uses
    `keys_today()`.
- **What works now:**
  - `tests/test_budget_persistence.py` (12 tests):
    - usage and request counts survive a restart and are enforced after it;
    - tier-cap totals survive;
    - dashboard tier, assignment and unassignment changes survive;
    - two processes see each other's spend, and a catalog change reaches the other
      process;
    - a failed write keeps the usage counted and retries;
    - a batch being written is neither lost nor double-counted;
    - increments, not overwrites;
    - pruning;
    - no-database cleanup.
  - Migration `e3a7c1d94f20` upgrades and downgrades.
  - **End-to-end** (real gateway, SQLite, fake engine; 20 weighted tokens per request,
    budget 100):
    - 3 requests and a tier added from the dashboard;
    - clean restart → usage still 60 tokens / 3 requests, tier still there; the 7th request
      overall → 403 `token_budget_exceeded`;
    - `kill -9` three seconds after the last request, then restart → no usage lost.

## D-038: Audit intent log: a write funnel in front of SQLite

- **Status:** Implemented, 2026-10-07. Replaces the plan in D-007 (an in-memory queue).
- **Problem:**
  - Every response, and `[DONE]` on every stream, waited for its audit insert to commit.
    When PII was found, that insert even ran *before* the model call.
  - SQLite allows one writer at a time, and each write opened its own connection and forced
    a disk flush. Under load, requests queued for the lock.
  - **Measured** (fake instant engine, so only gateway overhead): at 50 concurrent requests,
    p95 2.5 s and p99 4.8 s, almost all of it waiting on SQLite.
- **Decision (Peter):** a ZFS-SLOG-style intent log, not "switch to Postgres". It keeps the
  gateway deployable as a simple local system:
  - a request appends its audit rows to a local, append-only log file and responds;
  - one background drainer per gateway process writes them to the database in batches.

  As an outside review put it: an application-level write funnel, a tiny write-ahead broker
  in front of SQLite, not a database replacement. It fits because the writes are many small,
  independent facts, not concurrent relational transactions.
- **PostgreSQL path:** `GATEWAY_DB_AUDIT_DURABILITY=auto` (the default) picks the log on
  SQLite and direct writes on PostgreSQL. Postgres handles concurrent writers, and it usually
  runs in containers where a pod's local disk can vanish with unwritten records on it. Either
  database can choose any mode explicitly.

### The specification (the review's questions, answered)

Once the gateway acknowledges before the database commits, the log is part of the storage
system. These questions are its contract.

1. **What happens if the process crashes after acknowledging a request?**
   The record is already in the log file.
   - `process` mode: written to the OS (survives any gateway crash, including `kill -9`). A
     power cut or kernel crash can lose what the OS hadn't written back, up to ~30 s with
     Linux defaults (`vm.dirty_expire_centisecs`).
   - `grouped` mode: the request waits until its record has been forced to disk, so nothing
     acknowledged is ever lost, even on power loss.
   - `sync` mode: no log; the database commit happens before the response.
2. **How do we know which log entries reached the database?**
   Each process's log has a position (segment file and byte offset) stored in the
   `audit_journal` table. It's updated **in the same transaction** as the rows it covers, so
   rows and position commit together or not at all.
3. **Are operations idempotent?**
   They don't need to be. Correctness comes from that atomic position, not from re-running
   being harmless. (`audit_log.request_id` is unique, which also guards it; `pii_events` has
   no unique key, so it relies on the position alone.)
4. **How do we replay without double-applying?**
   Replay starts at the committed position, and every batch moves the position in the same
   transaction. A crash before a commit replays that batch once; after it, the batch is past
   the position. Exactly-once.
5. **When can a log segment be discarded?**
   Only a *closed* segment, and only after the position has moved past its last byte. If
   the process dies between that commit and deleting the file, the next drain finds nothing
   after the position and deletes it then.
   - One explicit exception: the size cap (see 6).
6. **What happens when the log grows faster than SQLite can drain it?**
   **Measured:**
   - the drainer writes **~14,500 rows/s** into SQLite (batches of 500, one transaction
     each);
   - one gateway process tops out around 120–140 req/s on the same machine;
   - an append costs ~18 µs.

   So in normal operation the drain can't fall behind (~100× headroom). The log grows only
   while the database is unreachable or locked:
   - it buffers up to `GATEWAY_DB_AUDIT_JOURNAL_MAX_MB` (default 1 GB, millions of rows);
   - past that, the **oldest** closed segment is dropped, logged as critical and counted in
     `*_audit_write_failures_total{outcome="lost"}`;
   - the gateway keeps serving. Refusing requests at the cap ("strict audit") would be an
     opt-in setting if a deployment needs it; not built yet.
7. **Does ordering matter globally, per table, or per key?**
   - **Within a process:** strict log order, preserved by the drainer.
   - **Across processes:** no order. Each process has its own log and drainer; rows carry
     their own timestamps.

   That's sufficient because everything in the funnel is an **insert-only fact** (audit rows,
   PII events). There are no updates or deletes whose order could matter. Anything needing
   read-your-writes or conflict detection (API keys, settings) stays a direct transaction. If
   more traffic goes through the funnel later, the same rule applies: only append-only facts
   or commutative increments (like budget usage, D-037).

### Other details

- **Layout:**
  - `<journal>/<pid-random>/` holds one process's numbered 64 MB segments and a lock file
    held for its lifetime.
  - At startup, and every 30 s, a process looks for log directories whose lock is free (their
    process died), drains them in order, and removes them. That covers restarts and dead
    workers alike.
  - A torn final record (a crash mid-write) is left alone, never half-parsed.
- **A bad row doesn't block the log:** on a constraint error the batch is retried row by
  row; rejected rows are logged and counted (`outcome="rejected"`), and the rest go in.
- **If an append fails** (disk full), that row is written directly to the database instead.
- **Visibility:**
  - `/health` → `audit: {mode, backlog_records, oldest_pending_seconds, log_bytes,
    database_reachable, records_dropped}`;
  - Prometheus `*_audit_backlog_records` and `*_audit_backlog_oldest_seconds`;
  - one error log when the database becomes unreachable, and one when it's back.
- **Group commit (`grouped`):** leader-based. The first request needing durability starts
  an fsync right away; requests arriving meanwhile share the next one.
  - *The first version* waited a fixed 50 ms window, so a lone request paid 50 ms (63 ms
    median at concurrency 1). Now a lone request pays one fsync. Measured fsync here:
    0.2 ms; expect 1–5 ms on SSDs, more on spinning disks or network volumes.
- **Containers:** put `GATEWAY_DB_AUDIT_JOURNAL_PATH` on a persistent volume, next to the
  SQLite file. A log on ephemeral storage defeats the point.

### Bugs found while building this

- **Audit writes on PostgreSQL were broken, and had been for a while.** The gateway writes
  timezone-aware times into `TIMESTAMP WITHOUT TIME ZONE` columns. SQLite accepts that;
  PostgreSQL's driver rejects it, so every audit row (and budget setting) on PostgreSQL
  failed and spilled. Nothing was tested against PostgreSQL before.
  - Fix: a `UTCDateTime` column type stores UTC with no zone (same type on disk, no
    migration) and always reads back UTC-aware.
  - The intent-log, budget and storage tests now run on both SQLite and a real PostgreSQL
    16 (`tests/conftest.py` `db_engine`; the PostgreSQL run skips when none is reachable).
- **Response IDs didn't match audit request IDs.** `chatcmpl-…` came from a second random
  ID, so a client couldn't find its request in the audit trail. Found when the crash test
  tried to match responses to rows. The OpenAI routes now use the audit request ID end to
  end.
- **`rate_limits.enabled`** was missing from `gateway.yaml` (needed to load-test with one
  client). Added; default on.

### What didn't work

- *Fixed-window group commit:* see above.
- *A column named `offset`:* a reserved word in SQL. Renamed to `byte_offset` before it
  shipped.
- *Killing benchmark servers with `pkill -f`:* the pattern also matched the shell running
  the benchmark. Switched to PID files.

### Measured (fake instant engine, SQLite, one gateway process)

| mode | concurrency | p50 | p95 | p99 | req/s |
|---|---|---|---|---|---|
| sync (old) | 1 | 19.7 ms | 25.9 ms | 29.4 ms | 50 |
| sync (old) | 50 | 151.6 ms | 2,541 ms | 4,753 ms | 81 |
| process | 1 | 13.2 ms | 22.4 ms | 36.4 ms | 70 |
| process | 50 | 373 ms | 676 ms | 931 ms | 122 |
| grouped | 1 | 14.3 ms | 21.9 ms | 30.0 ms | 68 |
| grouped | 50 | 352 ms | 555 ms | 724 ms | 134 |

Streams look the same (time to `[DONE]`): sync p99 5.8 s → 0.55 s at 50 concurrent.

**Why the median at 50 concurrent rose:** in sync mode, requests that win the SQLite lock
finish fast and the rest wait seconds. That gives a low median and a huge tail. Average
latency is concurrency ÷ throughput: 50 ÷ 81 = 617 ms (sync) vs 50 ÷ 122 = 410 ms (log).
The gateway's single Python process, at ~120–140 req/s here, is now the limit (the "single
worker" readiness item), not SQLite.

### What works now

- `tests/test_intent_log.py`: 14 tests, each run on SQLite and PostgreSQL:
  - rows reach the database, in order, across segments; segments are deleted;
  - a clean close drains everything;
  - **`kill -9` loses nothing and repeats nothing** (checked on `pii_events`, which has no
    unique key);
  - a live process's log isn't taken by another;
  - a torn record is left alone;
  - an outage keeps the backlog and drains it after;
  - the size cap drops the oldest and keeps serving;
  - group commit shares fsyncs, and a lone request isn't delayed;
  - `process` mode never fsyncs;
  - a rejected row doesn't block its batch;
  - `AuditLogger` integration, and the disk-full fallback.
- **End-to-end `kill -9` under load** (20 concurrent clients, real gateway, SQLite):
  - 256 requests got a 200 before the kill; 36 of them were only in the log at that moment;
  - after restart, the new process drained the dead one's log;
  - 256 rows, each matched to its response ID, 0 missing, 0 duplicates.
- **PostgreSQL end-to-end:** `auto` chose direct writes; rows landed with correct UTC
  timestamps.

### Correction (found while building D-040): exactly-once wasn't true on SQLite

- **What was wrong:** the batch insert runs inside a savepoint, so one bad row can be skipped.
  Python's `sqlite3` driver doesn't start a real transaction before a `SAVEPOINT`, so the
  savepoint opened its own transaction and `RELEASE` **committed** it. On SQLite the rows
  were committed *before* the position update, so "rows and position commit together" was
  false.
  - A crash, or a second drainer, between the two could insert a batch twice.
  - PostgreSQL was unaffected.
- **How it was found:** the `kill -9` test failed once in a full-suite run (30 PII rows
  instead of 25). The cause was a test artifact: its simulated crash didn't wait for the
  cancelled drainer to stop, so two drainers briefly ran. A new test races two drainers on
  purpose. It failed every time on SQLite, never on PostgreSQL, and that pointed at the
  driver.
- **Fixes:**
  1. **Real SQLite transactions** (`storage/engine.py`, SQLAlchemy's documented recipe): the
     driver's own transaction handling is off, and SQLAlchemy emits `BEGIN` at the start of
     every transaction. Savepoints nest properly. The WAL pragma moved into the connection
     hook, because journal mode can't change inside a transaction. This applies to every
     SQLite transaction in the gateway; the full suite passed three runs in a row.
  2. **The position update is a compare-and-set:** it only succeeds if the stored position
     is still the one the batch was read from, otherwise the whole transaction rolls back.
     Exactly-once is now enforced by the database itself, not just by the lock file that
     keeps a second drainer away.
- **Tested since:**
  - `test_two_drainers_on_one_log_never_double_apply`: three drainers racing over 60 PII
    records leave exactly 60 rows, on both databases.
  - End-to-end `kill -9` re-run after the fix: 206 acknowledged, 60 only in the log at the
    kill, 206 rows after restart, 0 missing, 0 duplicates.
  - Throughput at 50 concurrent unchanged (131 req/s, p99 0.81 s).

## D-039: Deployment tiers: Postgres is a scaling/security decision, not an install tax

- **Status:** Proposed, 2026-10-07 (from Peter's discussion with a reviewer). Nothing built
  yet beyond what D-035 and D-038 already provide.
- **Problem:** "SQLite hit write contention, so use Postgres" is an expensive answer to a
  narrow problem.
  - Postgres brings another service with credentials, backups, upgrades, health checks,
    connection handling, network exposure and its own failure domain.
  - For one user or a small trusted team, the workload is SQLite-shaped: many small,
    independent writes and mostly reads. D-038 removed the actual bottleneck (bursts of
    writes) at the application layer.
- **The real reason to move is tenant isolation, not write volume.** In SQLite, isolation can
  only be enforced in application code: every query must carry the tenant filter, and one
  missed `AND tenant_id = ?` is a cross-tenant leak. PostgreSQL row-level security makes the
  database refuse other tenants' rows even when a query forgets the filter:
  `CREATE POLICY tenant_isolation ON audit_log USING (tenant_id = current_setting('app.tenant_id'));`
- **Proposed tiers:**

  | Tier | Store | For |
  |---|---|---|
  | **Embedded** (default) | SQLite + intent log (D-038), single writer per process | one user, trusted office or team; install and run, no DBA |
  | **Server** | PostgreSQL; tenant ID on every row; RLS policies; per-tenant roles and quotas; connection pooling | multiple customers or untrusted user groups; several app instances on one database |
  | **Enterprise** | Server + external identity (OIDC/SAML), stricter audit and retention, separate audit store | regulated deployments |

  Redis (D-035) is orthogonal: an add-on for several gateway processes, at any tier.
- **When to cross from Embedded to Server:**
  - tenants that must not see each other's data;
  - several independent services mutating shared state concurrently;
  - or the write funnel turning into a distributed coordination system. If D-038's log ever
    needs cross-process ordering or consensus, stop and use Postgres instead.
- **Where the code stands:**
  - Embedded works and is the default.
  - PostgreSQL works as a store: tested against a real Postgres since D-038, which also
    fixed its broken audit writes.
  - There is **no tenant concept**. API keys have a `client_id`, and the dashboard filters
    reads by it in application code (D-005). Server mode needs:
    - a `tenant_id` on keys and on every stored row;
    - the tenant set per database session (`SET app.tenant_id`);
    - RLS policies, applied by migration on PostgreSQL only;
    - tests proving a query without a tenant filter returns nothing across tenants.
- **Not decided yet:**
  - whether Server mode requires PostgreSQL, or SQLite tenants are allowed with a warning;
  - how the dashboard's admin role spans tenants;
  - whether audit gets its own database at Enterprise.

## D-040: Cache validated API keys; batch last_used_at

- **Status:** Implemented, 2026-10-07
- **Problem:** every request with a database-backed key ran a SELECT plus an UPDATE of
  `last_used_at` and a commit, on SQLite's single writer, *before* the request could start.
  **Measured** (real gateway, SQLite, fake instant engine, cache off):
  - one request: 28.7 ms median;
  - 10 concurrent: p99 1.05 s;
  - 50 concurrent: requests **failed** with 500 `sqlite3.OperationalError: database is
    locked`. The UPDATEs queued past SQLite's lock timeout.
- **Fix:**
  - **Validated keys are cached for 30 s** (`GATEWAY_DB_KEY_CACHE_SECONDS`; 0 disables).
    Repeat requests do no database work. A key's own `expires_at` is checked on every cache
    hit, so expiry isn't delayed by the cache.
  - **Unknown keys are remembered for 5 s**, so a client spraying random keys can't turn each
    guess into a database query. Creating a key clears any "unknown" entry for it.
  - **Size-bounded** (10,000 keys, least-recently-used out).
  - **`last_used_at` is coalesced in memory** (latest time per key) and written in one
    transaction every 30 s and on shutdown. The UPDATE never moves it backwards, so a late
    flush from another process can't regress it. A failed flush keeps its times for the next
    attempt.
- **Why `last_used_at` doesn't go through the audit intent log (D-038):** it's an
  informational timestamp, not a record of what happened. Every request is already in the
  audit trail with its client. Losing up to 30 s of `last_used_at` freshness on a crash costs
  nothing; making it durable would add log traffic for no benefit. It's also an update, and
  the intent log carries only append-only facts.
- **Revocation:**
  - **Immediate** in the gateway process that handled `DELETE /api/keys/{id}`: it drops the
    key from its cache before responding.
  - **Other processes** stop accepting it within the cache lifetime (30 s by default). Set a
    shorter lifetime if that's too long for a deployment, or 0 to disable caching.
  - A cross-process invalidation broadcast (e.g. over the optional Redis) is possible later;
    not built.
- **Alternatives considered:**
  - *Write `last_used_at` on a sample of requests:* still writes on the request path.
  - *Drop `last_used_at`:* the dashboard shows it, and it's how operators find unused keys.
  - *Cache without the negative entries:* leaves invalid-key spraying as a DB load vector.
- **Measured after** (same setup, cache on):

  | concurrency | cache off p50 / p99 / req/s | cache on p50 / p99 / req/s |
  |---|---|---|
  | 1 | 28.7 ms / 47.9 ms / 34 | 11.6 ms / 21.0 ms / 84 |
  | 10 | 135 ms / 1,052 ms / 55 | 67.8 ms / 172 ms / 143 |
  | 50 | **500 errors** (database is locked) | 374 ms / 938 ms / 121, no errors |

- **What didn't work:** *my first end-to-end shutdown check reported `last_used_at` empty.*
  The test was wrong, not the code: `cd … && … python … &` backgrounds the whole `&&` chain,
  so `$!` was a wrapper shell, and the kill never reached the gateway. The gateway was still
  running and never shut down. With the real process stopped, the shutdown flush wrote it.
- **What works now:** `tests/test_key_cache.py` (13 tests, each on SQLite and PostgreSQL):
  - one lookup for 50 validations;
  - the cache lifetime;
  - a key expiring while cached is refused;
  - the size bound;
  - revocation immediate in the same process, and followed within the lifetime by another;
  - guesses don't each query the database;
  - a newly created key is accepted despite an earlier miss;
  - `last_used_at` coalesced, written on flush, never moved back, kept on a failed flush,
    written on stop;
  - the auth path uses the cache.

  **End to end:** a cached key gave 200, 200, then 401 immediately after `DELETE
  /api/keys/{id}`; `last_used_at` was written at clean shutdown.

---

## External review of `4390209`

An outside reviewer read the branch at `4390209` and ran the suite on Windows/Python 3.10.
Each finding below was reproduced before it was fixed. The fixes are D-041 to D-045.

## D-041: Unscrubbed PII reached engines (embeddings) and stayed in security scans

- **Status:** Implemented, 2026-10-07, `df17d9b`
- **Problem 1, embeddings:** `/v1/embeddings` scrubbed the input, logged "scrubbed", and then
  built the dispatched request from the **raw** body. The engine received the original
  text.
- **What didn't work:** *passing the scrubbed input as-is.* That exposed a second bug: text
  with no PII "scrubbed" to `None` (meaning "nothing changed"), so the request failed
  validation with 422.
- **Fix 1:** the dispatched request is built from the sanitized input, falling back to the
  original only when the scrubber reports no change (`is not None`, not truthiness).
  `tests/test_wire.py` now checks what the engine actually receives on the chat and
  embeddings routes (OpenAI and Ollama), rather than what the gateway logged.
- **Problem 2, security scans:** every scan stored the full prompt. It was redacted only
  when PII detection was on, and scans were never deleted. That contradicted the audit
  settings (request bodies are off by default; D-005 keeps raw PII out of audit rows) and kept
  raw prompts forever.
- **Fix 2:**
  - **Messages are kept only by opt-in:** `GATEWAY_SECURITY_STORE_MESSAGES`.
    - `none` (default): verdicts and metadata only.
    - `flagged`: messages of requests the regex scanner or the guard model flagged, for
      review.
    - `all`: every request, for training-data collection.
  - **Kept messages are always PII-redacted**, whether or not PII detection is on for the
    request path.
  - **Scans and PII events expire:** `GATEWAY_SECURITY_RETENTION_DAYS` (default 90, 0 =
    keep). This runs in the same retention loop as audit rows.
  - Training-data export skips scans whose messages weren't kept, instead of exporting
    empty prompts.
- **Trade-off:** the dashboard's scan review and labeling show a placeholder for scans
  without messages. Teams that label scans set `flagged` (or `all`) explicitly.
- **What works now:**
  - `tests/test_wire.py` (12 tests): engines receive scrubbed text, inline images and
    unchanged clean text.
  - `tests/test_security_store.py`: the default stores no messages; `flagged` keeps only
    flagged ones; `all` keeps everything and is redacted; old scans are deleted; export
    skips withheld scans.

## D-042: Access modes: keys, solo and a test mode; secure by default

- **Status:** Implemented, 2026-10-07, `b8e0228`. **Reverses D-001's default** and part of
  D-002.
- **Problem:** secure operation depended on optional config.
  - Keyless inference was on unless turned off (D-001 left the default open).
  - With auth on but no `GATEWAY_ADMIN_API_KEY`, **any client key** could reach the
    dashboard, key management and budgets.
  - With auth off, the gateway accepted requests from anywhere.
- **Also needed:** a way for the operator to try a real config without minting keys
  ("We need like a test mode I can use to test stuff out").
- **Fix, three modes:**
  - **keys** (`auth.enabled: true`):
    - every request needs a key;
    - keyless requests only if `auth.anonymous.enabled`, and then only from
      `auth.anonymous.allowed_networks` (default: this machine);
    - admin routes need `GATEWAY_ADMIN_API_KEY`. Without it they refuse with 403
      `admin_key_required`, rather than falling back to client keys.
  - **solo** (`auth.enabled: false`): one person on one machine. No keys, but requests only
    from `auth.anonymous.allowed_networks` (default loopback). A key doesn't bypass the
    network check.
  - **test mode** (`GATEWAY_DEV_MODE=true`, or `./start-gateway.sh --dev`):
    - keyless inference and dashboard from `GATEWAY_DEV_NETWORKS` (default this machine),
      whatever the auth config says;
    - requests that send a key are still checked (a wrong key is still 401);
    - audited as client `dev`;
    - the script uses a separate database (`data/dev.db`) and journal;
    - loud everywhere: startup banner, `/health` `access.mode`, dashboard banner.
  - **`GATEWAY_PROFILE=production`** refuses to start with test mode on, auth off, no admin
    key, or keyless access with no model, endpoint or rate restriction. It lists every
    problem at once.
- **"This machine" is the TCP peer address.** A request carrying proxy headers
  (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`, `X-Forwarded-Host`) never counts as local.
  With a reverse proxy on the same host, every internet request would otherwise arrive from
  127.0.0.1. Trusted-proxy support is a separate step, not built.
- **Breaking change:** keyless clients on other machines (LocalClaw, stock Ollama clients)
  need:

  ```yaml
  auth:
    anonymous:
      enabled: true
      allowed_networks: ["192.168.1.40/32"]   # the client's address or subnet
  ```

- **Alternatives considered:**
  - *Keep the open default with a louder warning:* that's what D-001 did; the review showed
    warnings aren't a control.
  - *Test mode as "auth off":* it would also drop key checks for clients that do send keys,
    so the config being tested isn't the one that runs.
- **What works now:** `tests/test_access_modes.py` (15 tests): keyless refused by default;
  opt-in only from allowed networks; admin key required; solo is local-only; a key doesn't
  bypass locality; proxied requests aren't local; test mode works with auth on, still checks
  keys and is limited to its networks; `GATEWAY_DEV_NETWORKS` parsing; the production
  profile's checks.

## D-043: Budgets are hard limits: reserve at admission, settle on completion

- **Status:** Implemented, 2026-10-07, `68f5968`. Amends D-037.
- **Problem** (reproduced through the real route):
  - Usage counted only after a request finished, so concurrent requests all passed a check
    against the same remaining budget. **10 concurrent requests against a 100-token
    budget: all 10 admitted.**
  - An exhausted budget admitted zero-estimate requests: `used + 0 > limit` is false at
    `used == limit`.
  - A request with no `max_tokens` estimated zero output.
  - An interrupted stream that produced only reasoning (`thinking`) or tool-call chunks
    counted as zero tokens.
- **Fix:**
  - **Admission reserves** the estimated cost. The check and the hold happen with no
    `await` between them, so on one event loop they're atomic.
  - **Completion settles:** the hold is replaced with actual usage (streams included).
  - **Failure releases:** holds of failed or abandoned requests are released when the
    request ends (the same teardown that releases key slots).
  - **A lost hold expires** after 10 minutes, so a bug can't pin a budget forever.
  - **Estimate:** prompt (about 4 characters per token, plus 1) plus `max_tokens`, or the
    configured default when unset. Embeddings and audio have no output tokens.
  - **Exhausted is exhausted:** `used >= limit` refuses, whatever the estimate.
  - Thinking and tool-call chunks count toward a stream's estimate.
- **Scope:** reservations are per process. Several gateway processes sharing a budget can
  each admit up to the remaining budget once. The shared Redis route (D-035) can carry
  reservations later; not built.
- **What works now:** `tests/test_budget_reservations.py` (10 tests):
  - 10 concurrent requests on a 100-token budget: exactly 1 admitted, settled to the
    actual 10 tokens;
  - a failing engine leaves no hold and no usage;
  - a streamed request settles to the usage the engine reported (7), not the 500 estimate;
  - the tracker's reserve, settle, release, expiry and exhausted-budget rules.

## D-044: Compose keeps its data, and the dashboard works from any browser

- **Status:** Implemented, 2026-10-07, `e065815`
- **Problem:**
  - The gateway's SQLite database and audit intent log lived in the container's filesystem.
    `docker compose down` and `up` lost every key, budget and audit row.
  - The dashboard image was built against `http://gateway:8000`, a name only Docker's
    internal DNS resolves. Browsers couldn't reach the API.
- **Fix:**
  - `/app/data` is a named volume (`gateway-data`), owned by the image's non-root user.
  - The dashboard is served by nginx, which also proxies `/api`, `/v1`, `/health` and
    `/metrics` to the gateway. It's built with an empty API base (same origin). Streams are
    unbuffered (`proxy_buffering off`).
  - **Compose requires `GATEWAY_ADMIN_API_KEY`** (`${GATEWAY_ADMIN_API_KEY:?}`). Behind the
    proxy nothing counts as local (D-042), so the operator key is the only way into the
    dashboard.
  - The gateway image no longer installs curl; its healthcheck uses Python. A
    `.dockerignore` keeps `node_modules`, `data` and `.git` out of the build context.
  - Redis is under the `ha` Compose profile, so it starts only when asked for.
- **What didn't work** (in this sandbox, not in the shipped files): apt has no plain-HTTP
  egress here, so `apt-get install curl` failed. That is one reason the image dropped
  curl. Image builds were verified with scratch copies that add the proxy CA.
- **What works now** (verified by building both images and running the Compose file):
  - `docker compose up` without the admin key refuses to start and names the variable;
  - the dashboard and API work through nginx;
  - a key created before `down`/`up` still works afterwards.

## D-045: CI proves the PostgreSQL, Redis and Windows claims

- **Status:** Implemented, 2026-10-07, `5b7e7e3`
- **Problem:**
  - CI ran SQLite only. The PostgreSQL and Redis tests skipped silently, so "works on
    PostgreSQL" and "works with Redis" were tested only on the developer's machine.
  - The reviewer's Windows/Python 3.10 run had 6 failures: 5 in the audit intent log, 1
    timing.
- **Fix:**
  - **Linux CI** (Python 3.11, 3.12, 3.13) runs against `postgres:16` and `redis:7` service
    containers. `GATEWAY_TEST_REQUIRE_SERVICES=1` makes an unreachable server an
    **error** instead of a skip.
  - **A `windows-latest` job** (Python 3.11, 3.13) runs the suite on SQLite.
  - **The intent log on Windows:**
    - process locks use `msvcrt` byte-range locking where `fcntl` is missing. Before, orphan
      recovery was simply off on Windows, so a crashed process's log was never drained;
    - shutdown closes the active segment before the final drain deletes it;
    - orphan recovery unlocks a drained orphan before removing its directory. Windows can't
      delete a file that is still open; POSIX can, which is why Linux never failed.
  - **The timing test** measured a real disk `fsync` (tens of milliseconds on Windows
    runners) against a 40 ms bound. It now stubs `fsync` and checks the no-batching-window
    property it was written for.
- **What works now:** locally, the full suite with services required: 914 passed, 0
  skipped. Windows is proven by the CI job, not locally.
- **Found while checking Python 3.10:**
  - **The bug:** shutting down could hang forever. `RedisConcurrency.close()` hung in
    `test_shared_state_redis.py` every time.
  - **Cause:** on Python before 3.12, `asyncio.wait_for(event.wait(), t)` returns normally if
    the event is set at the same moment the task is cancelled (CPython gh-86296). The
    Redis slot pump is woken by a release and cancelled right after, so it never stopped. The
    audit drainer had the same pattern.
  - **Fix:** both loops use `gateway.aio.wait_event`, built on `asyncio.wait`, which never
    swallows a cancellation.
  - **Proven by:** `tests/test_aio.py`. The old pattern hangs on 3.10 and not on 3.13,
    which is why it went unseen on 3.13.
- **Python 3.10 dropped; 3.11 is the minimum.**
  - **Why:** 3.10 reaches end-of-life in October 2026, and no known deployment needs it. The
    Docker image doesn't use it.
  - **Cost:** hosts whose system Python is 3.10 (Ubuntu 22.04) need a newer Python, from
    `uv`, deadsnakes or the Docker image.
  - **Code changes:** ruff targets `py311`, so `datetime.UTC` and the built-in
    `TimeoutError` replace the 3.10 spellings. `(str, Enum)` stays as is (UP042 ignored),
    because `StrEnum` changes how members format.
  - **3.11 still has the `wait_for` cancellation bug**, so `wait_event` stays.
- **Found by the first Windows CI run:** request durations were measured as the difference
  between two `datetime.now()` readings. That's wall-clock time, which ticks every ~15 ms on
  Windows, so a fast request measured 0 ms and got no tokens-per-second. Wall-clock time can
  also jump with NTP and give negative latencies on any OS. `RequestContext` now measures
  durations with `time.perf_counter`; `start_time` stays a wall-clock timestamp for logs.
  This was the review's sixth Windows failure (`test_observability.py::test_full_request_flow`).

## D-046: Never cancel a task mid-transaction (SQLite write lock left held on Python 3.11)

- **Status:** Implemented, 2026-10-07. Found by the first CI run on `main` after #10.
- **Problem:** `main`'s Linux/Python 3.11 job failed two tests. 3.12, 3.13, Windows and lint
  passed.
  - **Crash recovery:** the survivor hit `database is locked` for the full 5 s busy timeout
    and drained 15 of 25 rows (`test_intent_log.py::test_kill_9_…[sqlite]`).
  - **Key-cache revocation:** a key expected to still be cached was gone
    (`test_key_cache.py::test_other_process_follows_within_ttl[sqlite]`).
- **Cause 1, a real bug:**
  - **Trigger:** on Python 3.11 with SQLAlchemy 2.1 and aiosqlite 0.22, cancelling a task in
    the middle of a database call can leave its SQLite connection unclosed. Python's cycle
    collector is the only thing that eventually frees it.
  - **Effect:** until then it holds the write lock, so every other writer waits out the busy
    timeout and fails.
  - **How it was pinned down:**
    - Isolated reproducer: cancel a transaction at a random moment, then write from another
      connection. It failed in 3–4% of runs on 3.11 and 0 of 600 on 3.12 and 3.13.
    - Savepoints made no difference.
    - While the write was blocked, no aiosqlite worker thread was left for the old
      connection, and `gc.collect()` released the lock at once.
  - **What didn't work:** telling SQLAlchemy the aiosqlite dialect has no "terminate", so an
    invalidated connection is closed normally (3 to 8 failures in 250, same as before).
  - **Where it bites:** this isn't confined to tests. A gateway shutting down cancels its
    background loops, and the drainer, orphan recovery, budget sync, key-cache flush and
    retention cleanup can each be cut off mid-transaction. The stress test hit it during
    `IntentLog.close()` itself.
- **Fix:**
  - Background database work runs shielded (`gateway.aio.Uninterruptible`). Cancelling a loop
    stops it between units of work, and shutdown waits for the transaction in progress
    instead of abandoning it. This is correct on every Python version and database.
  - Retention cleanup waits at most 10 s at shutdown, then is cancelled after all; the
    process exits next, which releases SQLite's locks.
  - Shielding also closes two smaller gaps:
    - **key cache:** pending `last_used_at` times taken for a flush were dropped when it was
      cancelled;
    - **budgets:** a flush cancelled after its commit but before confirming would be re-sent
      and counted twice. Usage was never lost, though: an unconfirmed batch is re-sent.
- **Still exposed:** a client disconnect cancels its request handler, and on 3.11 that can
  hit a database call. On SQLite the request path rarely writes by design:
  - audit rows go through the intent log (D-038);
  - keys are cached (D-040);
  - WAL read transactions don't block writers.

  An operator choosing `GATEWAY_DB_AUDIT_DURABILITY=sync` on SQLite with Python 3.11 is the
  exposed case; Python 3.12 or newer avoids it.
- **Cause 2, a test bug:** the key-cache test used a 100 ms cache lifetime around a real
  revoke. On a slow CI disk the revoke's commit took 125 ms, so the entry expired before the
  "still cached" check. The test now uses a controlled clock.
- **What works now:**
  - **Stress test of shutdown and crash recovery on 3.11:** 0 failures in 300 runs with the
    fix; 11 in 300 without it.
  - **`tests/test_aio.py`:** cancelling a loop doesn't cut off its work; `finish()` waits for
    it; `finish(timeout)` cancels after the timeout.
  - **Full suite:** 919 passed on 3.11 and on 3.13.
  - **The two failing tests:** 15 runs in a row on 3.11 without a failure.
