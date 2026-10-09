# DevMesh Gateway — working rules for Claude sessions

## The decision log is the law of this repo

`docs/DECISIONS.md` (D-001…) records every design choice, bug fix, and reversal, with
the problem, what didn't work, the fix, and the proving test.

**Before changing any behavior:** search DECISIONS.md for entries touching the same
area and read them. Much of this codebase looks improvable until you learn why it is
the way it is — the log exists because dead ends were already explored.

**With every change that fixes a bug, makes a design choice, or reverses one:**
1. Add a DECISIONS.md entry (next free D-number; match the existing entry format:
   Status with date and commit, Problem, Fix, What works now naming the test).
2. Reference the relevant D-numbers in the commit message and in code comments where
   the "why" isn't obvious.
3. Add a regression test that fails on the old code; name it in the entry.

If a change contradicts an existing decision, say so explicitly in the new entry
("Amends D-0xx") rather than silently diverging.

## Design rules that decide arguments (details in docs/ARCHITECTURE_ROADMAP.md)

1. **Never silently degrade.** Pass through what we don't police verbatim
   (`format`, `options`, `think`, `keep_alive`); reject loudly what we do —
   real 4xx with the actual reason, never a clamp, never an invented default.
2. **Normalize what you police, pass through what you don't.**
3. **The audit trail is the forensic instrument** — full request/response shape,
   denials included; PII hashed with provenance, never raw.
4. **Symptoms are real, diagnoses are suspect.** Verify a reported mechanism against
   the code and the audit DB before accepting or shipping a theory.
5. **Capability is discovered, not assumed** — probe runtimes, don't hardcode model
   names or abilities (operator rule: model names change too fast).

## Practical notes

- Readiness claims live in `docs/CLIENT_DEPLOYMENT_READINESS.md`; keep them matched
  to tests, and narrow a claim rather than overstate it.
- Tests must not depend on the operator's local `.env` or on gitignored config;
  conftest isolates these — keep it that way.
- `config/gateway.yaml` and `.env` are local operator config (gitignored). Runtime
  knobs (routing, PII scrubbing, budget tiers) belong in `runtime_settings` via the
  dashboard, not in new env vars, when an operator will want to change them live.
- Run `ruff check --fix`, `ruff format`, and the full pytest suite before committing;
  CI runs Python 3.11–3.13, Windows, and real PostgreSQL/Redis.
