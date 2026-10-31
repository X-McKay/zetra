# Developing agents with Claude Code and Codex

This repository includes the same Zetra developer skills for both assistants.
Codex discovers `.agents/skills`; Claude Code discovers `.claude/skills`. Open the
repository in your assistant and describe the change, or explicitly invoke a
skill (for example, `$zetra-evaluate` in Codex or `/zetra-evaluate` in Claude Code).
The CLI and actual test results determine what has passed; skill instructions do
not enforce policy or grant permission to deploy.

## Start with the installed contract

Read `[tool.zetra.agents]` in `pyproject.toml` to locate the selected manifest.
Follow its entrypoint and packaged instructions. Keep the ordinary Python factory
editable and use its current SDK API; the starter is a Google ADK `LlmAgent` using
`ctx.model`, `ctx.tools`, and `ctx.instruction`. Do not mix this API with a different
runtime merely because it also uses a `create_agent` name.

Run commands from the project root after `uv sync --locked`. The starter currently
pins an exact SDK Git revision; retain that pin and the lockfile when changing an
agent. Use `uv run --locked zetra --help` and subcommand help for version-specific
options. A command absent from that installed help is a missing capability.

| Purpose | Current command |
| --- | --- |
| Validate developer skill copies | `python scripts/developer_skills.py --check` |
| Lint / format check | `uv run --locked ruff check .` / `uv run --locked ruff format --check .` |
| Unit and packaging tests | `uv run --locked pytest` |
| Static selected-agent metadata | `uv run --locked zetra check . --agent document_reader` |
| Explain quality requirement coverage | `uv run --locked zetra quality . --agent document_reader --explain` |
| Execute quality gate (mapped tests and fixture eval) | `uv run --locked zetra quality . --agent document_reader` |
| Deterministic fixture evaluation (standalone diagnosis) | `uv run --locked zetra eval . --agent document_reader --mode fixture` |
| Trusted host local inference | `uv run --locked zetra dev . --agent document_reader --input "Read the guide" --model-config model.json` |
| Evaluate a configured local model | `uv run --locked zetra eval . --agent document_reader --mode local --model-config model.json` |
| Build application wheel | `uv build --wheel` |
| Verify wheel resources and dependency pin | `uv run --locked python tests/packaging/check_wheel.py dist/*.whl` |
| All starter checks | `just validate` |

Replace `document_reader` with an indexed agent name when appropriate. Static
checks do not import factories. Tests and fixture evaluation execute trusted
project Python on the host. The fixture model deterministically calls a tool and
returns `Completed.`; adding natural-language cases does not make it a real model.
The active V2 JSONL dataset uses `schema_version: 2` and typed `assertions` for
final text, tool calls/arguments and bounded steps. `quality.json` maps each agent,
tool and declared runtime-skill requirement to case IDs or pytest selectors.
`zetra quality . --explain` checks coverage; execution runs fresh mapped tests and
evaluation and rejects missing or incomplete evidence. The normal gate already
runs fixture evaluation, so validation does not run a second standalone eval.
These assertions do not measure statistical model quality; use representative
live cases and explicit tests for properties the selected grader cannot score.

## Local execution and boundaries

Unit tests, pack checks, static checks and fixture evaluation require no cluster
or Temporal connection. Dependency installation may need network access. Genuine
model evaluation needs an explicitly configured endpoint and model; a fully offline
model requires a supported local endpoint and downloaded weights. `zetra dev`,
`eval --mode local` and `quality --mode local` run trusted project code without
Temporal or a cluster. Supply `--model-config model.json` with `base_url`, `model`
and optionally `api_key_env`, `max_tokens`, `timeout` and `temperature`. Keep secrets
in the named environment variable. Use the installed help for supported options.
Local Temporal remains separate platform work. Missing live infrastructure is not
permission to relabel fixture results or silently change execution mode.

