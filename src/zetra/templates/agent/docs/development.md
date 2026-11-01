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
No-Temporal execution remains the default. Optional local Temporal is described
below. Missing live infrastructure is not
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

## Optional local Temporal

On Linux, `dev` can run the shared Workflow and actual model/tool Activities with
an owned local Temporal server. With an installed CLI and fixture model, no cluster,
OpenShell or remote model endpoint is needed:

```sh
uv run --locked zetra dev . --input 'Read the approved guide.' --temporal \
  --temporal-binary /path/to/temporal \
  --temporal-state /outside/project/private-state
```

Without `--temporal-binary`, owned-server mode downloads pinned Temporal CLI 1.9.1
on first use. To use an existing server, replace the owned-server options with
`--temporal-address 127.0.0.1:7233 --temporal-state /outside/project/private-state`.
Only loopback servers are accepted. `--model-config`, `--workspace`, `--timeout`
and private `--output` export also work. Model request timeouts are capped at
30 seconds within the shared 45-second effect Activity deadline.

State must be outside the project and remains private and retained after exit:
SQLite history, immutable artifacts, execution fences, `last-handle.json` and
`last-report.json` can contain prompts and tool/file contents. Reports identify
`state_dir` and the exact namespace/Workflow/run handle. Use a new state directory
after source changes. Owned servers stop on normal completion or graceful
cancellation; caller-managed servers remain running. After forced termination,
inspect retained state and any surviving server. Do not delete live state as a
cancellation mechanism or retry an effect with an unknown outcome.

This is trusted host execution for cooperative in-process tools on Linux. Each
boundary runs in a fresh Python child using Temporal-accepted artifacts. Normal
process creation APIs are rejected; native syscall bypasses, hostile Python and
unmanaged background threads/processes are outside the supported profile. The
audit guard is not a sandbox. Local cleanup does not erase retained history and
is not production isolation evidence. `eval` and `quality` still use fixture or
explicit local-model modes; their reports do not certify Temporal orchestration.

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

## Offline MCP and the shared platform profile

The pinned SDK supplies `await ctx.mcp(backend, tool, arguments)` only within an
active registered `remote_read` tool. Calls from factory construction, a workspace
tool or an unmanaged background task fail. There is no implicit live endpoint.

For local tools, add a reviewed JSON fixture file with `schema_version: 1` and a
`calls` array. Each entry has `backend`, `tool`, exact `arguments`, and JSON `result`.
Pass that file explicitly to `zetra dev`, `zetra eval`, or `zetra quality` using
`--mcp-fixtures evals/<agent>/mcp.json`. The same option works with `dev --temporal`.
Unknown calls fail; reports identify the fixture digest independently of the model.
Synthetic MCP evidence does not establish live authorization or tool reliability.

The `zetra-agents` infrastructure investigator and document assistant demonstrate
schema-2 platform envelopes referencing the same packaged SDK definition indexed
in `pyproject.toml`. New core releases select `zetra-core-v1`; the platform supplies
model and MCP bindings and verifies the exact tool inventory. Keep development
fixtures separate from that explicit release profile. Existing legacy releases
need a new image and reviewed release; never relabel an old snapshot as core.

The platform migration and cluster qualification remain separately reviewed work.
This starter remains a small local-first Python project; it does not create a
cluster, provision identities, or approve a deployment.
