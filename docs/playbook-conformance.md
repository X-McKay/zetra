# Playbook conformance map

Zetra implements the technology-neutral [Playbooks](https://github.com/X-McKay/playbooks). This map shows, chapter by chapter, which part of Zetra answers each playbook standard and how far that answer has been proven.

Status uses the repository's existing vocabulary, so a target is never mistaken for an observed behaviour:

- **Implemented**: code in this repository, covered by `just check`.
- **Lab-qualified**: exercised against real components in the disposable `kind-zetra-validation` lab, with evidence under `integrations/`.
- **Candidate**: produced as a reviewable artifact, not trusted or activated.
- **Target**: described in the guides, not yet built.
- **Gap**: the playbook asks for it and Zetra has no answer yet.

## Agent Playbook

Chapters 1 to 8 are the playbook's Core part; chapters 9 to 13 are Advanced concepts.

| Chapter | Zetra answer | Status |
| --- | --- | --- |
| 1. Principles and scope | [Zero-trust principles](../README.md#zero-trust-principles); [security boundaries](../SECURITY.md) | Implemented as policy; enforcement varies by row below |
| 2. Reference architecture | [Integration deep dive](integration-deep-dive.html), [deployment playbook](deployment.html) | Lab-qualified for the local stack; production composition is target |
| 3. Agent contract | Manifest schema `schemas/agent-v1alpha1.schema.json`, loader `src/zetra/manifest.py`, typed factories in `examples/` | Implemented (v1alpha1) |
| 4. Skills | Capabilities registered in the manifest; repository workflow skills under `skills/` | Implemented as logical registration; no independent skill isolation |
| 5. Tools | Restricted MCP client `src/zetra/gateway.py` behind agentgateway | Implemented; gateway read lab-qualified |
| 6. Evaluation | `zetra eval` and scenario suites in `examples/*/evals/` | Implemented; evidence is unsigned |
| 7. Observability | OpenTelemetry collector fixture, gateway trace correlation | Lab-qualified for fixture-specific assertions |
| 8. Production readiness | `zetra render` at zero replicas, `zetra catalog` candidates, worker qualification | Candidate; production activation is withheld |
| 9. Durable execution and human approval | `src/zetra/temporal_workflow.py`, `src/zetra/temporal_worker.py`; [Temporal reference](reference/pydanticai-temporal/temporal-execution.md) | Implemented; worker history replay lab-qualified |
| 10. Cost optimization | Manifest budgets enforced by cooperative runtime checks | Implemented as cooperative checks; spend accounting for paid models is a gap |
| 11. Security and governance | [Governance guide](governance.html); OpenShell sandbox, Tetragon and network policy | Lab-qualified; OpenShell on Kubernetes is experimental upstream |
| 12. Risk assessment | `zetra check` risk analysis in `src/zetra/analysis.py`; `agentctl risk validate` against the full risk-assessment schema | Implemented; the two are not yet unified |
| 13. Implementation patterns | This repository; [PydanticAI and Temporal reference](reference/pydanticai-temporal/agent-reference-implementation.md) | Implemented for the selected stack |

## Multi-Agent Systems Playbook

| Chapter | Zetra answer | Status |
| --- | --- | --- |
| 1 to 5. Principles, architecture, system contract, roles, delegation | `agentctl system scaffold/validate/graph`; [multi-agent reference implementation](reference/pydanticai-temporal/multi-agent-reference-implementation.md) | System Spec validation implemented; execution is target, as Zetra runs single agents today |
| 6. Durable coordination | [Temporal coordination reference](reference/pydanticai-temporal/temporal-durable-coordination.md) | Target |
| 7 to 12. Evaluation, observability, cost, security, readiness, patterns | Not yet implemented beyond the single-agent equivalents above | Gap |
| 13. System risk assessment | `agentctl risk validate` | Implemented as validation |

[`agentctl`](../tools/agentctl/README.md), which moved here from the Playbooks, already validates System Specs, risk assessments and skills and renders composition graphs. It runs alongside the `zetra` CLI until its commands fold in; the multi-agent runtime rows stay at target until Zetra executes a composed system.

## Keeping this map honest

Change a row only when the evidence it cites changes. Moving a row to lab-qualified needs a recorded run under `integrations/`; moving it to implemented needs a test in `just check`. A playbook revision that renames or adds a chapter should update this map in the same change.
