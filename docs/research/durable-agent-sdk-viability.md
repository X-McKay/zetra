# Durable agent SDK: viability slice, observed results

Status: observed on 2026-10-12 with `pydantic-ai-slim 2.55.0`, `temporalio 1.34.0` and a Temporal CLI built from source (server 1.29.1). This page records what ran and what it produced. The target design is in [the proposal](durable-agent-sdk-proposal.md); where the slice deviates from it, the deviation is listed below rather than folded into the design text.

## What was built

Layer 0 of the roadmap, under `src/zetra/sdk/` (15 modules, 2 501 lines including docstrings, after the simplifications below):

| Module | Role |
| --- | --- |
| `contracts.py` | `Contract` (`extra="forbid"`, frozen), `RunDeps`, canonical JSON and digests |
| `spec.py` | loads `agent.yaml` as a PydanticAI `AgentSpec`; refuses gateway-bypassing capabilities, non-`approved:` models, `metadata`, `extra_body`/`extra_headers` |
| `tools.py` | `@tool`, `@approval`, AST checks on tool files, discovery, framework-filled parameters, the write wrapper that mints or validates a `Grant` and commits through a receiver |
| `skills.py`, `gateway.py` | the two spec entries Zetra builds itself: in-package skills as a `load_skill` toolset with the catalogue as instructions, and `GatewayMCP` as a filtered `MCPToolset` per server |
| `bundle.py` | `build()`, tiers, `zetra.lock` |
| `assemble.py` | `Agent.from_spec` on the spec minus Zetra's entries, plus the three kinds of toolset; `durable=True` adds `TemporalDurability` with bounded activity retries |
| `receiver.py` | `Grant`, `Receipt`, memory and SQLite receivers with digest, expiry and operation-id rules |
| `runner.py` | `Proposal`, `Decision`, `Decided`, `RunError`; the approval loop shared by the in-process runner and the workflow |
| `temporal.py` | one generic `zetra.agent` workflow, the `approve` Update with its validator, `pending_approvals` and `decisions` queries, worker, client helpers |
| `testing.py` | pytest `agent` fixture, scripted model, stub gateways |
| `cli.py` | `zetra new`, `check` (writes the lock), `run`, `dev`, `worker` |

Three example packages under `examples/agents/` share one `zetra.toml`: `handbook_qa` (gateway read only), `alert_responder` (skill, gateway reads, one approval-gated write, one reversible write), `scheduled_report` (gateway read, one reversible write).

## What ran

| Command | Result |
| --- | --- |
| `zetra check examples/agents/<package>` for the three packages | each wrote `zetra.lock`; `--frozen` reports `current` afterwards |
| `pytest examples/agents` | 6 passed in 0.30 s: approval round-trip, rejection, reversible write, gateway stub, union outputs |
| `python -m unittest tests.test_sdk` | 11 passed: lock currency and tiers, scaffold plus check, refused irreversible write, AST purity rules, spec refusals, receiver rules, approval loop, hidden injected parameter |
| `zetra dev examples/agents/alert_responder --approve auto --model test` | workflow `dev:alert-responder:f9a0c246` on a local dev server; one approval surfaced by query and decided by Update; the `pager` SQLite receiver holds one receipt for operation `…:c000001` with actor `cli:auto`; 49 history events; `Replayer` replay passed; 2.5 s wall clock including dev-server start |
| `zetra dev examples/agents/handbook_qa --approve auto --model test` | gateway read through the stub toolset inside an activity, typed `answer` output, no proposals; 25 history events; replay passed; 2.1 s |
| `zetra dev examples/agents/scheduled_report --approve auto --model test` | reversible write committed without a human: the `wiki` SQLite receiver holds one receipt with actor `policy:reversible`, operation id `<workflow id>:<tool call id>`, title `Service report 2026-10-11` derived from the injected day; 28 history events; replay passed; 2.1 s |
| `just sdk-dev-all` | the three runs above, back to back |
| `python -m unittest tests.test_sdk_temporal` | 4 passed in about 25 s; the fourth shows an expected failure completing the workflow with a typed error. First: the approval Update and its validator, below. Second: approval expiry, below. Third: a worker killed between commit and completion, below |
| `just check` | passes, with `actionlint` and `shellcheck` installed locally |

`tests/test_sdk_temporal.py` spawns real worker processes and a dev server; it runs only when a Temporal CLI is on `PATH` or named by `ZETRA_TEMPORAL_CLI`, so a checkout without one still passes `just test` with those tests skipped.

