Review the codebase (or specified files) using the Universal Code Review Rubric.

## Process

1. **Classify risk tier** for each changed/reviewed area:
   - Tier 0: docs, comments, formatting, tests only
   - Tier 1: feature work behind existing interfaces, internal logic
   - Tier 2: auth/authz, networking, persistence, concurrency, error-handling changes
   - Tier 3: security controls, crypto, secrets, policy/routing/enforcement, remote execution

2. **Score each gate (0-2)**:
   - Gate 1: Intent & Scope — purpose explicit, no hidden scope
   - Gate 2: Correctness & Contracts — input validation, output guarantees, edge cases
   - Gate 3: Failure Semantics — timeouts, retries, fail-open/closed intentional, errors propagated
   - Gate 4: Security & Abuse Resistance — injection, secrets, SSRF, deserialization
   - Gate 5: Data Integrity & State — idempotency, races, transactions
   - Gate 6: Concurrency & Performance — deadlocks, blocking in async, memory growth
   - Gate 7: Observability & Operability — traceability, structured logs, redaction
   - Gate 8: Tests & Evidence — tier-appropriate test coverage
   - Gate 9: Maintainability — readability, module boundaries, dependency hygiene

3. **Cross-reference against project docs**:
   - `rule.md` — AI system design principles (secure by design, SOLID, DRY, YAGNI, KISS)
   - `gatewayprd.md` — PRD requirements (v0 features, API design, routing, policy)
   - `COMPLIANCE_AUDIT.md` — previous audit findings and gaps

4. **LLM/Agent module checks** (if applicable):
   - Model output treated as untrusted input
   - Tool calls validated + authorized
   - Prompt injection resistance
   - Logging redaction for sensitive content

5. **Produce structured output**:

```
## Risk Tier: [0-3]

## Gate Scores
| Gate | Score | Notes |
|------|-------|-------|

## Blockers (must fix)
- Location: file:line
- Impact: what breaks
- Recommendation: fix approach

## High Risk (should fix)
...

## Medium
...

## Nice-to-have
...

## Questions / Assumptions
...

## Merge Rules Check
- Any blocker: [pass/fail]
- Tier 2+ score >= 14/18: [score/18]
- Tier 3 Security + Failure gates = 2: [pass/fail]
```

Do not focus on style. Focus on system risk, correctness, security, failure modes, and state integrity.
