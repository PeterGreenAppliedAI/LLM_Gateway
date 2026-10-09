# DevMesh Gateway — Architecture & Roadmap

_Rewritten 2026-10-09. The January 2026 version of this file described a program that has
since shipped, changed shape, or been superseded; see git history for it. The living
documents are [DECISIONS.md](DECISIONS.md) (why things are the way they are, D-001…D-054)
and [CLIENT_DEPLOYMENT_READINESS.md](CLIENT_DEPLOYMENT_READINESS.md) (what holds, proven by
which test). This file is the standing overview and the forward list._

## What the gateway is

A self-hosted security and governance layer in front of heterogeneous inference runtimes
(Ollama, vLLM, OpenAI-compatible, media engines). One API in both OpenAI and Ollama
dialects; one audit trail; policy enforced at the only place it can't be bypassed.

**Design rules that decide arguments** (accumulated through DECISIONS.md, enforced by
tests):

1. **Never silently degrade.** Pass through what we don't police (`format`, `options`,
   `think`, `keep_alive` — verbatim); reject loudly what we do (4xx with the real reason,
   never a clamp, never an invented default).
2. **Normalize what you police, pass through what you don't.** The internal request model
   carries normalized fields for policy and verbatim engine-native fields for dispatch.
3. **The audit trail is the forensic instrument.** Full request shape (schema, options,
   think, tool names) and full response shape (content, thinking, tool calls) per row;
   failed dispatches included; PII hashed with provenance, never raw.
4. **Symptoms are real, diagnoses are suspect.** Every external bug report so far pointed
   at a real defect with a wrong mechanism. Verify against the audit DB before accepting a
   theory — and before shipping one.
5. **Capability is discovered, not assumed.** Models advertise tools/vision/thinking from
   the runtime's own metadata; endpoints advertise media capabilities; clients filter up
   front instead of discovering support via 400s.

## Current architecture (as deployed)

```
client ──> auth (keys / anonymous networks / admin) ──> sanitize ──> PII scan
      ──> policy (rate windows, token budgets reserved-at-admission, task pins)
      ──> admission (per-endpoint slots, FIFO handover, batch share)
      ──> dispatch (catalog-aware resolution, pins, circuit breakers, overflow
           filtered to endpoints that have the model) ──> adapter (Ollama / vLLM /
           OpenAI / media) ──> stream recorder (record-once audit, budget settle)
async: security analyzer (regex + optional guard model) · audit intent log (SLOG
       funnel, exactly-once drain) · budget sync · model & voice discovery
```

- **State:** SQLite (or PostgreSQL) migrated at startup; `runtime_settings` carries
  dashboard-managed config (PII scrubbing, budget tiers, routing policy) that overrides
  yaml and survives restarts. Optional Redis shares rate windows and slots across
  processes; budgets and breakers remain per-process (documented in D-035/D-043).
- **Access modes (D-042):** keys / solo (local networks only) / dev. Production profile
  refuses unsafe combinations at startup.
- **Routing (D-049/D-053/D-054):** resolution honors explicit `endpoint/model` pins
  (pins never fail over), per-key `target_endpoint`, model homes, task pins (hard, on
  every path), then priority or least-loaded order over endpoints that actually have the
  model.
- **Media (D-020…D-026):** OpenAI audio routes over any compatible engine; voice registry
  with engine profiles; budget reservation by characters/duration.

## Roadmap (in priority order)

1. **D-052 — ML PII gate (Laya + extractor).** Designed, blocked on one decision: how to
   store gate-training data without violating the redact-everything invariant (separate
   scoped raw store vs synthetic augmentation). Shadow-mode first; the gateway needs its
   own PII-tuned Laya instance (the one at :8010 is the harness's router).
2. **Security-scan corpus decision.** 714k pre-redaction rows are held from retention
   (`GATEWAY_SECURITY_RETENTION_DAYS=0`): label for the guard-model fine-tune, export a
   slice, or let them age out. They are simultaneously the training asset and the largest
   data liability.
3. **Multi-process hard budgets.** Reservations are per-process; several workers can each
   admit up to the remaining budget once. Move reservations to the shared-state layer
   (Redis path exists for rate/slots).
4. **Operator legibility** (readiness §5): "needs attention" triage strip; store routing
   reason / fallback path / weighted tokens on `audit_log`; link PII and scan findings to
   request detail.
5. **Operability gaps** (readiness §4): `/livez`–`/readyz` split, graceful stream drain,
   trusted-proxy support, scan-drop metric.
6. **Known capacity-layer debts** (from the 2026-10-08 review, unfixed): Redis lease GC
   backstop (slot leak if Redis is enabled), Redis-down waiters misreported as capacity
   503s, health loop resetting breakers for endpoints whose inference fails, fan-out
   bypassing per-key concurrency, reservation TTL vs >10-minute streams.
7. **Hybrid mode (external fallback):** blocked on an egress allowlist; key/environment
   scoping already applies to the endpoint used.

## Not in scope (still)

Multi-region, autoscaling, fine-tuning orchestration, agent frameworks. The gateway stays
a policy-and-observability layer; applications live on top of it.