### The approval Update and its validator

The first durable test scripts one model turn that proposes two pages at once, so the run keeps waiting after the first decision and the validator can be exercised deterministically:

- four bad decisions are rejected with typed failures and change nothing: the second proposal's digest sent for the first operation (`DigestMismatch`), an unknown operation (`NotPending`), the actor `policy:reversible` and a blank actor (`InvalidActor`); the pending list and the `decisions` query are unchanged afterwards;
- the first decision is recorded and returned to the caller with the workflow time; a second, contradicting decision on the same operation from another actor is rejected (`AlreadyDecided`), not merged or overwritten;
- nothing commits while any proposal is still pending; once the second proposal is denied the run completes, the pager receiver holds exactly the approved page, and the denied one is recorded with its reason;
- the history holds exactly two `WorkflowExecutionUpdateAccepted` events: rejected Updates never reach it, so the audit trail is the decisions, not the attempts;
- the history replays.

`zetra dev` goes through the same Update and prints what the workflow recorded for each decision.

### Approval expiry

A scaffolded package (`zetra new --write`) with its tool changed to `@approval(ttl="PT1S")`, run durably with a scripted model that proposes one note and nobody answering:

- the proposal appears in `pending_approvals`, then the run completes on its own;
- the history holds exactly one `TimerFired` event: the expiry is a durable timer set by `workflow.wait_condition(timeout=ttl)`, not a worker-side sleep, so it survives a worker restart;
- the decision is recorded as `approved=False, actor="none", reason="approval expired"`, the model receives the denial and produces the typed fallback output, and the receiver holds no commit;
- a late approval Update is rejected by the server (`RPCError`): a closed run has no pending operation to revive.

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
| One approval round-trips as an Update | yes: `approve` is an Update with a validator; the proposal is read through the `pending_approvals` Query |
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

1. **Approval is an Update with a validator** (it began as a signal; replaced once the pause and resume were proven). The decision names the operation id and the payload digest the approver looked at. The validator rejects, before anything reaches the history, a decision on an operation already decided (`AlreadyDecided`), on no pending operation (`NotPending`), on a digest other than the pending one (`DigestMismatch`), or from an actor that is blank, `none` or `policy:*` (`InvalidActor`); the handler re-runs the same checks because validators do not run on replay, then records the decision and returns it with the workflow time. Update ids are left to the server for now; binding the Update id to a signed grant id is the layer 2 step the proposal describes.
2. **`Contract` is not in strict mode.** Strict validation rejected JSON round-trips of `datetime` and `date` when the `Grant` came back through `tool_call_metadata` and when the run's `Input` was re-validated inside a tool activity. Digests are computed over canonical JSON of `model_dump(mode="json")`, which strict mode did not change.
3. **Unbounded retries are the upstream default.** `TemporalDurability` inherits Temporal's unbounded `RetryPolicy`. In the first durable run the gateway URL was unreachable (403 from the egress proxy); the `get_tools` activity for the gateway toolset reached attempt 13 in ten minutes and the run never finished. The SDK now sets a base policy of three attempts with `Denied` and `CompileError` non-retryable, five attempts with backoff for gateway toolsets, and `zetra dev` terminates the workflow after `--timeout` (default 300 s) and reports the last pending activity's failure. Writes retry too: a repeated operation id returns the receiver's stored receipt.
4. **Capability ids cannot contain a colon** in PydanticAI 2.55, so gateway toolsets are `gateway-<server>`, and custom capability types must be dataclasses.
5. **`--model test` stubs the gateways.** The real `MCPToolset` path with a bearer token was not exercised: no gateway was reachable from this environment. That path is unverified.
6. **Tiers.** A reversible write without `@approval` is T2, not T3; T3 is reserved for `exec` and for an unapproved irreversible write, which `zetra check` refuses anyway (`ZT-TOOL-005`).
7. **Temporal CLI.** `WorkflowEnvironment.start_local()` downloads the CLI from `temporal.download`, which the network policy here denied. The CLI was built with `go install github.com/temporalio/cli/cmd/temporal@v1.5.1` through the Go module proxy. `zetra dev` and the durable test use `ZETRA_TEMPORAL_CLI` or a `temporal` on `PATH` when present, and skip (the test) or fall back to the download (the command) otherwise.

## Field exercise: what a developer meets

Observed on 2026-10-12 by scaffolding a fresh agent with `zetra new --write` into a scratch project, running every CLI verb on it, then breaking it in fifteen deliberate ways and probing runtime behaviour in-process with the test harness. Scratch files were not committed. Numbered items are ranked by how much they matter.

