# Proposal: a compiled, zero-trust agent SDK on PydanticAI v2 and Temporal

Status: **proposal for discussion**, drafted 2026-10-11. Nothing here is implemented or qualified. It describes a target developer experience and architecture for Zetra, grounded in the current v0.1 toolkit, the Playbooks, and a review of the current PydanticAI (v2.55.0, released 2026-10-09; the v2 line began 2026-06-23) and Temporal Python SDK (1.34.0 pinned) surfaces, checked against the upstream `main` docs and source because the rendered docs sites are blocked from this environment. Where this document and the code disagree, the code and its recorded evidence win, per `AGENTS.md`. The verified upstream facts and sources behind it are in [the research notes](durable-agent-sdk-research.md).

## 1. The problem in one paragraph

Today a Zetra agent package is a hand-written `factory.py` returning an object with `run(request: dict) -> dict`, a strict `agent.yaml` (v1alpha1), hand-written scenario tests mapped through `evals/scenarios.json`, and one coarse Temporal Activity (`zetra.run_agent`) that re-runs the whole agent on retry. Every new agent repeats the same request parsing, capability checks, approval plumbing, receiver wiring, test scaffolding and worker setup. Meanwhile PydanticAI v2 has grown exactly the primitives the Playbooks ask for: a declarative Agent Spec, composable **capabilities** with lifecycle hooks, Agent Skills loading, guardrails, spend limits, tool approval, and per-call Temporal activities. The opportunity is to stop writing agents as Python programs and start **compiling** them from four reviewed inputs, with Zetra owning the governance layer that PydanticAI deliberately leaves to the application.

## 2. What a developer writes (target DX)

Four inputs per agent, nothing else by default:

```text
agents/incident-triage/
├── agent.yaml          # Zetra manifest: identity, risk, capabilities, budgets + embedded PydanticAI AgentSpec
├── contracts.py        # Pydantic models: Input, Output, Deps (serializable IDs only), tool payloads
├── tools/              # @zetra.tool functions with declared effect/retry/scope, or tool.yaml for remote (MCP) tools
│   └── tickets.py
├── skills/             # Agent Skills (SKILL.md) libraries, validated against agentskills.io
│   └── checkout-runbook/SKILL.md
├── instructions/       # versioned prompt fragments referenced from agent.yaml
└── evals/              # authored scenario cases; required scenarios are generated from risk tier
```

### 2.1 `agent.yaml` (v1alpha2 sketch)

One file, two halves. `spec.agent` is a verbatim PydanticAI `AgentSpec` (validated with `AgentSpec.model_validate`, so upstream fields, templates and capability syntax work unchanged). `spec.zetra` is the governance the platform enforces and PydanticAI does not define.

```yaml
apiVersion: zetra.dev/v1alpha2
kind: Agent
metadata:
  name: incident-triage
  version: 0.3.0
  owner: service-operations@example.invalid
  businessFunction: service-operations
spec:
  agent:                                  # PydanticAI AgentSpec, passed through
    model: approved:balanced-v1           # logical model policy; resolved by a Zetra ResolveModelId capability
    instructions:
      - file: instructions/triage.md
    model_settings: {temperature: 0.1, max_tokens: 2000}
    retries: 2
    tool_timeout: 20
    end_strategy: graceful
    instrument: true
    capabilities:
      - Skills: {directories: skills, include: [checkout-runbook]}
      - Thinking: {effort: low}
  zetra:
    contracts: incident_triage.contracts    # module exposing Input, Output, Deps
    tools: [tickets.create_ticket, gateway.read_status]   # explicit allowlist; unlisted tools never load
    capabilities: [knowledge.read, mcp.read_status, ticket.write]
    dataClassification: internal
    autonomy: supervised
    execution: temporal                     # local | temporal | temporal-sandboxed
    riskTier: T2                            # computed floor; may only be raised here
    budgets: {maxModelRequests: 8, maxToolCalls: 12, maxSeconds: 300, maxCostUsd: 0.50}
    approvals:
      ticket.write: {mode: human, bindTo: [actionDigest, actor, tenant, release, operationId], ttlSeconds: 86400}
    guardrails: {input: [redact_secrets], toolResults: [prompt_injection_defender]}
    telemetry: {redact: [prompt_content]}
```

The v1alpha1 manifest keeps loading; the compiler upgrades it in place (`zetra migrate`). The existing `Manifest.parse` strictness (unknown keys fail, duplicate YAML keys fail) carries over to v1alpha2.

### 2.2 `contracts.py`

```python
from pydantic import BaseModel, Field
from zetra.sdk import ZetraDeps

class Incident(BaseModel):
    incident_id: str = Field(pattern=r"INC-[0-9]{4}-[0-9]{4}")
    service: Literal["checkout-api"]
    severity: Literal["warning", "critical"]
    summary: str = Field(max_length=500)

class TriageResult(BaseModel):
    status: Literal["ticket_created", "needs_review", "escalated"]
    ticket_id: str | None = None
    evidence: list[str]

class TriageDeps(ZetraDeps):          # ZetraDeps = tenant_id, actor_id, scopes, release_id, run_id, deadline
    runbook_key: str                  # only serializable identifiers; clients are rebuilt inside activities
```

