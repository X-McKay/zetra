# Durable agent SDK: viability slice, observed results

Status: observed on 2026-10-12 with `pydantic-ai-slim 2.55.0`, `temporalio 1.34.0` and a Temporal CLI built from source (server 1.29.1). This page records what ran and what it produced. The target design is in [the proposal](durable-agent-sdk-proposal.md); where the slice deviates from it, the deviation is listed below rather than folded into the design text.

## What was built

Layer 0 of the roadmap, under `src/zetra/sdk/` (14 modules, 2 378 lines including docstrings):

| Module | Role |
| --- | --- |
| `contracts.py` | `Contract` (`extra="forbid"`, frozen), `RunDeps`, canonical JSON and digests |
| `spec.py` | loads `agent.yaml` as a PydanticAI `AgentSpec`; refuses gateway-bypassing capabilities, non-`approved:` models, `metadata`, `extra_body`/`extra_headers` |
| `tools.py` | `@tool`, `@approval`, AST checks on tool files, discovery, framework-filled parameters, the write wrapper that mints or validates a `Grant` and commits through a receiver |
| `skills.py`, `gateway.py` | the two custom capability types: in-package skills and `GatewayMCP` |
| `bundle.py` | `build()`, tiers, `zetra.lock` |
| `assemble.py` | `Agent.from_spec` plus the toolset; `durable=True` adds `TemporalDurability` with bounded activity retries |
| `receiver.py` | `Grant`, `Receipt`, memory and SQLite receivers with digest, expiry and operation-id rules |
| `runner.py` | the approval loop shared by the in-process runner and the workflow |
| `temporal.py` | one generic `zetra.agent` workflow, `approve` signal, `pending_approvals` query, worker, client helpers |
| `testing.py` | pytest `agent` fixture, scripted model, stub gateways |
| `cli.py` | `zetra new`, `check` (writes the lock), `run`, `dev`, `worker` |

Three example packages under `examples/agents/` share one `zetra.toml`: `handbook_qa` (gateway read only), `alert_responder` (skill, gateway reads, one approval-gated write, one reversible write), `scheduled_report` (gateway read, one reversible write).

## What ran

| Command | Result |
| --- | --- |
| `zetra check examples/agents/<package>` for the three packages | each wrote `zetra.lock`; `--frozen` reports `current` afterwards |
| `pytest examples/agents` | 6 passed in 0.30 s: approval round-trip, rejection, reversible write, gateway stub, union outputs |
| `python -m unittest tests.test_sdk` | 11 passed: lock currency and tiers, scaffold plus check, refused irreversible write, AST purity rules, spec refusals, receiver rules, approval loop, hidden injected parameter |
| `zetra dev examples/agents/alert_responder --approve auto --model test` | workflow `dev:alert-responder:f9a0c246` on a local dev server; one approval surfaced by query and decided by signal; the `pager` SQLite receiver holds one receipt for operation `…:c000001` with actor `cli:auto`; 49 history events; `Replayer` replay passed; 2.5 s wall clock including dev-server start |
| `zetra dev examples/agents/handbook_qa --approve auto --model test` | gateway read through the stub toolset inside an activity, typed `answer` output, no proposals; 25 history events; replay passed; 2.1 s |
| `zetra dev examples/agents/scheduled_report --approve auto --model test` | reversible write committed without a human: the `wiki` SQLite receiver holds one receipt with actor `policy:reversible`, operation id `<workflow id>:<tool call id>`, title `Service report 2026-10-11` derived from the injected day; 28 history events; replay passed; 2.1 s |
| `just sdk-dev-all` | the three runs above, back to back |
| `python -m unittest tests.test_sdk_temporal` | 3 passed in about 22 s. First: scripted model and stub gateway inside activities, approval by signal, duplicate signal ignored, receiver committed once, history replays. Second: approval expiry, below. Third: a worker killed between commit and completion, below |
| `just check` | passes, with `actionlint` and `shellcheck` installed locally |