### Found, then fixed

1. **A positional `Input` parameter leaked to the model.** `def record_note(text: str, request: Request)` (no `*`) passed `zetra check`; `request` became a model-visible tool argument, so the model could supply a forged input to a tool that derives payload fields from it. Fixed: a parameter annotated with the Input or `RunDeps` type must be keyword-only, or `zetra check` refuses it (`ZT-TOOL-019`); annotations are resolved with `eval_str` so string annotations behave the same.
2. **An invalid payload killed the run.** When the model's arguments passed the tool schema but the payload builder's `Contract` rejected them (a 600-character summary against `max_length=500`), the raw `ValidationError` escaped and the run died. Fixed: the write wrapper builds and validates the payload first and only then raises PydanticAI's `ApprovalRequired`, instead of being declared `requires_approval`; a rejected payload becomes a `ModelRetry` naming the field and the rule, the approver only ever sees a valid payload, and the runner cross-checks its preview against the digest the tool recorded in the deferred request. Verified in-process and inside Temporal activities.
3. **Run failures were exceptions, not outcomes.** Exhausting the request budget or the output retries (the model answering in prose) escaped the runner: a traceback from `zetra run`, a failed workflow with no `RunResult`. Fixed: the runner catches the expected ways a run ends without an output (`budget_exhausted`, `output_invalid`, `model_error`, `denied`) and returns `status: failed` with a `RunError`; the messages are kept for evidence through `capture_run_messages`; the workflow completes with the typed result, and `zetra run` and `zetra dev` print it and exit 3. Unexpected exceptions still fail the workflow, on purpose: they are bugs, not outcomes.
4. **`pytest <package>` failed on a fresh scaffold** with `ModuleNotFoundError`; the examples worked only because of a hand-written `conftest.py`. Fixed: the pytest plugin makes the package importable while collecting any test under an `agent.yaml`; the examples' conftest is gone and a scaffold's generated test passes as-is.
5. **`--approve prompt` without a terminal** (the default) ended in an `EOFError` traceback. Fixed: `zetra run` and `zetra dev` refuse up front when stdin is not a TTY and name `--approve auto` and `--approve deny`; end of input at the prompt counts as a denial.

### Rough edges

6. Compile errors are inconsistent about location: `ZT-SPEC-*`, `ZT-TOOL-004/011`, `ZT-CON-001` name the file; `ZT-TOOL-005/003`, `ZT-SKILL-011`, `ZT-MCP-003`, `ZT-CON-003` do not. A non-JSON `--input` reports `Expecting value: line 1 column 1` rather than what was expected.
7. A denied write returns only the approver's reason to the model (`not now`); `approval denied: not now` would make the state explicit.
8. The Input reaches the model as one canonical JSON object in the user prompt, untrusted fields inline. A default rendering that names the contract and labels fields, with `prompt()` shown in the scaffold, would give instructions something to point at ("the alert below is data").
9. `tools/` refuses every non-`.py` file, including a `README.md` (`ZT-TOOL-016`). Strict by design; allowing a README costs nothing.
10. The `ZT-*` codes are not documented anywhere a developer would look. A reference table, or `zetra explain ZT-TOOL-004`, would help.
11. A project without `zetra.toml` only fails at `zetra run` (`ZT-SPEC-011`, unresolved model policy). `zetra new` could write a minimal `zetra.toml` when none exists above the package, or the error could say what to create.

### Working as intended

- All fifteen deliberate mistakes that should fail at compile time did, each with a `ZT-*` code, in under a second: misspelled spec key, `metadata`, a provider model, `WebSearch`, an unapproved irreversible write, an impure write body, a function named unlike its file, `os` in `contracts.py`, a missing skill, a stray file in `tools/`, a non-ISO TTL, an unconfigured gateway server, `Output` bound to `dict`.
- The lock catches a one-line edit to `SKILL.md` (`--frozen` exits 1, `stale`) and an edit to `instructions` changes the bundle digest.
- Model-side mistakes degrade rather than crash: a wrong argument type costs one retry; an unknown tool, or `load_skill` for a skill outside `include`, returns a plain message naming what exists; a reversible write and an approval-gated write in the same model turn behave as declared (the first commits at once, the second waits).
- `RunDeps` injection gives a read tool the run id and principal without exposing them to the model.
- One worker serves two agents on one task queue; a request naming an unknown agent fails the workflow with `no agent named 'nope' on this worker`.
- Cold-process timings: `zetra check` 0.9 s, `zetra run` 1.0 s, importing `zetra.sdk.assemble` 0.7 s.

