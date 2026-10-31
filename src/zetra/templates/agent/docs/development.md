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
| Deterministic fixture evaluation | `uv run --locked zetra eval . --agent document_reader --mode fixture` |
| Build application wheel | `uv build --wheel` |
| Verify wheel resources and dependency pin | `uv run --locked python tests/packaging/check_wheel.py dist/*.whl` |
| All starter checks | `just validate` |

Replace `document_reader` with an indexed agent name when appropriate. Static
checks do not import factories. Tests and fixture evaluation execute trusted
project Python on the host. The fixture model deterministically calls a tool and
returns `Completed.`; adding natural-language cases does not make it a real model.
Current JSONL cases support `id`, `input`, `contains`, and `files`. They do not
provide built-in trajectory grading, runtime-skill activation metrics, or statistical
model-quality evaluation. Use explicit tests for observable behavior that the
current fixture cannot score and identify missing model-based evidence separately.

## Local execution and boundaries

Unit tests, pack checks, static checks and fixture evaluation require no cluster
or Temporal connection. Dependency installation may need network access. Genuine
model evaluation needs a configured provider; a fully offline model requires a
supported local provider and downloaded weights. No-Temporal live inference and
a turnkey local-Temporal runner are planned platform work, not commands supplied
by this pinned starter. Missing live infrastructure is not permission to relabel
fixture results or silently change the execution mode.

The pinned SDK admits only `workspace_read` and `workspace_write` tool effects.
It rejects `remote_read` as well as remote writes; newer SDK checkouts may have
different capabilities. Remote/MCP tools require a separately reviewed SDK upgrade,
compatible lockfile and runtime qualification before use in this starter. Arbitrary
background processes and parallel tools are also outside its admitted profile.
A prompt or Python annotation is not authorization or sandbox enforcement. Any
future remote integration needs explicit transport, identity, resource scope and
deployment policy. Do not acquire production credentials or change a global kube
context to make a local task run.

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
Developer-defined domain criteria complement platform requirements; this starter
does not yet implement the proposed mandatory cross-component quality contract.

See [the prepared quality upgrade](quality-upgrade/README.md) for an inactive V2
fixture case, tool-test contract and disposable candidate rehearsal. Enable
`zetra quality` in normal CI only after a reachable published SDK revision is
selected, the Git pin and lockfile are updated together, and rendered validation
passes. The prepared files do not change this starter's active legacy dataset;
local Temporal remains separate.

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
