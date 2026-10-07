# Decision Log

Design decisions for the gateway: what was chosen, why, what it costs, and what
would make us revisit it. Newest last. Read it before changing behavior that one of these
decisions covers. When you reverse a decision, add a new entry that supersedes the old one
rather than editing history.

**Statuses:** Proposed (awaiting sign-off), Accepted (agreed, may not be built yet),
Implemented, Superseded.

Related: [CLIENT_DEPLOYMENT_READINESS.md](CLIENT_DEPLOYMENT_READINESS.md) (gap report),
[ARCHITECTURE_ROADMAP.md](ARCHITECTURE_ROADMAP.md) (Phase 7 covers the observe-only
prompt-injection decision).

---

## D-001: Keyless inference stays allowed, governed by `auth.anonymous`

- **Status:** Implemented (2026-10-06, `bbfe4b5`)
- **Context:** Stock Ollama/OpenAI clients (e.g. LocalClaw) send no API key. With auth enabled,
  keyless requests ran with no restrictions, so any client could drop its key to escape its
  key's allowlists and rate limit.
- **Decision:** Keyless inference stays on by default. A new `auth.anonymous` block can disable
  it, or restrict its models, endpoints and shared RPM. Startup logs a warning while keyless
  access is unrestricted.
- **Why:** Requiring keys by default would break existing keyless clients on upgrade. An explicit
  policy closes the bypass for anyone who sets it.
- **Trade-off:** The default is still bypassable. It's the operator's job to configure it, and
  the warning says so.
- **Revisit when:** No keyless clients remain, at which point default `enabled: false`.

## D-002: The admin key covers everything; the control plane is operator-only

- **Status:** Implemented (2026-10-06, `d7ed7dc`, `a0e31b8`)
- **Context:** Budget and tier changes, alert deletion and scan labeling accepted any client key.
  Dashboard reads let any client key see every client's prompts and responses.
- **Decision:** Every control-plane read and write requires `GATEWAY_ADMIN_API_KEY`. The admin key
  is accepted anywhere a client key is, because the dashboard sends one key for everything.
  With no admin key configured, any valid key still works (backward compatible), and startup
  warns.
- **Why:** The dashboard is an operator console. Scoping every query per tenant is a larger
  feature.
- **Trade-off:** Clients have no self-service view of their own usage.
- **Revisit when:** Multi-tenant or MSP deployments need per-client views (Profile C).

## D-003: Routing scope = key allowlist ∩ environment, enforced in the dispatcher

- **Status:** Implemented (2026-10-06, `0f70f4a`)
- **Context:** The key's endpoint allowlist was only checked against the *requested* endpoint.
  Environments were configured but never applied, and an unknown `X-Environment` meant no
  restrictions.
- **Decision:** Each request carries `allowed_endpoints`: the key's allowlist intersected with
  the environment's endpoints. The dispatcher enforces it on every endpoint it tries, including
  model-based routing, priority order, fallback, streaming and `endpoint/model` pins. A key
  bound to an environment can't switch it with `X-Environment`. Unknown environment names are
  refused (403).
- **Why:** Enforcing at the single place that chooses endpoints is the only way to guarantee it
  holds on fallback.
- **Trade-off:** Keys with no environment may still choose one with `X-Environment`.
- **Revisit when:** Environments are used as a security boundary for keyless clients. Then
  stop keyless traffic from choosing its environment.

## D-004: Streams are charged to budgets and recorded on every exit path

- **Status:** Implemented (2026-10-06, `c41d764`)
- **Decision:** A shared `StreamRecorder` records audit, metrics and budget exactly once per
  stream: success, provider error, dispatch error or client disconnect. Failed and abandoned
  streams are still charged. Without upstream usage, completion tokens are estimated as one
  per content chunk (marked `completion_tokens_estimated`). The disconnect write is shielded
  from cancellation.
- **Why:** A failed or abandoned stream still used the GPU. Not charging it made streaming a
  budget bypass.
- **Trade-off:** Estimates are approximate. Ollama sends roughly one token per chunk; other
  backends may batch.

## D-005: PII is redacted at rest whenever detection is on, regardless of scrubbing

- **Status:** Implemented (2026-10-06, `27645da`)
- **Decision:** With PII detection on, stored audit bodies and `security_scans.messages` are
  scrubbed before persisting, even in flag-only mode. The model still receives the original
  text in flag-only mode. Text past the scan limit is dropped from storage, never stored
  unscanned. If redaction fails, nothing is stored.
- **Why:** "Raw PII is never stored" must hold in every mode. Scrubbing *before the model* and
  scrubbing *at rest* are different choices; this one is always on.
- **Trade-off:** Stored bodies and guard-model training data contain placeholders (`[EMAIL]`), not
  the original values.

## D-006: Audit writes retry and spill; the database is required at startup

