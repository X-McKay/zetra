# 12. Reference implementation

This reference extends the Agent Playbook implementation with a governed system
package. It illustrates boundaries and validation order; applications should
adapt APIs to their pinned PydanticAI and Temporal versions.

## Package

```text
src/acme_agents/
├── agents/
│   ├── resolution_coordinator/
│   │   └── agent.yaml
│   ├── customer_investigator/
│   │   └── agent.yaml
│   ├── policy_reviewer/
│   │   └── agent.yaml
│   └── customer_action_executor/
│       └── agent.yaml
├── systems/
│   └── customer_resolution/
│       ├── system.yaml
│       ├── factory.py
│       ├── models.py
│       ├── coordination.py
│       └── policies/
│           ├── delegation.yaml
│           ├── data-flow.yaml
│           └── termination.yaml
├── workflows/
│   └── customer_resolution.py
├── activities/
│   ├── agents.py
│   ├── admission.py
│   └── customer_actions.py
├── runtime/
│   ├── manifests.py
│   ├── registries.py
│   └── worker.py
└── policy/
    ├── authorization.py
    ├── budgets.py
    └── approval.py
```

## Typed system metadata

```python
from typing import Literal

from pydantic import BaseModel, Field


class SystemLimits(BaseModel):
    max_agent_runs: int = Field(gt=0)
    max_delegation_depth: int = Field(ge=0)
    max_parallel_agents: int = Field(gt=0)
    max_rounds: int = Field(gt=0)
    max_model_requests: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_cost_usd: float = Field(gt=0)
    deadline_seconds: int = Field(gt=0)


class SystemMetadata(BaseModel):
    contract_version: int
    owner: str
    operational_owner: str
    version: str
    execution_class: Literal["ephemeral", "durable", "human_governed"]
    governance_tier: Literal["low", "medium", "high", "critical"]
    risk_assessment: str
    threat_model: str
    evaluation_policy: str
    data_classification: str
    temporal_enabled: bool
    approval_policy: str | None = None
```

Member, role, interaction, topology, state, and termination models validate the
rest of the System Spec. Use discriminated unions for topology-specific fields
instead of one mapping with many optional keys.

## Interaction models

```python
from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class BudgetGrant(BaseModel):
    max_model_requests: int = Field(gt=0)
    max_tool_calls: int = Field(ge=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_cost_usd: float = Field(gt=0)


class Delegation(BaseModel, Generic[InputT]):
    system_name: str
    system_version: str
    manifest_id: str
    run_id: str
    interaction_id: str
    delegation_id: str
    parent_delegation_id: str | None
    caller: str
    callee: str
    actor_id: str
    tenant_id: str
    authorization_scopes: frozenset[str]
    input: InputT
    evidence_refs: list[str]
    budget: BudgetGrant
    depth: int = Field(ge=0)
    deadline: datetime


class DelegationResult(BaseModel, Generic[OutputT]):
    delegation_id: str
    status: Literal[
        "completed",
        "needs_information",
        "rejected",
        "policy_blocked",
        "budget_exhausted",
        "timeout",
        "cancelled",
        "failed_retryable",
        "failed_permanent",
    ]
    output: OutputT | None
    evidence_refs: list[str]
    performed_effects: list[str]
    model_requests: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
```

Use concrete input and output types for each interaction. Generic models show
the common envelope but do not replace edge-specific schema validation.

## Factory flow

```python
def build_system(settings):
    spec = load_system_spec("customer_resolution/system.yaml")
    metadata = SystemMetadata.model_validate(spec.metadata)
    policies = settings.policy_registry.resolve_system_policies(spec)

    members = {}
    for member_ref in spec.members:
        agent_manifest = settings.agent_registry.resolve_pinned(member_ref)
        validate_member_conformance(agent_manifest, member_ref, metadata)
        members[member_ref.agent] = build_restricted_member(
            manifest=agent_manifest,
            restrictions=member_ref.restrictions,
            settings=settings,
        )

    graph = build_interaction_graph(spec.interactions)
    validate_graph(graph, members, policies, spec.limits)
    validate_effective_class_and_tier(spec, members, graph)
    validate_risk_and_evaluation_references(spec, settings)

    manifest = build_composition_manifest(
        spec=spec,
        members=members,
        graph=graph,
        policies=policies,
        worker_build=settings.worker_build,
    )
    return SystemRuntime(spec, members, graph, policies, manifest)
```

Registries resolve only reviewed and permitted artifacts. Construction performs
no model or external-service I/O and fails closed on any inconsistency.

## Deterministic policy boundary

```python
def authorize_delegation(request, state, runtime, actor_context):
    edge = runtime.graph.require_edge(
        interaction_id=request.interaction_id,
        caller=request.caller,
        callee=request.callee,
    )
    edge.input_adapter.validate_python(request.input)

    scopes = (
        edge.maximum_scopes
        & state.caller_delegable_scopes(request.caller)
        & runtime.member_maximum_scopes(request.callee)
        & actor_context.authorization_scopes
        & state.allowed_scopes()
    )
    if not edge.minimum_scopes <= scopes:
        raise DelegationDenied("required authority is not available")

    grant = state.budgets.reserve(edge.maximum_budget, request.delegation_id)
    return AuthorizedDelegation.from_request(request, scopes=scopes, budget=grant)
```

