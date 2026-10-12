# agentctl

`agentctl` scaffolds agent and multi-agent system packages, validates agent, system and risk contracts, validates Agent Skills, renders composition graphs, runs version-pinned eval adapters, and applies fail-closed release gates.

It moved here from the [Playbooks](https://github.com/X-McKay/playbooks) when that repository became technology-neutral. It is kept as a separate uv project with its own lockfile because it needs Python 3.12 or newer, while the `zetra` package supports 3.11. Its unique commands (System Specs, risk contracts, skill validation) are planned to fold into the `zetra` CLI; until then, both tools exist side by side.

## Install or run locally

From the Zetra repository root:

```bash
just agentctl --help
# or directly
uv run --project tools/agentctl --locked agentctl --help
```

Install it into an isolated environment when using it from another repository:

```bash
uv tool install ./tools/agentctl
agentctl --help
```

`just agentctl-check` runs its lint, format and unit tests, and validates the skills in `skills/`.

## Terminal experience

Interactive terminals receive styled help, semantic colors, responsive tables and panels, and unobtrusive spinners around longer phases. Redirected output automatically drops animation and ANSI styling. JSON modes remain machine-readable.

```bash
agentctl --help
agentctl --no-color --no-animations validate
agentctl --force-color skills validate skills
agentctl validate --format json
```

The standard `NO_COLOR`, `FORCE_COLOR`, `CI`, and `TERM=dumb` conventions are respected.

## Scaffold an agent

```bash
scripts/agentctl scaffold customer-support \
  --root /path/to/project \
  --package acme_agents \
  --owner customer-platform \
  --execution-class durable \
  --risk-tier medium
```

Durable and human-governed agents automatically receive workflow and activity modules. Human-governed agents also receive an approval policy. Medium-, high-, and critical-risk agents receive a threat-model template.

Scaffolding never overwrites an existing file unless `--force` is explicitly supplied.
The generated package is intentionally incomplete: replace the purpose and ownership placeholders, implement the agent and eval adapter, and establish an approved baseline before expecting validation and release checks to pass.

## Validate contracts

```bash
scripts/agentctl validate --root /path/to/project
scripts/agentctl validate --root /path/to/project --format json
```

Validation covers required Agent Spec metadata, semantic versions, execution and risk classes, budgets, Temporal structure, approval policy, threat-model presence, eval policy, capability existence, tool effect, retry safety, authorization declarations, timeouts, output bounds, and consistency with the referenced risk assessment.

## Scaffold and validate a multi-agent system

Scaffold conformant member agents first, then compose their pinned Agent Specs:

```bash
scripts/agentctl system scaffold research-system \
  --root /path/to/project \
  --package acme_agents \
  --owner ai-platform \
  --operational-owner research-operations \
  --execution-class durable \
  --risk-tier medium \
  --topology supervisor_worker \
  --member planner-agent:coordinator \
  --member researcher-agent:worker
```

The scaffold creates a System Spec, deterministic policy boundaries, delegation,
data-flow and termination policies, a system risk assessment, threat model,
system eval adapter, and Temporal workflow/activity boundaries when required.
Use `--dynamic-membership` to also create registry and admission policies, and
`--recursive-delegation` only when bounded recursive decomposition is intended.

```bash
scripts/agentctl system validate --root /path/to/project
scripts/agentctl system explain \
  src/acme_agents/systems/research_system/system.yaml
scripts/agentctl system graph \
  src/acme_agents/systems/research_system/system.yaml --format dot
```

System validation applies the packaged Draft 2020-12 schema and semantic checks
for owners, topology, dynamic admission, pinned member versions, execution and
governance floors, member restrictions, declared edges, referenced policies,
system limits, Temporal structure, and cross-artifact system risk consistency.
Graph output supports human-readable text, JSON, and Graphviz DOT.

## Validate and explain risk assessments

Risk assessments are governed by the packaged Draft 2020-12 JSON Schema and
semantic checks for the risk matrix, classification summaries, governance
floors, prohibited uses, control evidence, identifiers, and dates.

```bash
# Discover assessments under docs/risk-assessments.
scripts/agentctl risk validate --root /path/to/project

# Validate one file or recursively validate a directory.
scripts/agentctl risk validate docs/risk-assessments/customer-support.yaml
scripts/agentctl risk validate docs/risk-assessments --format json

# Show a human-readable profile or a stable JSON envelope.
scripts/agentctl risk explain docs/risk-assessments/customer-support.yaml
scripts/agentctl risk explain docs/risk-assessments/customer-support.yaml --format json
```

Schema and semantic errors exit `1`. Draft status and unresolved questions are
reported as warnings and do not make the standalone risk command fail. Normal
Agent Spec validation also validates its referenced assessment and enforces
matching agent name, version, execution class, and governance tier.

## Validate Agent Skills

```bash
scripts/agentctl skills validate --root /path/to/project
scripts/agentctl skills validate skills/build-agent-evals
scripts/agentctl skills validate --run-skills-ref
```

The built-in validator checks frontmatter, naming, description and body constraints, line limits, internal references, duplicate names, and optional `agents/openai.yaml`. `--run-skills-ref` additionally calls the separately installed Agent Skills reference validator.

## Run evals

```bash
scripts/agentctl eval run customer-support --root /path/to/project
scripts/agentctl eval run research-system --system --root /path/to/project
```

By default, agent evaluation runs `evals/customer_support/run.py`; system
evaluation runs `evals/systems/research_system/run.py`. To wrap a version-pinned
Pydantic Evals command:

```bash
scripts/agentctl eval run customer-support \
  --root /path/to/project \
  --report artifacts/evals/customer-support.json \
  --command uv run python -m evals.customer_support.run
```

The adapter receives:

- `AGENTCTL_AGENT`
- `AGENTCTL_REPORT_PATH`
- `AGENTCTL_SUBJECT_KIND`
- `AGENTCTL_SUBJECT_NAME`

`AGENTCTL_AGENT` remains available for agent adapters. New reports may identify
either kind of subject:

```json
{
  "schema_version": 1,
  "subject": {"kind": "system", "name": "research-system"},
  "hard_gates": {"critical_system_risks_pass": true},
  "metrics": {"task_success_rate": 0.94}
}
```

The legacy agent-only report shape remains supported and uses this normalized
form:

```json
{
  "schema_version": 1,
  "agent": "customer-support",
  "hard_gates": {
    "critical_safety_pass_rate": 1.0,
    "schema_validity_rate": 1.0,
    "unauthorized_tool_calls": 0,
    "unapproved_consequential_actions": 0
  },
  "metrics": {
    "task_success_rate": 0.94,
    "p95_latency_seconds": 8.2,
    "average_cost_usd": 0.08
  }
}
```

`agentctl` validates the report and adds timestamps, the invoked command, and the Git commit. This adapter boundary lets each agent pin its Pydantic Evals and provider dependencies independently.

The eval adapter should add the remaining release provenance required by its policy, such as dataset and evaluator versions, exact model and settings, prompt, skill and toolset versions, manifest identifier, and run count.

The generated adapter deliberately reports a failing `placeholder_eval_removed` gate. It must be replaced with real datasets and evaluators before release.

## Apply release gates

```bash
scripts/agentctl release check \
  --report artifacts/evals/customer-support.json \
  --policy evals/customer_support/release-policy.yaml \
  --baseline evals/customer_support/baselines/main.json
```

Policies support:

- Exact hard-gate values
- Absolute numeric minimums and maximums
- Direction-aware regression limits against a baseline
- Required provenance fields

Missing gates or metrics fail closed. When a policy defines regressions, an approved baseline is required; omitting it also fails the release check.

## CI example

```yaml
- name: Validate agent contracts and skills
  run: |
    uv sync --locked
    uv run --locked agentctl --no-animations validate
    uv run --locked agentctl --no-animations risk validate
    uv run --locked agentctl --no-animations skills validate

- name: Run smoke evals and release gates
  run: |
    uv run --locked agentctl --no-animations eval run customer-support
    uv run --locked agentctl --no-animations release check \
      --report artifacts/evals/customer-support.json \
      --policy evals/customer_support/release-policy.yaml \
      --baseline evals/customer_support/baselines/main.json
```

## Exit codes

- `0`: checks passed or scaffolding completed.
- `1`: conformance or release-gate failures.
- `2`: invalid invocation, unreadable input, or unsafe overwrite attempt.

## References

- [Core concepts](https://github.com/X-McKay/playbooks/blob/main/core/README.md)
- [Advanced concepts](https://github.com/X-McKay/playbooks/blob/main/advanced/README.md)
- [Multi-agent systems](https://github.com/X-McKay/playbooks/blob/main/multi-agent/README.md)
- [PydanticAI Agent Specs](https://pydantic.dev/docs/ai/core-concepts/agent-spec/)
- [Pydantic Evals quickstart](https://pydantic.dev/docs/ai/evals/getting-started/quick-start/)
- [PydanticAI Temporal integration](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/)
- [Agent Skills specification](https://agentskills.io/specification)
