# References

These sources informed the first draft. Framework behavior should be checked against the versions pinned by an implementation.

## PydanticAI

- [Agent Specs](https://pydantic.dev/docs/ai/core-concepts/agent-spec/) — declarative YAML/JSON agent configuration, templates, capabilities, schemas, retries, timeouts, instrumentation, and metadata.
- [Skills](https://pydantic.dev/docs/ai/harness/skills/) — Agent Skills format, deferred loading, allowlists, multiple libraries, and bundled-resource behavior.
- [Temporal durable execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/) — workflows, generated activities, stable names, serialization, payloads, streaming, activity configuration, retries, and telemetry.
- [Pydantic Evals](https://pydantic.dev/docs/ai/evals/evals/) — datasets, cases, evaluators, experiments, trajectories, and online evaluation.
- [Evaluator overview](https://pydantic.dev/docs/ai/evals/evaluators/overview/) — deterministic, custom, and LLM-based evaluators.
- [Span-based evaluation](https://pydantic.dev/docs/ai/evals/evaluators/span-based/) — evaluation using OpenTelemetry spans.
- [Agentic evaluators](https://pydantic.dev/docs/ai/evals/evaluators/agentic/) — tool correctness, argument correctness, and trajectory evaluation.
- [Online evaluation](https://pydantic.dev/docs/ai/evals/online-evaluation/) — sampled evaluation of production and staging traffic.
- [Multi-run evaluation](https://pydantic.dev/docs/ai/evals/how-to/multi-run/) — evaluating stochastic variability.
- [Observability and Logfire](https://pydantic.dev/docs/ai/integrations/logfire/) — PydanticAI instrumentation, OpenTelemetry, content controls, metrics, and alternative backends.
- [Usage API and limits](https://pydantic.dev/docs/ai/api/pydantic-ai/usage/) — request, tool, token, per-request context, and cost limits.
- [Spend limits](https://pydantic.dev/docs/ai/harness/spend/) — run, conversation, daily, monthly, scoped, and shared spend controls.
- [Compaction](https://pydantic.dev/docs/ai/harness/compaction/) — message-history management and compaction strategies.
- [Tool output limits](https://pydantic.dev/docs/ai/harness/tool-output-limits/) — truncate, spill, and summarize strategies.
- [Cache-bust monitoring](https://pydantic.dev/docs/ai/harness/warn-on-cache-busts/) — observing loss of prompt-cache effectiveness.

## Agent Skills clients and specification

- [Agent Skills specification](https://agentskills.io/specification) — portable package structure, frontmatter rules, progressive disclosure, bundled resources, examples, and validation.
- [Claude Code skills](https://code.claude.com/docs/en/skills) — project and personal discovery locations, invocation, and supporting files.
- [OpenAI skills guide](https://developers.openai.com/api/docs/guides/tools-skills) — portable skill packages, discovery, supporting resources, validation, and runtime safety.
- [OpenAI plugin skill authoring](https://developers.openai.com/plugins/build/skills) — skill resources, Codex metadata, and plugin packaging.

## Temporal

- [Python SDK developer guide](https://docs.temporal.io/develop/python)
- [Python SDK best practices](https://docs.temporal.io/develop/python/best-practices)
- [Durable AI](https://docs.temporal.io/ai)
- [Safe deployments](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning)

## Risk and security

- [NIST AI Risk Management Framework](https://airc.nist.gov/airmf-resources/airmf/)
- [NIST Generative AI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)
- [NIST Privacy Framework](https://www.nist.gov/privacy-framework/privacy-framework)
- [NIST Cybersecurity Framework 2.0](https://www.nist.gov/cyberframework)
- [ISO/IEC 23894:2023 — AI risk management](https://www.iso.org/standard/77304.html)
- [European Commission AI Act overview](https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai)
- [OWASP Agentic AI threats and mitigations](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/)
- [OWASP Securing Agentic Applications Guide](https://genai.owasp.org/resource/securing-agentic-applications-guide-1-0/)

## Notes

- PydanticAI Harness APIs may change while on `0.x` releases. Pin dependencies and treat upgrades as behavioral changes.
- NIST describes the AI RMF as voluntary guidance. Organizations remain responsible for applicable legal, regulatory, contractual, and internal requirements.
