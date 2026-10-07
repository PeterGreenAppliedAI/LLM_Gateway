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
