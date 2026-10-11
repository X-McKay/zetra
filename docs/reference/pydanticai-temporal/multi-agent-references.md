# References

These sources inform the draft. The [Agent Playbook references](../agent-playbook/references.md)
remain applicable to every member agent and to inherited PydanticAI, Temporal,
evaluation, observability, risk, and security requirements.

Framework behavior and protocol details MUST be checked against the versions
pinned by an implementation.

## PydanticAI

- [Multi-agent applications](https://pydantic.dev/docs/ai/guides/multi-agent-applications/) — delegation, programmatic handoff, graph-based control flow, and deep-agent patterns.
- [Subagents](https://pydantic.dev/docs/ai/harness/subagents/) — named delegate agents, isolated runs, dependency forwarding, usage limits, failure handling, and cancellation.
- [Dynamic Workflow](https://pydantic.dev/docs/ai/harness/dynamic-workflow/) — model-authored bounded sub-agent choreography, fan-out, chaining, voting, structured results, sandboxing, and call budgets.
- [Capabilities](https://pydantic.dev/docs/ai/capabilities/overview/) — planning, delegation, context, memory, guardrail, and runtime capabilities.
- [Agents](https://pydantic.dev/docs/ai/core-concepts/agent/) — typed agents, dependencies, toolsets, output, and execution iteration.
- [Temporal durable execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/) — workflow and activity integration, stable identities, payloads, retries, and telemetry.
- [Pydantic Evals](https://pydantic.dev/docs/ai/evals/evals/) — cases, datasets, evaluators, experiments, trajectories, and reports.
- [Usage API and limits](https://pydantic.dev/docs/ai/api/pydantic-ai/usage/) — request, tool, token, and cost controls.

## Temporal

- [Python SDK developer guide](https://docs.temporal.io/develop/python)
- [Child Workflows](https://docs.temporal.io/child-workflows) — parent and child lifecycle, isolation, cancellation, and history guidance.
- [Message passing](https://docs.temporal.io/encyclopedia/workflow-message-passing) — Signals, Queries, and Updates.
- [Durable AI](https://docs.temporal.io/ai)
- [Safe deployments](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning)

## Interoperability

- [Agent2Agent Protocol](https://a2a-protocol.org/latest/specification/) — interoperable agent discovery, task, message, artifact, and communication contracts.
- [Model Context Protocol](https://modelcontextprotocol.io/specification/latest) — model and agent connections to tools, resources, and prompts.
- [Agent Skills specification](https://agentskills.io/specification) — portable procedural capability packages.

Interoperability is not authorization. Protocol discovery and metadata provide
inputs to deterministic admission and policy; they do not establish trust,
conformance, or permission to invoke a remote agent.

## Observability

- [OpenTelemetry generative-AI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) — agent, model, tool, token, and workflow telemetry conventions.
- [OpenTelemetry messaging semantic conventions](https://opentelemetry.io/docs/specs/semconv/messaging/) — producer, consumer, message, context propagation, span links, and asynchronous trace structure.
- [OpenTelemetry trace semantic conventions](https://opentelemetry.io/docs/specs/semconv/general/trace/) — trace and span conventions.

Semantic conventions may be experimental or move between specification
locations. Pin the convention version emitted by instrumentation and avoid
unstable high-cardinality attributes in production metrics.

## Risk, security, and governance

- [NIST AI Risk Management Framework](https://airc.nist.gov/airmf-resources/airmf/)
- [NIST Generative AI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)
- [NIST Cybersecurity Framework 2.0](https://www.nist.gov/cyberframework)
- [NIST Privacy Framework](https://www.nist.gov/privacy-framework/privacy-framework)
- [ISO/IEC 23894:2023 — AI risk management](https://www.iso.org/standard/77304.html)
- [European Commission AI Act overview](https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai)
- [OWASP Agentic AI threats and mitigations](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/)
- [OWASP Securing Agentic Applications Guide](https://genai.owasp.org/resource/securing-agentic-applications-guide-1-0/)

## Notes

- PydanticAI Harness and multi-agent APIs may change while on `0.x` releases.
  Pin dependencies and treat upgrades as behavioral changes.
- A2A, MCP, OpenTelemetry, and other protocol support does not remove the need
  for application-specific authorization, data governance, risk assessment,
  evaluation, and operational controls.
- NIST describes the AI RMF as voluntary guidance. Organizations remain
  responsible for applicable legal, regulatory, contractual, and internal
  requirements.