- **Status:** Implemented (2026-10-06, `eb0e274`)
- **Decision:** Audit and PII-event writes retry briefly, then append to a fsynced JSONL spill
  file that is replayed on startup. Rows the database rejects, such as duplicates, are not
  retried. Failures are counted in `gateway_audit_write_failures_total{table,outcome}`.
  Startup fails if the database can't be initialized, unless `GATEWAY_DB_REQUIRED=false`.
- **Why:** A compliance product can't silently drop its audit trail or run without one.
- **Trade-off:** If both the database and the disk are unavailable, rows are lost. That's
  counted as `outcome="lost"` and logged at critical level.

## D-007: Audit writes move off the request path (background writer)

- **Status:** Accepted (2026-10-07), capacity plan phase 3
- **Decision:** Requests put audit rows on a bounded in-memory queue. One writer task inserts them
  in batches, with D-006's retry and spill. The queue is flushed on shutdown; when it's full,
  rows spill to disk rather than being dropped. `GATEWAY_AUDIT_MODE=sync` keeps today's
  behavior (commit before the response completes).
- **Why:** Removes database latency from every response and from time-to-first-token. One
  writer ends SQLite write-lock contention.
- **Trade-off:** A hard crash (kill -9, power loss) loses rows still in the queue, typically a
  few milliseconds' worth. Deployments that need zero loss use `sync`.

## D-008: Routing default is priority with overflow; least-loaded is opt-in

- **Status:** Accepted (2026-10-07), capacity plan phase 2
- **Decision:** New setting `resolution.strategy`. With `priority` (the default), requests go to
  the first endpoint in priority order that has the model **and a free slot**, and overflow to
  the next one only when it's full. With `least_loaded`, requests go to the endpoint with the
  fewest in-flight requests, and priority only breaks ties. Explicit pins stay hard, and D-003
  scope applies first.
- **Why:** Operators use priority on purpose, for example to prefer the big GPU. Changing how an
  idle system routes would surprise them. Overflow gives most of the benefit of balancing
  without that.
- **Revisit when:** Model residency (`/api/ps`, preferring boxes where the model is already
  loaded) is added, a planned follow-up for Ollama fleets.

## D-009: Admission control lives in the gateway (per-endpoint `max_concurrent`)

- **Status:** Accepted (2026-10-07), capacity plan phase 2
- **Decision:** The gateway counts in-flight requests per endpoint. When every endpoint that
  could serve a request is full, the request waits in a FIFO queue up to `max_queue_wait`
  (default 5s), then gets 503 + `Retry-After`. Streams hold their slot until they finish.
  When `max_concurrent` is unset, endpoints are unlimited (today's behavior).
- **Why:** Queueing inside Ollama is invisible, can't overflow to another box, and fails by
  timing out after minutes. Queueing in the gateway gives metrics, overflow routing and fast
  failure.
- **Trade-off:** Limits are per endpoint, not per model, even though Ollama's parallelism is per
  model. Per-key concurrency limits and priority classes are deferred.

## D-010: Single gateway process; shared state behind one interface; Redis later

- **Status:** Proposed (2026-10-07)
- **Decision:** Run one gateway process per deployment, with active/passive for high
  availability. Rate limits, budget counters, in-flight slots and key-cache invalidation go
  behind one state interface, implemented in memory only for now. A Redis implementation can
  then be added behind `GATEWAY_REDIS_URL`, plus a `docker compose --profile ha`, without
  touching callers.
- **Why:** The GPUs are the bottleneck, not the gateway. Requiring Redis would add a service
  every on-prem or air-gapped customer has to run, secure and back up, plus a new failure mode
  (whether to fail open or closed when Redis is down). Sharing in-flight slots across replicas
  also needs crash-safe leases, so it's a real phase of work, not a config flag.
- **Trade-off:** No active/active and no horizontal scaling until the Redis phase.
- **Revisit when:** Load tests show gateway CPU saturating before the GPUs, a customer requires
  active/active, or a Kubernetes deployment needs replicas > 1.

## D-011: Inline images pass through; image URLs are rejected

- **Status:** Proposed (2026-10-07)
- **Context:** The OpenAI chat format allows images inline (`data:` base64) or as URLs the server
  downloads. Ollama only accepts inline images. Today the OpenAI route silently drops both.
- **Decision:** Pass inline `data:` images through to the model. Reject `http(s)` image URLs with
  400 and a message saying to send the image inline.
- **Why:** Fetching client-supplied URLs would let any client make the gateway request internal
  addresses (GPU boxes, admin pages, cloud metadata). It would also make arbitrary outbound
  requests, breaking air-gapped and egress-controlled deployments, and leave only a mutable
  URL in the audit trail. Rejecting loudly beats today's silent drop.
- **Trade-off:** Clients that send image links must base64-encode them first. The OpenAI SDKs
  already do this for local files.
- **Revisit when:** A customer needs URL images. Then add opt-in fetching with a domain
  allowlist, private addresses blocked, and size and time caps.
