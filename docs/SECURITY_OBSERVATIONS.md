# Security Analyzer Observations

_Refreshed 2026-10-09. The February 2026 version of this file made claims that have been
false for months (alerts were in-memory only; embedding responses were being scanned); its
still-valid field notes are kept below, dated. Current behavior is authoritative in
[DECISIONS.md](DECISIONS.md) (D-038, D-041, D-042, D-048, D-050) and the code._

## Current state of the pipeline (2026-10)

- **Persistence:** every scan's verdicts and metadata (regex level, matches, guard fields,
  disagreement flag) are stored in `security_scans`. Alerts are no longer memory-only.
  Scan writes are direct (not via the audit intent log) and are lost on DB contention —
  a known gap.
- **Message capture is opt-in and always redacted** (`GATEWAY_SECURITY_STORE_MESSAGES`:
  `none` default / `flagged` / `all`). There is no raw-storage option anymore (D-041).
  This deployment runs `all` to keep collecting guard-training data.
- **Retention** (`GATEWAY_SECURITY_RETENTION_DAYS`, default 90, runs at startup, deletes
  labeled rows too) is set to `0` (keep) here: the pre-redaction backlog — ~714k rows,
  2026-03→09, zero labeled — is held pending the label/export/age-out decision. Those
  rows contain raw PII; nothing backfills redaction.
- **PII events** store a SHA-256 of the matched value with exact provenance
  (message/part/offsets, D-048). Note: unsalted hashes of small-keyspace values (SSNs,
  IPv4) are brute-forceable; a keyed HMAC is the open fix.
- **Scan scope:** inputs are scanned for all tasks; embedding *responses* are not (the
  Feb 2026 false-positive fix, implemented long ago). The regex detector remains 5 types
  (email, US phone, SSN, card, IPv4) — the D-052 proposal (ML gate + extractor) is the
  path past that.
- **Guard model** (shadow, Granite/Llama-Guard style) remains disabled in this deployment;
  the labeling UI and Llama-Guard-format export still work and drop placeholder rows.

## Field notes (dated, kept for the record)

- **2026-02:** `qwen3-embedding` output tokens (`[system]`) tripped the delimiter-attack
  regex — fixed by not scanning embedding responses; inputs still scanned (poisoned
  retrieval text is a real vector).
- **2026-02:** true-positive "ignore previous instructions" injection correctly flagged;
  non-blocking by design.
- **2026-08:** the audit trail settled a model-obedience dispute in one query: 15 of 17
  models honored `think:false`; both gpt-oss models disobeyed (20B at 40%, 120B at 27%
  plus 18 silent discards under grammar constraints). Pattern worth keeping: store enough
  request/response shape that "did the model actually do X" is a lookup, not a rerun.
- **2026-10:** external reports repeatedly identified real defects with wrong mechanisms
  (routing "broken" = fallback masking a corrupt model blob; "schema enforcement skipped"
  = token starvation). Verify mechanisms against the audit DB before acting.
