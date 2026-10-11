# PydanticAI Agent Design Standard

## Canonical Package

```text
project/
  .agents/skills/                 # Development-agent skills
  src/<package>/
    agents/<agent_name>/
      agent.yaml                  # Machine-readable Agent Spec
      factory.py                  # Validated PydanticAI construction
      dependencies.py
      models.py
      instructions/
      skills/                     # Agent-private runtime skills
      tools/                      # Agent-private tools
    skills/                       # Shared runtime skills
    tools/                        # Shared tools and toolsets
    workflows/
    activities/
    runtime/
    observability/
    policy/
    settings.py
  evals/
  tests/
  deploy/
```

Use private directories for capabilities owned by one agent. Promote a capability to shared only after its contract and ownership are stable.

## Agent Spec Fields

Record at least:

| Area | Required information |
| --- | --- |
| Identity | Stable name, version, owner, status |
| Purpose | Goal, users, non-goals |
| Contract | Input, dependencies, structured output, failure types |
| Execution | Ephemeral, durable, or human-governed; time and retry budget |
| Risk | Assessment reference, governance tier, residual risk; data and side-effect classification |
| Capabilities | Explicit skills, tools, and denied operations |
| Quality | Eval suite, hard gates, target metrics |
| Operations | SLOs, telemetry, budgets, escalation, rollback |

## Factory Invariants

- Receive model, dependencies, toolsets, and runtime configuration explicitly.
- Keep provider credentials in runtime configuration, never prompts or skill files.
- Return structured Pydantic output for machine-consumed results.
- Keep the factory deterministic and cheap to construct.
- Register only capabilities listed in the Agent Spec.
- Version prompts and policies independently from model configuration.

Package Agent Specs, instruction files, and runtime skills into the built artifact. Resolve package resources independently of the process working directory.

## Execution Classification

Choose `ephemeral` only when work is short-lived and has no externally visible side effect. Choose `durable` when the process needs Temporal-backed recovery, timers, signals, or reversible writes. Choose `human_governed` when consequential actions require approval. A path may elevate its effective class, but it must never silently downgrade the class required by an enabled tool.

## Risk Classification

Assess concrete harm scenarios across financial, operational, reputational,
legal, security, privacy, regulatory, and human-impact/safety dimensions. The
Agent Spec risk tier is the governance tier derived from the highest inherent
scenario and mandatory floors. Residual risk is scored after verified controls
and determines the launch decision. Do not average dimensions or scenarios.

Increase controls with risk: tighter allowlists, human approval, stronger eval gates, more complete telemetry, smaller budgets, and explicit rollback.

## Definition of Done

- Agent Spec is reviewable and versioned.
- Types validate all public boundaries.
- Skill and tool allowlists are explicit.
- Import and construction tests pass without external calls.
- At least one happy-path and one denied or failure-path eval exists.
- Execution class and evidence-linked risk assessment have written rationale.
- Owner, SLO, budget, escalation, and rollback are named.