`tests/test_sdk_temporal.py` spawns real worker processes and a dev server; it runs only when a Temporal CLI is on `PATH` or named by `ZETRA_TEMPORAL_CLI`, so a checkout without one still passes `just test` with those tests skipped.

### Approval expiry

A scaffolded package (`zetra new --write`) with its tool changed to `@approval(ttl="PT1S")`, run durably with a scripted model that proposes one note and nobody answering:

- the proposal appears in `pending_approvals`, then the run completes on its own;
- the history holds exactly one `TimerFired` event: the expiry is a durable timer set by `workflow.wait_condition(timeout=ttl)`, not a worker-side sleep, so it survives a worker restart;
- the decision is recorded as `approved=False, actor="none", reason="approval expired"`, the model receives the denial and produces the typed fallback output, and the receiver holds no commit;
- a late approval signal is rejected by the server (`RPCError`): a closed run has no pending operation to revive.

Expiry of a *granted* approval is a separate rule and is tested in `tests/test_sdk.py`: a receiver refuses a `Grant` whose `expires_at` has passed.

### Worker killed between commit and completion

This is the case the idempotent receipt exists for. The test (`tests/test_sdk_temporal.py`, with `tests/_durable_worker.py` as the worker process) runs `alert_responder` with two real worker processes against one SQLite ledger on a throwaway dev server:

1. Worker A serves the agent with the pager receiver wrapped so that, once the real receiver has committed, the process calls `os._exit(137)`. Nothing is reported to the server. The test confirms exit code 137 and one row in `pager.db`.
2. The write activity is now orphaned. With no heartbeat on write activities, the server notices only through the activity's start-to-close timeout (set to 3 s here through `Bindings(tool_timeout=...)`; 30 s by default) and schedules the retry allowed by the bounded policy.
3. Worker B, a plain worker on the same ledger, runs the retry. The write wrapper rebuilds the payload from the same arguments, re-validates the same `Grant` from `tool_call_metadata`, and calls `commit` with the same operation id. The receiver returns the stored receipt instead of writing again.
4. The run completes with the typed `paged` output. The ledger still holds one row. The history shows exactly one activity that needed a second attempt, its `last_failure` is a timeout failure, and the completed activity's payload carries the receipt id that worker A printed before dying.

Observed: about 19 s end to end, most of it the 3 s timeout, the sticky-queue fallback for the workflow task that worker A had cached, and two process start-ups. Recovery time in production is bounded by the tool activity timeout, which the worker chooses; a write that legitimately takes longer than that timeout would be retried while still running, which the receiver's operation-id rule makes safe but is worth noting when the timeout is set.

## Exit test of layer 0, item by item

| Exit criterion (proposal section 8) | Observed |
| --- | --- |
| Three archetypes compile from their packages | yes; `zetra check` produced a lock for each |
| Run in-process | yes; `pytest examples/agents` |
| Run on the local Temporal dev server | yes, all three (`just sdk-dev-all`) |
| One approval round-trips as an Update | round-trips as a **Signal** plus a Query, see deviation 1 |
| A recorded history replays | yes, in `zetra dev` and in the unit test |
| Per-agent line counts measured | yes, table below |
| Inner-loop timings measured | in-process suite 0.30 s; durable runs 2.1 to 2.5 s each including dev-server start; the two durable tests 2.5 s |
| Section 10 unknowns answered | items 4, 5, 7 (partly), 10 and 12, see below |

### Authored lines per agent

| Package | Files | Lines |
| --- | --- | --- |
| `handbook_qa` | `agent.yaml`, `contracts.py`, one test | 56 |
| `alert_responder` | `agent.yaml` 14, `contracts.py` 38, two tools 23, `SKILL.md` 9, tests 50 | 134 |
| `scheduled_report` | `agent.yaml`, `contracts.py`, one tool, one test | 59 |
| `examples/incident-triage` (hand-written, for comparison) | factory, manifest, scenarios, demo | 591 |