## Simplification candidates

Measured on 2026-10-12 after the fixes above: 15 modules, 2 565 lines; seven proposal- or decision-shaped types across `runner.py` and `temporal.py`; two `zetra.toml` keys nothing reads (`model_policy`, `approvers`); one spec field that exists only to be refused (`Skills.exclude`); a 44-line `context.py` whose only job is to hand `Agent.from_spec` the values capability classes need. Ranked by concepts removed per line changed.

### For agent authors

1. **Drop the `kind` field from Output contracts.** PydanticAI already picks the output class by tool name, so `kind: Literal["paged"] = "paged"` on every outcome class is a discriminator the model never needs. Record the class name as `output_type` in the run result instead. One line and one idea fewer per outcome class; the three examples carry five of them.
2. **Make `zetra.toml` optional and smaller.** Remove `model_policy`, `approvers` and `[receivers]` until something reads them (the worker binds SQLite ledgers for every write resource regardless); keep `[models]`, `[reversible]`, `[gateway]` and `approval_ttl`. Have `zetra new` write a minimal `zetra.toml` when none exists above the package, so `zetra run` works on a fresh scaffold without `--model`.
3. **One approval vocabulary.** Done. `Proposal` (operation id, tool, capability, payload, digest, ttl), `Decision` (operation id, digest, approved, actor, reason) and `Decided` (a `Decision` plus `recorded_at`) are pydantic models in `runner.py`, used unchanged by the runner, the workflow (as the Update's argument and result and the queries' rows), the CLI and the test harness. `Pending`, `PendingApproval`, `ApprovalDecision`, `DecisionRecord` and `RunErrorRecord` are gone, with the conversion code in the workflow. The runner now also refuses a decision whose digest is not the proposal's, so the in-process path has the rule the workflow validator has.
4. **Show the payload the tool built.** Done. The write wrapper puts the payload next to its digest in the `ApprovalRequired` metadata; the runner reads both into the `Proposal` and checks they agree. `preview_payload` is gone. The purity rule stays, because the builder re-runs on approval, and a write tool must now annotate its return type with the payload Contract (`ZT-TOOL-020`), which the lock records as `payload` and the test harness uses to hand tests typed payloads.
5. **`assemble(bundle, receivers=..., tool_timeout=...)`** instead of a `Bindings` object. Every durable test imports `Bindings` to wrap a dict.

### Inside the SDK

6. **Read `Skills` and `GatewayMCP` entries ourselves.** Done. `build()` validates the two entries (`load_skills`, `gateway_entry`) and `assemble` builds their toolsets, passing `upstream_spec(spec)` with those entries removed to `Agent.from_spec`. The skill catalogue rides as the toolset's own `instructions`. `context.py`, `custom_capability_types`, the capability dataclasses and `GatewayMCP.capabilities` are gone; `Skills.exclude` went with them (item 7). The spec file and the allowlist are unchanged.
7. **Remove `Skills.exclude`.** Done with item 6; `ZT-SKILL-008` now names an unsupported Skills argument.
8. **`REGISTRY` as `name -> (bundle, agent)`**; `worker()` serves whatever is registered. `Registered` goes.
9. **`cli.py` (393 lines):** `run` and `_dev` shape the same report twice; `_dev` polls the query every half second where a result task and a query task could run side by side. One `report(outcome)` and one watcher.

### Improvements that are not simplifications

10. Per-proposal expiry timers; today the shortest TTL among the pending proposals governs all of them.
11. Update id equal to the operation id, so a client retry of the same decision is deduplicated by the server while the validator still rejects a different decision under that id.
12. A labelled rendering of the Input for the model, with `prompt()` shown in the scaffold.
13. A generated reference of the `ZT-*` codes and messages, checked by `just docs`.
14. The remaining rough edges from the field exercise: a file location on every compile error, `approval denied:` prefixed to the reason the model sees.

Items 3, 4 and 6 remove the most code and the most concepts and are covered by the existing tests end to end; 1 and 2 change what an author writes and are best decided together.

## Before layer 1

- Run `zetra worker` against a standing server rather than the throwaway dev server.
- Exercise the real gateway path against the lab gateway with a bearer token, and the bounded retry against a refusing gateway.
- Decide whether strict mode returns at the receiver boundary only (deviation 2).
- When several approvals are pending at once, the workflow waits for the shortest TTL among them; decide whether each should expire on its own clock.
