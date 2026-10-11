# Temporal Agent Durability Standard

## Boundary

Workflow code may coordinate deterministic state, schedule activities, wait on timers or signals, expose queries, and decide transitions from recorded inputs. Activities perform all model calls, PydanticAI runs, tool calls, database and network access, file I/O, randomness, and other nondeterministic operations.

## Activity Design

- Use coarse enough activities to avoid excessive history, but narrow enough for meaningful retry and observability.
- Pass versioned, bounded Pydantic payloads.
- Set start-to-close timeouts for bounded work and heartbeat timeouts for long-running work.
- Heartbeat useful progress and cancellation state.
- Return artifact references instead of large transcripts or binaries.
- Map provider failures into stable application failure types.

## Retry Matrix

| Failure | Default handling |
| --- | --- |
| Rate limit or transient network | Backoff and retry within budget |
| Provider timeout before side effect | Retry within budget |
| Invalid input or schema | Terminal; repair upstream |
| Authentication or permission | Terminal; alert owner |
| Policy denial | Terminal or explicit human path |
| Unknown write outcome | Reconcile by idempotency key before retry |
| Model quality failure | Constrained repair or fallback, then stop |

Set maximum attempts, elapsed retry time, and cost limits. Temporal retries and provider or SDK retries must share one budget.

## Human Approval

Persist an approval request identifier, requested action, actor requirements, expiry, and immutable audit facts. Wait for a named signal. Validate the actor and signal payload in an activity or deterministic policy layer. Model approved, rejected, expired, and cancelled states explicitly.

## Evolution

- Keep workflow identifiers stable for business processes.
- Version payload schemas compatibly.
- Use patches or versioning for nondeterministic workflow changes.
- Run replay tests against representative production histories before deployment.
- Use `continue_as_new` for unbounded loops or histories approaching operational limits.

## Required Tests

- Workflow transition unit tests.
- Time-skipping tests for timers and expiry.
- Activity retry and terminal failure tests.
- Duplicate-delivery and idempotency tests.
- Worker termination and resumption.
- Approval authorization and timeout.
- Replay tests for changed workflow code.
- Trace-context propagation.