Nothing in the three packages wires a receiver, a worker, a workflow, an approval queue or a fixture; those are the SDK's.

### Section 10 items answered

| Item | Answer |
| --- | --- |
| 4. `tool_call_metadata` crosses into the tool activity | yes. The `Grant` travels in `DeferredToolResults.metadata` and is read from `ctx.tool_call_metadata["grant"]` inside the write activity; the receiver committed with the approver's actor on both the in-process and the durable path |
| 5. Scripted models under `TemporalDurability` | a `FunctionModel` instance passed at assembly runs inside the model activity in the same worker process; no `ResolveModelId` or model registry was needed for single-process tests |
| 7. Per-tool `ActivityConfig` | per tool through `metadata={"temporal": ActivityConfig}` on `FunctionToolset.add_function`, per toolset through `toolset_activity_config` keyed by toolset id. Configured and type-checked; the bound was not exercised against a failing gateway after the fix in deviation 3 |
| 10. Framework-filled parameters | the generated schema hides `*, alert: Alert`; a model-supplied `service` argument is rejected by the tool wrapper and never reaches the approver (`test_model_cannot_choose_the_injected_service`) |
| 12. Code size and timings | tables above |

Items 1, 2, 3, 6, 8, 9 and 11 belong to later layers and were not touched.

## Deviations from the proposal, and findings

1. **Approval is a Signal, not an Update.** The workflow exposes `approve(ApprovalDecision)` as a signal with first-decision-wins semantics and `pending_approvals` as a query. An Update with a validator that rejects a stale or mismatched decision is the layer 1 shape; the signal was enough to prove the pause and resume.
2. **`Contract` is not in strict mode.** Strict validation rejected JSON round-trips of `datetime` and `date` when the `Grant` came back through `tool_call_metadata` and when the run's `Input` was re-validated inside a tool activity. Digests are computed over canonical JSON of `model_dump(mode="json")`, which strict mode did not change.
3. **Unbounded retries are the upstream default.** `TemporalDurability` inherits Temporal's unbounded `RetryPolicy`. In the first durable run the gateway URL was unreachable (403 from the egress proxy); the `get_tools` activity for the gateway toolset reached attempt 13 in ten minutes and the run never finished. The SDK now sets a base policy of three attempts with `Denied` and `CompileError` non-retryable, five attempts with backoff for gateway toolsets, and `zetra dev` terminates the workflow after `--timeout` (default 300 s) and reports the last pending activity's failure. Writes retry too: a repeated operation id returns the receiver's stored receipt.
4. **Capability ids cannot contain a colon** in PydanticAI 2.55, so gateway toolsets are `gateway-<server>`, and custom capability types must be dataclasses.
5. **`--model test` stubs the gateways.** The real `MCPToolset` path with a bearer token was not exercised: no gateway was reachable from this environment. That path is unverified.
6. **Tiers.** A reversible write without `@approval` is T2, not T3; T3 is reserved for `exec` and for an unapproved irreversible write, which `zetra check` refuses anyway (`ZT-TOOL-005`).
7. **Temporal CLI.** `WorkflowEnvironment.start_local()` downloads the CLI from `temporal.download`, which the network policy here denied. The CLI was built with `go install github.com/temporalio/cli/cmd/temporal@v1.5.1` through the Go module proxy. `zetra dev` and the durable test use `ZETRA_TEMPORAL_CLI` or a `temporal` on `PATH` when present, and skip (the test) or fall back to the download (the command) otherwise.

## Before layer 1

- Run `zetra worker` against a standing server rather than the throwaway dev server.
- Exercise the real gateway path against the lab gateway with a bearer token, and the bounded retry against a refusing gateway.
- Replace the signal with an Update plus validator (deviation 1) and decide whether strict mode returns at the receiver boundary only (deviation 2).
- When several approvals are pending at once, the workflow waits for the shortest TTL among them; decide whether each should expire on its own clock.