`ZetraDeps` is the one base class every agent inherits. It exists because PydanticAI requires `deps` to be Pydantic-serializable to cross the activity boundary, and because the Playbooks require that deps never carry model-generated authority, secrets or live clients.

### 2.3 `tools/tickets.py`

```python
from zetra.sdk import tool, Receipt

@tool(
    capability="ticket.write",
    effect="write_consequential",       # read | write_reversible | write_consequential
    retry="idempotency_key_required",   # safe | idempotency_key_required | unsafe
    scopes=["tickets:write"],
    approval="human",                   # none | policy | human  (consequential writes may not be 'none')
    receiver="tickets",                 # named receiver adapter bound by the platform, never by the tool
    max_output_bytes=4096,
)
async def create_ticket(ctx: RunContext[TriageDeps], title: str, body: str) -> Receipt:
    """Create one investigation ticket. The receiver enforces approval, idempotency and tenant scope."""
    return await ctx.deps.receivers["tickets"].commit(
        operation_id=ctx.tool_call_id,          # stable across activity retries
        payload={"title": title, "body": body},
    )
```

The decorator does three things: it registers the function in a `ZetraToolset`, it writes the policy into PydanticAI tool metadata (so `SetToolMetadata`, `ToolGuardrail`, `ToolCallJudge` and `TemporalDurability` can read it), and it makes the declaration **statically checkable** by `zetra check` without importing the module (AST, as today). A tool whose declared `effect` is a write but whose `retry` is `safe` is a compile error, matching `agentctl`'s existing `TOOL007` rule.

### 2.4 What is *not* written per agent

No `factory.py`, no workflow class, no activities module, no worker `__main__`, no request/response dict parsing, no capability `require()` calls, no approval plumbing, no receiver wiring, no OpenTelemetry setup, no scenario-to-test JSON map. All of that is produced by the compiler and the shared runtime described next. A developer who needs custom coordination (multi-step plan, approve, commit) opts in by subclassing `ZetraWorkflow` (section 5.5); everyone else gets the generated one.

## 3. Build on PydanticAI v2, do not re-implement it

The review of the current docs changes the "framework-neutral runtime" assumption in `docs/developer.html`. PydanticAI v2 now ships most of the runtime mechanics the Playbooks require, so Zetra's leverage is in governance, compilation and evidence rather than in the agent loop. Reuse, verified against current upstream docs:

| Playbook need | PydanticAI v2 / Harness primitive | Zetra adds |
| --- | --- | --- |
| Declarative agent definition | `AgentSpec`, `Agent.from_spec` / `Agent.from_file`, capability spec syntax (`- Name: {kwargs}`), JSON Schema export | Governance envelope, strict manifest, capability allowlist, migration from v1alpha1 |
| Pluggable behavior with hooks | `AbstractCapability` with `before_/after_/wrap_/on_*_error` hooks for run, node, model request, tool validate/execute, output; `prepare_tools`; `@on_event`; `@durable_operation` | `ZetraPolicy` capability (section 5.2) |
| Skills | `pydantic_ai_harness.Skills(directories, include/exclude)`; SKILL.md exposed as deferred capabilities via `load_capability`; `references/`, `scripts/`, `assets/` are **not** read or executed; `allowed-tools` is experimental in the agentskills.io spec and ignored by Harness | Skill validation at build time (reuse `agentctl skills validate`, optionally `skills-ref`), per-file SHA-256 digests in the bundle as the Skills-over-MCP extension does, explicit allowlist, no script execution by policy |
| Tool allowlisting and shaping | `FunctionToolset`, `WrapperToolset`, `.filtered()`, `.prefixed()`, `.approval_required()`, `.with_metadata()`, `PrepareTools` | `ZetraToolset` wrapper that denies unlisted capability and emits audit events |
| Remote (MCP) tools | `MCPToolset(url, auth=<bearer|httpx Auth|'oauth'>, http_client=<httpx client for private CA / mTLS>, process_tool_call=..., tool_error_behavior=..., elicitation_handler=...)`; `.prefixed()`, `FilteredToolset`; each Temporal activity opens its own MCP session | `GatewayMCPToolset`: agentgateway-only endpoint, workload token provider, correlation `_meta` via `process_tool_call`, tool allowlist from the bundle, bounded responses, no redirects (today's `gateway.py` invariants) |
| Human approval | `requires_approval=True`, `ApprovalRequired`, `DeferredToolRequests`/`DeferredToolResults`, `ToolApproved(override_args)`/`ToolDenied`, `HandleDeferredToolCalls` inline handler, `ApprovalRequiredToolset` | Approval bound to canonical action digest + actor + tenant + release + operation; Temporal Update transport; receiver-side verification |
| Guardrails | `InputGuardrail`, `OutputGuardrail`, `ToolGuardrail` (`guard`, `result_guard`), `GuardrailResult.allow/block/replace/retry/approve`, detectors `redact_secrets`, `redact_personal_data`, `blocked_keywords`; `PromptInjectionDefender` (StackOne) | Tier-driven defaults, evidence that guardrails ran |
| Budgets | `UsageLimits(request_limit, tool_calls_limit, cost_limit, ...)` per run; `SpendLimits(Budget(usd, tokens, window, scope))` with `RedisSpendStore`, post-hoc accounting, durable-aware dedup | Manifest-derived limits; durable step counters in workflow state; reservation-based authority remains a target |
| Secondary review of tool calls | `ToolCallJudge(model, tools, on_uncertain='block'|'allow'|'ask')`; "a filter, not a security boundary" | Enabled by risk tier; never a substitute for receiver authorization |
| Durable execution | `TemporalDurability(activity_config, model_activity_config, toolset_activity_config, event_stream_handler, run_context_type, models)`, `PydanticAIWorkflow`, `PydanticAIPlugin` (Pydantic payload converter, sandbox passthroughs, activity registration, typed workflow-failure exceptions); per-tool `metadata={'temporal': ActivityConfig(...)}`; `TemporalAgent` deprecated, removed in v3 | Generated workflow, approval/stop signals, workflow-ID scheme, retry matrix, continue-as-new policy, replay tests |
| Observability | `Instrumentation(settings=InstrumentationSettings(...))`, GenAI semantic-convention spans | Correlation IDs (release/run/operation), redaction by data classification, join with gateway and Temporal spans |
| Deterministic testing | `TestModel`, `FunctionModel`, `agent.override(model=..., deps=..., toolsets=...)`, `models.ALLOW_MODEL_REQUESTS = False`, `capture_run_messages` | Generated scenario tests per required risk ID |
| Evals | `pydantic_evals` `Dataset`/`Case`/`Evaluator`, span-based and trajectory evaluators | Hard-gate evaluators for authorization, approval, idempotency; evidence binding |

Two facts from the upstream review constrain the design and are worth stating plainly:

- **Approval is not authorization.** PydanticAI's docs say so explicitly for deferred tools; `ToolCallJudge` says the same. Zetra's receiver and gateway remain the enforcement points. The SDK's job is to make the approval *provably bound* to the exact action and to make the receiver's verification unavoidable.
- **Harness is 0.x.** `pydantic-ai-harness` iterates fast with deprecation warnings; `pydantic-ai` core follows a stricter policy. Pin both in `uv.lock`, keep the Zetra capability stack behind a thin internal interface, and run the compatibility evals on every upgrade (section 8).

## 4. Architecture

```text
 developer inputs            compiler (zetra check / build)              runtime (shared, versioned)
 ─────────────────           ───────────────────────────────            ────────────────────────────
 agent.yaml        ──┐       1 parse + strict-validate manifest          ZetraAgentWorkflow (Temporal)
 contracts.py      ──┼──►    2 AST-scan tools, bind policy metadata  ──►   ├─ signals: stop | updates: approve
 tools/            ──┤       3 validate skills, compute digests             ├─ PydanticAI agent.run() → activities
 skills/           ──┘       4 derive risk tier, required scenarios        │    ├─ model request activity
                             5 assemble capability stack (ordered)         │    ├─ tool call activity → ZetraToolset
                             6 emit AgentBundle + check report             │    │     └─ receiver / gateway MCP
                                                                           │    └─ validate_args activity
                                                                           └─ continue_as_new, artifact refs
                             AgentBundle = {manifest, AgentSpec,          worker: PydanticAIPlugin, payload codec,
                               tool policy matrix, skill digests,          mTLS, task queue per agent/tier
                               capability stack, source digest}
```

### 4.1 Compiler contract

`zetra build <dir>` is deterministic and performs no I/O beyond reading the package. It produces an `AgentBundle` (JSON) whose digest becomes the `release_id` already used by evidence and the gateway correlation headers. The bundle records: manifest, resolved `AgentSpec`, tool policy matrix (name, capability, effect, retry safety, scopes, approval mode, timeout, output bound, activity config), skill names and digests, the ordered capability stack with its configuration, required scenario IDs, risk tier and reasons. `zetra check` is `build` plus static findings and never imports agent code, preserving today's guarantee.

The compile fails closed on: a tool declaring a capability absent from `spec.zetra.capabilities`; a consequential write without `approval: human|policy` or without a named receiver; `retry: safe` on any write; a skill with unsupported frontmatter or external references; `execution: local` combined with `write_consequential` (writes require Temporal, per the Playbooks); a deps model with non-serializable fields; a capability in `spec.agent.capabilities` outside the reviewed allowlist (for example a `Shell` or `LocalWorkspace` capability on an agent whose classification forbids it).

### 4.2 Runtime assembly

```python
def assemble(bundle: AgentBundle, bindings: PlatformBindings) -> Agent[ZetraDeps, BaseModel]:
    contracts = import_contracts(bundle)                       # inside src/, same path rule as loader.py
    toolset = ZetraToolset.from_bundle(bundle, bindings)       # wraps FunctionToolset + GatewayMCPToolset
    stack = [
        Instrumentation(settings=telemetry_settings(bundle)),
        ZetraPolicy(bundle),                                   # allowlist, budgets, stop, audit (section 5.2)
        *guardrails_for(bundle),                               # InputGuardrail / ToolGuardrail / OutputGuardrail
        SpendLimits(budgets_for(bundle), store=bindings.spend_store),
        ApprovalGate(bundle, bindings.approvals),              # HandleDeferredToolCalls bound to Temporal Update
        *judges_for(bundle),                                   # ToolCallJudge when tier >= T2
        ResolveApprovedModel(bindings.model_registry),         # 'approved:balanced-v1' -> pinned provider model
    ]
    if bundle.execution.startswith("temporal"):
        stack.append(TemporalDurability(**activity_configs(bundle)))
    return Agent.from_spec(
        bundle.agent_spec,
        name=bundle.name,                                      # stable: activity names derive from it
        deps_type=contracts.Deps,
        output_type=contracts.Output | DeferredToolRequests,
        toolsets=[toolset],
        capabilities=stack,
        custom_capability_types=[Skills, ...],
    )
```

Ordering matters and is fixed by the SDK, not the developer: PydanticAI runs `before_*` hooks in capability order and `after_*` in reverse, so `ZetraPolicy` sits before guardrails and approval, and `TemporalDurability` is last so every preceding hook that does I/O is already wrapped as a durable operation.

## 5. Temporal mapping

### 5.1 Activity granularity: per call, not per run

The v0.1 worker runs the whole agent in one `zetra.run_agent` Activity, so an activity failure re-runs every model and tool step. With `TemporalDurability`, each model request, each tool call, MCP `get_tools`/`call_tool`, and `validate_args` is its own Activity with its own timeout, retry policy and heartbeat (model activities default to a 30 s heartbeat timeout). This is the granularity the Playbooks' Temporal standard asks for. Two execution profiles follow:

| Profile | Where the agent loop runs | When to use |
| --- | --- | --- |
| `temporal` (default) | Workflow code drives `agent.run()`; PydanticAI wraps calls as Activities on the same worker | Agents whose tools are receivers or gateway MCP calls. Isolation comes from the worker pod, network policy and gateway identity. |
| `temporal-sandboxed` | One coarse Activity hands the bundle and input to a sandbox bridge (OpenShell) and returns a typed result | Agents that must execute untrusted or file/process-heavy code. Keeps today's coarse `zetra.run_agent` semantics and the Activity-to-sandbox bridge contract from the integration review. |
| `local` | `agent.run()` in-process, cooperative controls only | Read-only development and deterministic evals. Never for consequential writes. |

### 5.2 The generated workflow

```python
@workflow.defn(name="zetra.agent")          # one workflow type; the bundle digest is an input, not a type
class ZetraAgentWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = [...]          # registered per worker from the loaded bundles

    def __init__(self) -> None:
        self.state = RunState()             # typed, versioned: phase, steps, approvals, artifacts, outcome

    @workflow.signal
    def stop(self, reason: StopReason) -> None: ...

    @workflow.update
    async def approve(self, decision: ApprovalDecision) -> ApprovalReceipt: ...
    @approve.validator
    def validate_approval(self, decision: ApprovalDecision) -> None:
        # rejects before it is written to history: unknown operation, digest mismatch, expired, already consumed
        self.state.approvals.check(decision)

    @workflow.query
    def status(self) -> RunStatus: ...

    @workflow.run
    async def run(self, request: RunRequest) -> RunOutcome:
        agent = registry.agent(request.bundle_digest)
        deps = request.deps                                   # ZetraDeps, serializable
        result = await agent.run(request.input, deps=deps, usage_limits=limits(request))
        while isinstance(result.output, DeferredToolRequests):   # stop-the-world fallback when no inline handler
            results = await self.await_approvals(result.output)   # workflow.wait_condition on self.state + timer
            result = await agent.run(message_history=result.all_messages(), deferred_tool_results=results, deps=deps)
            if self.state.should_continue_as_new():
                workflow.continue_as_new(request.with_resume(result.conversation))
        return RunOutcome.from_result(result, self.state)
```

Design points, each traceable to a Playbook or upstream constraint:

- **Workflow ID** = `<env>:<tenant>:<agent>:<business-id>:<request-id>`; a retry of the same business operation reuses it, and `continue_as_new` stays in the chain.
- **Approval transport is an Update with a validator**, not a Signal, so a malformed or stale decision is rejected synchronously and never pollutes history. There is no official PydanticAI-plus-Temporal approval sample; the docs sketch two shapes, both supported here: an inline `HandleDeferredToolCalls` handler that awaits `workflow.wait_condition` on state the Update sets (default), or the stop-the-world shape where the run returns `DeferredToolRequests` and resumes with `deferred_tool_results` (fallback, also used by `execution: local`). Under Temporal a tool's `args_validator` runs in a `validate_args` activity before approval and again on resume with `tool_call_approved=True`, which is where the digest is recomputed. Temporal constraints shape the handler: Update validators are synchronous and cannot do I/O or call Activities, so approver identity is checked deterministically against a signed grant whose verification material arrives in the workflow input, or in an Activity *before* acceptance; a rejected Update leaves no history event; the server allows 10 in-flight and 2,000 total Updates per execution, so the approvals ledger bounds outstanding proposals and continue-as-new resets the count. Temporal's own approval pattern (record approver, decision, reason, timestamp; always time out with defined behaviour; deduplicate duplicates; expose a Query of all attempts) is followed verbatim. The Update payload carries an approval grant issued by the authenticated approval service; the workflow verifies digest, actor, tenant, release and expiry against `RunState`, and the receiver verifies the same grant again before committing. Signals remain for `stop`.
- **Approval binding** follows the action-bound, fail-closed pattern that the Loopjacking paper and Microsoft's ADR-0030 converge on: the recorded `ActionBinding` is `{operation_id, agent (bundle digest), tool name, tool schema version, resource/tenant, actor, parameters, policy_epoch, expires_at}`, canonicalized with **RFC 8785 JCS** and hashed with SHA-256. Today's `canonical()` (`json.dumps(sort_keys=True)`) is close but not JCS; the SDK adopts JCS so receipts interoperate. The enforcement point (gate, then receiver) rebuilds the binding from the arguments it is about to execute, recomputes the digest and compares, then checks unexpired and unconsumed in one transaction. `ToolApproved(override_args=...)` is refused by the gate unless the manifest explicitly allows approver edits, because an edit changes the digest. Approval receipts are hash-linked (`prev_receipt_hash`) so the audit chain for a run is tamper-evident.
- **Stop semantics.** `stop` flips state checked by `ZetraPolicy.before_model_request` and `before_tool_execute`, so the next step is refused inside the run rather than only before and after the Activity as today. A tool call already executing is not revoked by the workflow; receiver fencing covers that window. Python has no cancellation-scope API, and Activity cancellation is delivered only through heartbeats, so every generated tool Activity heartbeats and honours `CancelledError`, and the workflow uses `ActivityCancellationType.WAIT_CANCELLATION_COMPLETED` for writes so the outcome is recorded before the run ends.
- **Idempotency key** for every write is `<workflow-id>:<tool-call-id>:<operation-version>` and is reused across activity attempts. The receiver returns the original receipt on a duplicate and refuses the key with a changed payload, exactly as the SQLite demonstration does today.
- **Retry matrix** is derived from the tool declaration and is always explicit, because Temporal's default `RetryPolicy` has `maximum_attempts=0` (unbounded) and PydanticAI does not override it, so an unconfigured model activity can re-run a model request indefinitely. The compiler always emits a bounded policy in `model_activity_config` and in every tool's activity config: `read` and `retry: safe` get the default `RetryPolicy`; `idempotency_key_required` retries with the same key; `unsafe` gets `maximum_attempts=1` and a reconcile step on uncertain outcome. `Denied`, `ContractError`, `UserError`, `UnexpectedModelBehavior` and approval failures are non-retryable. Provider transport retries are set to zero so Temporal owns infrastructure retry (the upstream docs recommend `max_retries=0` on provider clients).
- **Histories stay bounded.** Large tool results and documents go to object storage behind `ArtifactReference{uri, sha256, size, classification, expires_at}`. PydanticAI has no built-in continue-as-new (upstream issue #6675, PR pending); the documented workaround is a capability hook on `before_model_request` that raises a typed exception when `workflow.info().is_continue_as_new_suggested()`, which the generated workflow catches and turns into `continue_as_new` with the stored `conversation`. `deps` is copied into every activity dispatch, so `ZetraDeps` stays small; oversized inbound payloads fail the workflow *task* and retry forever, so the compiler enforces payload budgets and the worker uses either Temporal's `ExternalStorage` data-converter option (256 KiB threshold, public preview) or a claim-check `PayloadCodec`. The same codec layer encrypts payloads at rest (encryption runs before externalization); a Codec Server decrypts for authorized UI users only. Server limits the design budgets against: history warns at 10,240 events / 10 MB and terminates at 51,200 events / 50 MB; payloads warn at 256 KB and fail at 2 MB. Before `continue_as_new` the workflow awaits `workflow.all_handlers_finished` so no approval Update is cut off mid-flight.
- **Versioning.** Agent `name`, toolset `id`s, capability `id`s, `@durable_operation` names and payload models are frozen in the bundle and diffed by `zetra check` against the previous release. Worker Versioning is GA (2026-03-30); Temporal's decision table recommends **PINNED behaviour plus Upgrade-on-Continue-as-New** for long workflows that wait on humans, which is exactly the approval case, so the generated workflow is `@workflow.defn(versioning_behavior=VersioningBehavior.PINNED)` and continue-as-new passes `initial_versioning_behavior=AUTO_UPGRADE`. `workflow.patched()` remains for in-place fixes to running histories. Either path requires a replay test over recorded histories before the worker image is promoted. The SDK docstring still labels `deployment_config` experimental, so the lab must confirm the pinned server/CLI versions support it.

### 5.3 Upstream constraints the generated code must respect

Verified against the `pydantic_ai.durable_exec.temporal` source and docs on 2026-10-11:

- Inside activities only a copy of the run context is available (`deps`, `run_id`, `conversation_id`, `metadata`, `tool_call_id`, `tool_name`, `tool_call_approved`, `tool_call_metadata`, `retry`, `max_retries`, `run_step`, `usage`, `usage_limits`, and a few more). Reading `model`, `messages`, `prompt` or `model_settings` raises `UserError`; mutating `ctx.usage` is discarded, so a tool that delegates to another agent must return that usage explicitly (issue #6886). `ZetraPolicy` therefore does its budget accounting workflow-side and treats activity-side counters as snapshots.
- Agent and workflow objects are module-level; `name` and every toolset, capability and `@durable_operation` identifier are frozen durable contracts. Per-run toolsets, `@agent.toolset` inside a workflow, `agent.run(capabilities=...)` and unregistered `Model` instances are refused, so model selection goes through a `ResolveModelId` capability or the `models=` registry.
- Only one durable engine per agent; `Instrumentation` is the one capability that may be added per run.
- `MCPToolset` opens a fresh session inside each `get_tools`/`call_tool` activity, so the gateway sees one authenticated session per call and tool discovery costs one activity per run.
- `UsageLimits` bound model responses workflow-side and do not see Temporal, SDK or transport retries; the "retry multiplication" guidance is to disable provider retries and bound the Temporal policy, which is what the compiler does.
- `PydanticAIPlugin` marks `UserError`, `AgentRunError` (including `UsageLimitExceeded`), `PydanticUserError` and workspace errors as workflow-failure types so they fail the workflow with a typed error rather than retrying the workflow task.
- `SubAgents` is not spec-serializable and stopped loading agent files by default in v2.52.0; the official `pydantic/pydantic-ai-temporal-example` still uses the deprecated `TemporalAgent` and has no continue-as-new, so it is a reference for worker and signal wiring only.

### 5.4 Worker

One worker binary, `python -m zetra.worker --bundle-dir /app/bundles --task-queue <queue>`, connects with `PydanticAIPlugin()` plus a Zetra payload codec and the tracing interceptor, registers `ZetraAgentWorkflow`, and refuses to start if any bundle's execution profile, capability set or receiver bindings are not satisfied by the mounted platform bindings. This generalizes today's read-only worker, which already refuses write capabilities without a qualified receiver; the refusal moves from "writes never" to "writes only with a bound, qualified receiver adapter". Task queues are per agent and per risk tier so capacity, worker identity and network policy can differ, but Temporal has no per-task-queue authorization: a queue is routing isolation only, and a credential boundary between tenants or tiers needs a separate namespace with its own namespace-scoped service account per worker deployment. The worker sets `identity=` to the bundle digest plus pod identity, enables the OpenTelemetry interceptor (the newer `OpenTelemetryPlugin` is still experimental), exports metrics with low-cardinality tags only, and treats the workflow sandbox as a determinism aid rather than isolation, as the SDK README states.

### 5.5 Custom coordination

Some agents need deterministic multi-phase coordination that should not be left to the model (the incident-triage plan → approve → commit shape). They subclass the base workflow and compose the provided helpers:

```python
@workflow.defn(name="zetra.incident-triage")
class TriageWorkflow(ZetraWorkflow[Incident, TriageResult]):
    @workflow.run
    async def run(self, request: RunRequest[Incident]) -> RunOutcome[TriageResult]:
        proposal = await self.run_agent("triage-planner", request)          # read-only agent run
        grant = await self.require_approval(proposal.action, ttl=timedelta(hours=24))
        receipt = await self.commit(proposal.action, grant)                  # executor tool through the receiver
        return self.complete(TriageResult(status="ticket_created", ticket_id=receipt.id, evidence=proposal.evidence))
```

`run_agent`, `require_approval`, `commit`, `compensate` and `complete` are the only verbs; each is replay-safe and emits the audit events the governance guide expects. This is also the seam for multi-agent systems later: `run_agent` on a different bundle or a child workflow, guarded by the System Spec that `agentctl` already validates.

## 6. Zero-trust controls and where they are enforced

| Principle (README) | Compile time | Runtime (cooperative, in-process) | Independent enforcement |
| --- | --- | --- | --- |
| Own identity per agent | Bundle digest = release_id; frozen names/IDs | Correlation headers on every gateway call and span | Gateway JWT subject binding, SPIFFE/workload identity, image digest admission |
| Deny by default | Unlisted tool or capability fails `check`; skills allowlisted | `ZetraToolset` refuses unlisted names; `PrepareTools` hides tools outside deps scopes | agentgateway CEL tool rules compiled from the bundle; NetworkPolicy; sandbox policy |
| Per-action, expiring authority | Consequential tools must declare approval and receiver | Approval gate binds digest/actor/tenant/release/op; single use in `RunState` | Receiver re-verifies grant and consumes it atomically with the effect |
| Mediate every call | Only `GatewayMCPToolset` may reach MCP; raw HTTP tools are a finding | Restricted client semantics kept (no redirects, bounded responses, pinned protocol) | Gateway authorization and audit; egress deny |
| Assume breach | Execution profile chosen by classification | Guardrails and injection defender on tool results; `ToolCallJudge` for T2+ | OpenShell / Tetragon / Cilium as today |
| Evidence before trust | Required scenario IDs generated from tier | Audit events, spend records, approval receipts | Signed bundle + evidence; `render` withheld until qualified |
| Observation never grants permission | `profile` output stays a candidate | n/a | n/a |

Everything in the middle column is cooperative Python, exactly as `SECURITY.md` describes today. The SDK makes the right column *reachable* by compiling gateway rules, receiver bindings and network intent from the same bundle, but it does not claim them.

The external standards each row should be able to cite, so conformance claims are precise rather than generic:

- **OWASP Top 10 for Agentic Applications 2026**: ASI02 Tool Misuse (schema validation before every tool call, tool allowlist), ASI03 Identity and Privilege Abuse (per-agent workload identity, no shared service accounts), ASI04 Agentic Supply Chain (digest-pinned skills, MCP tool schemas and capability packages), ASI05 Unexpected Code Execution (no skill script execution; sandboxed profile for code-running agents), ASI06 Memory and Context Poisoning (tool-result guardrails, injection defender), ASI09 Human-Agent Trust Exploitation (action-bound approvals), ASI10 Rogue Agents (budgets, stop, receiver fencing).
- **MCP 2026-07-28**: OAuth 2.1 with RFC 9728 protected-resource metadata and RFC 8707 `resource`, audience validation and no token passthrough at the gateway; tool annotations (`destructiveHint` defaults to true) treated as untrusted and overridden by the manifest's declared `effect`; the stateless core removes `initialize` and `Mcp-Session-Id`, so the restricted client in `gateway.py` (pinned to 2025-11-25) needs a revision.
- **Agent identity**: `draft-klrc-aiagent-auth` and the NIST NCCoE concept paper (SPIFFE/WIMSE workload identity, RFC 8693 token exchange, transaction tokens, no token forwarding by tools, tamper-evident audit with delegated subject). None is a ratified standard yet; the SDK should keep identity behind the gateway and receiver adapters so the mechanism can change.
- **Approval binding**: Loopjacking (arXiv 2609.21081), Microsoft ADR-0030 and OWASP AISVS 9.4.2 all converge on JCS canonicalization, SHA-256 digests, expiry, single use and re-verification at the enforcement point, which is what section 5.2 adopts.

## 7. Verification and evidence (generated, not hand-written)

- `zetra check`: build + static findings + risk tier + required scenario IDs + diff against previous bundle (renamed tool, widened capability, raised budget, changed approval mode are each a finding).
- `zetra eval`: runs (a) **generated** deterministic scenarios for every required risk ID using `FunctionModel` scripts that attempt the forbidden thing (call an unlisted tool, commit without approval, replay a consumed grant, exceed budget, inject instructions through a tool result) and assert the independent outcome at a fixture receiver, and (b) **authored** `pydantic_evals` datasets for task quality. Replaces `evals/scenarios.json`; authored tests bind to IDs with `@scenario("approval.bound")`.
- `zetra replay`: replays recorded histories (from the lab and from previous eval runs) against the candidate worker; required for any bundle whose frozen identifiers changed.
- Evidence JSON keeps today's shape (source digest, evaluator digest, scenario results) and adds bundle digest, pinned `pydantic-ai`/`harness`/`temporalio` versions, model policy resolution and skill digests. Still unsigned locally; signing stays a pipeline responsibility.

## 8. Roadmap (rough, for discussion)

| Phase | Deliverable | Exit evidence |
| --- | --- | --- |
| 0. Spike (1–2 weeks) | Port `examples/incident-triage` to PydanticAI v2 + `TemporalDurability` with an approval Update; measure lines of per-agent code before/after; record a history and replay it | Working demo on the local Temporal dev server; replay passes; code-size comparison |
| 1. Compiler and local runtime | v1alpha2 manifest, `AgentBundle`, `@tool` decorator and AST check, `ZetraToolset`, `ZetraPolicy`, generated scenario tests, `zetra build/check/eval/run` | `just check` green; all four examples compiled from manifests with no `factory.py` |
| 2. Durable runtime | `ZetraAgentWorkflow`, worker, retry matrix, stop and approval Update, continue-as-new, payload codec, replay command | Time-skipping tests, replay fixtures, worker-crash and duplicate-delivery tests |
| 3. Approval and receiver protocol | `Receiver` adapter protocol (SQLite reference + one remote), approval service contract, grant verification at gate and receiver | Negative tests: tampered digest, expired grant, replayed grant, changed payload |
| 4. Gateway and telemetry | `GatewayMCPToolset` over agentgateway, compiled CEL rules, OTel correlation across gateway/Temporal/agent spans | Lab run reproducing the existing gateway and telemetry evidence through the new runtime |
| 5. Deployment | `render` from bundle (worker, task queue, network policy, gateway policy), image build, Worker Versioning plan | Zero-replica render and cluster qualification as today |
| 6. Multi-agent | `ZetraWorkflow.run_agent` on member bundles under a System Spec; child workflows for independent lifecycles | System-level replay and delegation budget tests |

Phases 1 and 2 remove the repeated code. Phases 3 to 5 make what gets deployed enforceable rather than merely declared.

## 9. Decisions to make together

1. **Commit to PydanticAI as the golden path?** The manifest can stay framework-neutral, but the SDK stack above is PydanticAI-specific. Keeping a second adapter alive doubles the test matrix. Recommendation: PydanticAI first, keep the `temporal-sandboxed` coarse profile as the framework-neutral escape hatch.
2. **Manifest shape.** Envelope with embedded `AgentSpec` (proposed) versus a pure `AgentSpec` with Zetra data under `metadata`. The envelope gives a strict schema and a catalog `kind`; the pure form lets `Agent.from_file` work with no Zetra code. Recommendation: envelope, with `zetra export-spec` for interoperability.
3. **Approval transport.** Update with validator (proposed) versus Signal. Updates give synchronous rejection and a return value; they require Temporal server support that must be verified in the lab (see open research items).
4. **Budget authority.** Accept PydanticAI `SpendLimits` post-hoc accounting with a shared Redis store for v1, with durable per-run step counters in workflow state, and keep reservation-based accounting as a target? Or build the reservation service now.
5. **Sandboxing versus fine-grained activities.** Per-call activities run in the worker process. If OpenShell isolation per agent is mandatory for a tier, that tier uses the coarse `temporal-sandboxed` profile and loses per-step durability. Is that trade acceptable, or should the sandbox host a worker?
6. **Harness dependency risk.** `pydantic-ai-harness` is 0.x. Pin, wrap, and test on upgrade; or vendor the few capabilities we rely on (Skills, Guardrails, SpendLimits).
7. **Where tools live.** Per-agent `tools/` versus a shared `tools/` package with `tool.yaml` contracts, as `agentctl` already validates. Recommendation: both, with promotion to shared after the contract stabilizes, per the Playbooks.
8. **agentctl fold-in.** The compiler is the natural place to absorb `agentctl validate`, `skills validate`, `risk validate` and `release check`. It needs Python 3.12 today; `zetra` supports 3.11.
9. **Canonicalization.** Replace `canonical()` with RFC 8785 JCS for action digests and receipts so Zetra receipts interoperate with ADR-0030-style verifiers. Recommendation: yes, with a one-release compatibility shim in the SQLite receiver.
10. **Isolation unit.** Namespace per tenant or per risk tier (credential boundary) versus task queue per agent (routing only). Recommendation: namespace per tier at minimum, task queue per agent inside it.
11. **Skill resources.** Harness `Skills` reads SKILL.md only; the community `pydantic-ai-skills` adds script execution and resource reads. Recommendation: Harness only; a skill that needs code becomes a reviewed tool.
12. **MCP protocol revision.** Move the gateway client to the stateless 2026-07-28 core now, or stay on 2025-11-25 until agentgateway's pinned release supports it. Needs a lab check of agentgateway 1.6.

## 10. Research status

The upstream review is complete and recorded in [durable-agent-sdk-research.md](durable-agent-sdk-research.md), with sources and the items that could only be confirmed through secondary summaries. Residual unknowns that need a lab check rather than more reading:

- Whether the pinned Temporal server and CLI in the local lab support Worker Versioning and Upgrade-on-Continue-as-New, and whether `ExternalStorage` behaves under the 1.34.0 SDK pin.
- Whether agentgateway 1.6 accepts the MCP 2026-07-28 stateless core, which determines when `gateway.py` changes protocol.
- Whether `TemporalDurability`'s per-call activities can be hosted inside an OpenShell sandbox pod without breaking the gateway identity chain, which decides question 5 in section 9.
- The real per-agent code size after the phase 0 spike, which is the number this proposal is ultimately judged on.