The pinned SDK admits `workspace_read`, `workspace_write` and reviewed
`remote_read` tool effects. Remote writes, arbitrary background processes and
parallel tools remain outside its admitted profile. Admission does not install an
MCP transport or supply identity, resource scoping or deployment policy. Review and
implement those integrations explicitly, test accepted/rejected resource scopes,
and qualify the intended runtime. A prompt or Python annotation is not
authorization or sandbox enforcement. Do not acquire production credentials or
change a global kube context to make a local task run.

The separate qualification workflow requires its documented infrastructure and
explicit configuration. Retain `OUTCOME_UNKNOWN` and unconfirmed cleanup states.
Do not automatically repeat an operation whose external outcome is unknown.
Keep credentials, private prompts, and raw sensitive traces out of tracked data;
use synthetic or reviewed, redacted fixtures. Local `.zetra/` artifacts are ignored.

## Quality evidence

For a changed component, record the requirement, representative positive and
negative cases, failure cases, grader/assertion, runner mode, and evidence result.
Evaluate final outcomes and required trajectory properties independently. A
trajectory records observable model/tool events, arguments, results, artifacts,
and ordering constraints; it does not require private chain-of-thought. Allow
different valid paths unless a specific ordering is required for correctness.

Tool contracts need argument, authorization-scope, error, timeout and output-bound
tests. Runtime skills need loading/packaging, activation, nonactivation, conflicting
instructions, tool interactions and outcome tests. Add injection, data-disclosure,
recovery, escalation/abstention and efficiency cases where the capabilities make
them relevant. A skipped or unscored case is missing evidence, not a passing case.
Developer-defined domain criteria complement the active component quality
contract. Keep contract inventories and mapped tests/cases consistent with actual
capabilities; a declared runtime skill needs its required evidence mappings.

See [the quality contract reference](quality-upgrade/README.md) for the active
starter mappings, their limits and upgrade history. Both validation and release
workflows run `zetra quality .` before building. The current fixture contract sets
`live_model_required` to false; set it true when live evidence becomes a gate
requirement, then run `quality --mode local --model-config model.json` with
appropriate live assertions. The `Completed.` marker case is not a live
summarization grader.

Prefer deterministic assertions when they express the requirement. Calibrate model
graders against reviewed labels, keep held-out data separate from prompt tuning,
and report sample counts, failures/errors and repeat variability. Reports should
name the source revision, dataset, model/settings, runtime mode and grader version.
Do not change graders or lower thresholds merely to accept a candidate.

## Maintaining the pack

`developer-skills.json` records the pack version and managed file hashes. Canonical
content is in `.agents/skills/zetra-*`; Claude copies are generated. To update it,
review/edit canonical files, then run:

```sh
python scripts/developer_skills.py --sync
python scripts/developer_skills.py --check
git diff
```

Sync is local and explicit. It refuses conflicting mirror edits and symlinked
managed paths before writing, preserves other skill names, and does not download
upgrades or delete files. Reconcile conflicting edits into the canonical source
first; keep project-specific skills under distinct names such as `team-review`.
The inventory is a consistency check, not a signed security policy. When adding a
new managed file, add its canonical SHA-256 to the inventory; bump the pack version
for a published content change. Removing a skill requires an explicit reviewed
change to both copies and the inventory; sync does not perform removal.

Developer skills and this tooling stay outside `src/` and the application wheel.
Runtime skills are different: package their resources with the agent, define how
the factory loads them, and evaluate the resulting behavior. Merely placing a
runtime document under an assistant's discovery directory will not load it into
the deployed agent.

`tests/development/skill-scenarios.json` describes behavioral smoke tasks for pack
maintainers. Run them in fresh disposable repositories with each supported
assistant, with and without relevant skills, and record actual actions and outputs.
Use repeated trials before claiming effectiveness or cost improvements. These
scenarios are not the deployed agent's eval dataset and do not run paid assistants
as part of ordinary `just validate`.
