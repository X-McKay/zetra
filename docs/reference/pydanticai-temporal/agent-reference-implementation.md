# 12. Reference implementation

This chapter shows how the standards fit together. It is intentionally illustrative: adapt imports and capability construction to the exact pinned PydanticAI and Temporal versions in the project.

## Agent package

```text
src/acme_agents/
├── agents/
│   └── customer_support/
│       ├── agent.yaml
│       ├── factory.py
│       ├── dependencies.py
│       ├── models.py
│       └── instructions/
├── skills/
│   └── customer-policy/
│       └── SKILL.md
├── tools/
│   └── customer_records/
│       ├── models.py
│       ├── policy.py
│       ├── service.py
│       ├── tools.py
│       └── toolset.py
├── workflows/
│   └── support.py
├── runtime/
│   ├── worker.py
│   └── registries.py
└── policy/
    ├── agent_metadata.py
    ├── execution.py
    └── approval.py
```

## Governance metadata

```python
from typing import Literal

from pydantic import BaseModel, Field


class AgentBudgets(BaseModel):
    max_requests: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_cost_usd: float = Field(gt=0)


class AgentMetadata(BaseModel):
    contract_version: int
    owner: str
    version: str
    execution_class: Literal["ephemeral", "durable", "human_governed"]
    risk_tier: Literal["low", "medium", "high", "critical"]
    risk_assessment: str
    data_classification: str
    model_policy: str
    enabled_skills: list[str]
    enabled_toolsets: list[str]
    budgets: AgentBudgets
```

## Factory flow

```python
def build_agent(settings):
    spec = load_agent_spec("customer_support/agent.yaml")
    metadata = AgentMetadata.model_validate(spec.metadata)

    model = settings.model_registry.resolve(metadata.model_policy)
    skills = settings.skill_registry.resolve(metadata.enabled_skills)
    toolsets = settings.tool_registry.resolve(
        metadata.enabled_toolsets,
        execution_class=metadata.execution_class,
    )

    capabilities = [
        skills,
        build_usage_limits(metadata.budgets),
        build_spend_limits(metadata, settings.spend_store),
        build_observability(settings),
    ]

    if metadata.execution_class in {"durable", "human_governed"}:
        capabilities.append(build_temporal_durability(settings))

    if metadata.execution_class == "human_governed":
        require_approval_policy(settings.approval_registry, spec.name)

    risk = load_risk_assessment(metadata.risk_assessment)
    validate_agent_contract(spec, metadata, model, skills, toolsets, risk)

    return construct_agent(
        spec=spec,
        model=model,
        deps_type=SupportDependencies,
        output_type=SupportResolution,
        toolsets=toolsets,
        capabilities=capabilities,
    )
```

The registries are policy boundaries. They resolve only reviewed models, skills, tools, and approval policies. They also make startup validation deterministic.

## Durable workflow

```python
from temporalio import workflow


@workflow.defn
class SupportWorkflow:
    @workflow.run
    async def run(self, request: SupportRequest) -> SupportResolution:
        dependencies = SupportDependencies.from_workflow_input(request)
        result = await support_agent.run(request.prompt, deps=dependencies)
        return result.output
```

The actual worker registers the workflow and the PydanticAI Temporal plugin. The API starts or signals the workflow; it does not call the durable agent directly.

## Human-governed action

```text
agent proposes typed action
        ↓
deterministic policy requires approval
        ↓
workflow records canonical action hash
        ↓
authenticated approver sends Signal/Update
        ↓
workflow verifies scope, expiry, and hash
        ↓
activity performs action with approval ID and idempotency key
        ↓
audit record captures result
```

## Local development

The local environment SHOULD provide:

- A development Temporal server
- Mock or sandbox external services
- Non-production model credentials and spend limits
- OpenTelemetry collector or Logfire development project
- Fast smoke evals

Suggested commands:

```bash
scripts/agentctl validate
scripts/agentctl skills validate
scripts/agentctl eval run customer-support
scripts/agentctl release check \
  --report artifacts/evals/customer-support.json \
  --policy evals/customer_support/release-policy.yaml \
  --baseline evals/customer_support/baselines/main.json
```

The repository may wrap these commands with Make, Just, or its build system. See the companion [`agentctl` documentation](../tools/agentctl/README.md).

## Pull-request evidence

A behavioral pull request SHOULD include:

```text
agent and capability versions changed
reason for the change
affected execution classes, governance tiers, and risk scenarios
eval suites run
before/after quality, latency, and cost
Temporal replay result, when applicable
rollout and rollback plan
```

## Initial adoption path

Teams adopting the playbook incrementally should:

1. Inventory agents, owners, tools, side effects, and credentials.
2. Assign execution classes and complete scenario-based risk assessments.
3. Introduce typed inputs, outputs, and tool contracts.
4. Move state-changing agents behind Temporal.
5. Add hard budgets and correlated telemetry.
6. Establish smoke, regression, safety, and trajectory evals.
7. Add human approval to consequential actions.
8. Complete the production-readiness checklist.