The actual policy also checks tenant, purpose, data classification, graph
version, member health, deadline, depth, concurrency, approval, and kill-switch
state.

## Durable workflow

```python
from temporalio import workflow


@workflow.defn
class CustomerResolutionWorkflow:
    def __init__(self) -> None:
        self.state = ResolutionState.initial()

    @workflow.run
    async def run(self, request: ResolutionRequest) -> ResolutionOutcome:
        runtime = load_replay_safe_runtime(request.composition_manifest)
        self.state.start(request, runtime)

        while not self.state.is_terminal:
            proposal = self.state.next_deterministic_step()

            if proposal.requires_agent:
                authorized = authorize_delegation(
                    proposal.delegation,
                    self.state,
                    runtime,
                    request.actor_context,
                )
                result = await workflow.execute_activity(
                    run_member_agent,
                    authorized,
                    **runtime.activity_options(authorized.interaction_id),
                )
                self.state.accept_result(result, runtime)
            else:
                self.state.apply(proposal, runtime)

            self.state.evaluate_termination(runtime.termination_policy)

        return self.state.to_outcome()
```

`load_replay_safe_runtime` uses the recorded manifest and workflow-safe policy
data. It does not query mutable registries or read deployment configuration.

In a model-routed system, a coordinator activity returns a typed route proposal.
The workflow validates it and dispatches the selected member; the coordinator
does not directly invoke undeclared agents.

## Dynamic admission

```python
candidate_evidence = await workflow.execute_activity(
    inspect_registry_candidate,
    candidate_request,
    **registry_activity_options,
)

decision = deterministic_admission_policy.evaluate(
    evidence=candidate_evidence,
    current_graph=self.state.graph,
    limits=self.state.remaining_limits,
    policy_version=self.state.admission_policy_version,
)

if decision.admitted:
    self.state.add_member_and_edges(decision)
    self.state.record_run_manifest_update(decision)
```

The activity authenticates the registry and gathers current evidence. The
workflow makes the replay-safe decision from the recorded result. A model may
propose a capability need or candidate name but cannot perform admission.

## Fan-out and gather

```python
authorized = [
    authorize_delegation(item, self.state, runtime, actor_context)
    for item in deterministic_branch_order(proposals)
]

results = await asyncio.gather(
    *(run_member_activity(item, runtime) for item in authorized),
    return_exceptions=True,
)

normalized = normalize_results_in_branch_order(authorized, results)
self.state.accept_fanout(normalized, runtime.aggregation_policy)
```

Production workflow code uses Temporal-safe concurrency primitives and pinned
SDK behavior. Logical branch order, not completion order, governs aggregation.

## Human-governed action

```text
members produce evidence and a typed action proposal
        ↓
deterministic policy verifies graph, authority, evidence, and limits
        ↓
workflow records action hash, causal references, and composition manifest
        ↓
authenticated approver reviews final action and material dissent
        ↓
workflow verifies actor, tenant, scope, policy, expiry, hash, and manifest
        ↓
executor activity performs the exact action with approval and idempotency IDs
        ↓
workflow records result, usage, audit evidence, and compensation obligation
```

## Composition manifest

```python
class CompositionManifest(BaseModel):
    manifest_id: str
    system_name: str
    system_version: str
    contract_version: int
    spec_sha256: str
    execution_class: str
    governance_tier: str
    risk_assessment_sha256: str
    topology: str
    members: list[ResolvedMemberManifest]
    interactions: list[ResolvedInteractionManifest]
    policy_hashes: dict[str, str]
    schema_hashes: dict[str, str]
    limits: SystemLimits
    evaluation_policy_sha256: str
    worker_build_id: str
```

Compute `manifest_id` from canonical serialization of the resolved fields. A
dynamic run manifest references the base composition manifest and appends
admission and graph-version records.

## Local development

Provide:

- A development Temporal server
- Mock and sandbox member agents, registries, tools, and external services
- Non-production credentials and strict spend limits
- Deterministic model and member doubles for workflow tests
- Failure-injection controls for members, queues, events, and stores
- OpenTelemetry collection and graph-aware trace inspection
- Fast member, interaction, and system smoke evals

Suggested future commands are:

```bash
scripts/agentctl system validate
scripts/agentctl system graph customer-resolution-system
scripts/agentctl eval run --system customer-resolution-system
scripts/agentctl release check \
  --report artifacts/evals/customer-resolution-system.json \
  --policy evals/systems/customer_resolution_system/release-policy.yaml \
  --baseline evals/systems/customer_resolution_system/baselines/main.json
```

These commands describe the intended `agentctl` extension and are not yet
implemented in the current toolkit.

## Initial adoption path

1. Inventory existing members, owners, tools, identities, data, and side effects.
2. Draw the actual interaction and trust graph.
3. Run the admission test against a simpler baseline.
4. Create valid Agent Specs and assessments for every member.
5. Introduce the System Spec, typed envelopes, and deterministic policies.
6. Move long-running and state-changing coordination behind Temporal.
7. Add root budgets, capacity limits, termination, and correlated telemetry.
8. Add interaction, topology, adversarial, durability, and ablation evals.
9. Complete the system risk assessment and production-readiness checklist.
10. Start with static membership; enable evaluated dynamic mechanisms where
    their measured benefit justifies the added control surface.
