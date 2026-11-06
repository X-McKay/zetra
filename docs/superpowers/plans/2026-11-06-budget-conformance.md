# Exact T13/T13b budget qualification

Approved scope: exercise the production gateway/APS/Redis budget path with a dedicated
deterministic byte-token backend. This does not qualify Qwen token accounting. Existing
backend bounds and production rate policies remain unchanged. No cluster changes,
tags or releases are performed by this branch.

## Components and acceptance

1. Reproduce late settlement before an expiry sweep in both store implementations;
   expired reservations remain fully spent, including at the exact deadline.
2. Add reserved-token and successful per-decision settlement records without bearer,
   request-body, Redis-key or credential material.
3. Add disabled-by-default GET budget observation. TokenReview must authenticate the
   exact configured harness ServiceAccount for agent-policy-service; sessions must be
   owned by tenant-test and an explicitly configured fixture agent. Reads can perform
   the existing expiry sweep; no ledger reset or state mutation API is exposed.
4. Add a bounded OpenAI-compatible backend: model conformance-byte-v1, byte tokenizer,
   prompt <=200, completion <=100, aggregate <=300, one completion, strict input fields,
   bounded bodies and delays, computed usage. Test actual HTTP behavior and rejection
   of amplification and aliases. Build a static cross-platform image.
5. Add two signed deterministic fixtures: conformance-budget-day (2000/day) and
   conformance-budget-session (1000/session), with no orchestration model calls.
   Evidence contains actual HTTP statuses, trace IDs, usage, and timings, never tokens.
6. Add opt-in harness tests with exact baseline, exhaustion, concurrent reservation,
   settlement <=5 seconds, session independence, local usage tampering, generation
   restore, and gateway restart gates. Missing evidence, rate limiting, dirty baselines,
   timeouts, unreadable logs, and unperformed restart/restore are failures.
7. Separately inspect/reproduce gateway handling of production n>1. Document what is
   and is not established; the test backend's n=1 enforcement is not production proof.
8. Run APS/backend tests and vet, relevant runner suite/Ruff, agent repository checks,
   image override verification, and independent review before commits/PRs.

## Integration interfaces

- New backend llm-budget-conformance; only its catalogue maxModelLen is 300.
- Test route keeps strict JWT/agent/model checks, APS authorization, credential
  replacement and ordinary OTLP settlement. Test-only burst must admit ten concurrent
  attempts while retaining the 30/minute rate. Real routes remain unchanged.
- Observation env: APS_BUDGET_OBSERVER_SUBJECT and APS_BUDGET_OBSERVER_AGENTS.
- Parent owns applied GitOps, gateway restarts and signed fixture releases.

## Review focus

No synthetic success evidence; no first-call refusal counted as exhaustion; no retry
conceals 429; no guest-controlled ledger observations; restore preserves session key;
concurrent decisions all precede first successful response; late settlement never
refunds expiry; malformed/unknown backend fields cannot invalidate the 300 bound.

## Execution evidence

- Late-before-sweep regression failed on Redis at130s/131s, then both stores passed.
- Full APS Go tests and vet passed after observer/logging/expiry changes.
- Backend authentication, byte usage, amplification and startup-key tests passed;
  configured control-byte key regression failed before validation was added.
- Runner full scoped suite:310passed; focused budget evaluator/history/watcher:28passed.
- Agent repository justcheck passed; both installed fixture image guards and normal
  unprivileged/network-none CI factory checks passed. ARM64 backend image inspected.
- Ruling: use a read-only gateway watcher plus an external authorized operator, not
  harness Deployment writes. Both exact old pod UIDs must disappear within the fixed
  pause; absent or late replacement fails. Parent approved this interface.
- Ruling: backend fixed bounded5s holds avoid a new privileged control endpoint.
  Every concurrency decision must predate the first response; slow arrivals fail.
- Ruling: cluster UTC timestamps establish settlement<=5s; negative or late intervals
  fail, and the response timestamp precedes writing for a conservative bound.

- Rebased onto main89034e2, preserving T4/T5/admission/T25/revocation adapters.
- Independent review fixes: all started sessions clean up even if one cleanup fails
  or is cancelled; extra backend responses and off-route APS decisions for selected
  sessions fail exact-evidence checks. All regressions failed before fixes.
- Tightened trusted sample ordering, concurrency-window coverage and monotonic spent;
  four regressions failed before fixes. Focused budget tests:36passed.
- Final integrated runner suite:453passed; Ruff clean. APS full Go tests/vet and
  complete agent repository justcheck passed again on rebased main.

Independent review complete: no remaining material findings; reviewer independently ran all36 budget evaluator/runtime tests. PR publication is next.
