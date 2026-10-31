# Zetra developer skills

Status: implementation of the developer skill pack approved in the design conversation.
The wider platform decisions below remain milestones, not claims about current capabilities.

## Intent

A generated agent repository should immediately help Claude Code and Codex develop,
test, evaluate, debug, optimize, and prepare an agent release. Developers keep ordinary
editable Python, prompts, tools, and packaged resources. Skills explain Zetra-specific
decisions; deterministic tooling and CI establish results and enforce requirements.

## Approach

Use one repository-local pack, delivered through the existing canonical template and
SDK bundling path. This works without a global plugin installation and pins guidance
alongside the application. A centrally installed plugin could simplify fleet-wide
updates but would make guidance depend on developer configuration. Separate manually
maintained Claude/Codex packs would drift. Shared content with generated host copies
is the selected implementation approach.

For this first delivery, canonical skill content lives in zetra-template/template
under `.agents/skills/zetra-*`; `.claude/skills/zetra-*` is a generated mirror. Zetra's
existing `scripts/sync_template.py` imports the whole committed template into the SDK.
This follows the existing repository ownership boundary. A later SDK-managed pack
upgrade command may replace the small local synchronizer without changing the format.

## Contract

- Seven automatically discoverable skills: zetra-develop-agent, zetra-author-tool,
  zetra-author-skill, zetra-evaluate, zetra-debug, zetra-optimize, zetra-release-review.
- Shared development documentation supplies current commands and capability limits.
  Short AGENTS.md and CLAUDE.md files point to it. No permission-bypass settings,
  automatic hooks, global installation, or extra runtime dependencies.
- A versioned JSON inventory records managed skill files and SHA-256 digests. These
  detect drift; they are not signatures or a trust boundary. Check mode is read-only.
- Explicit synchronization copies canonical content to the Claude locations. Refuse
  conflicting local mirror edits and unsafe paths before writing anything. Preserve
  unrelated skills. Do not support automatic deletion or arbitrary remote upgrades.
- Current SDK Git pin and lockfile stay unchanged. Skills must not invent `zetra dev`,
  local Temporal switches, trajectory graders, or policy-enforcement APIs.
- The fixture model runs trusted host code and returns deterministic results. Passing
  fixtures establishes neither model accuracy, sandbox isolation, nor release approval.
- Agent-level, tool, runtime-skill, trajectory, safety, recovery, and efficiency coverage
  are described conditionally. Missing current infrastructure is an explicit gap;
  prose is not an implemented mandatory quality gate.
- Developer skills stay outside application wheels; runtime skills belong with package
  resources and require explicit loading, packaging tests, and behavioral evaluation.
- No cluster, production credentials, external MCP access, publication, or deployment
  is required to validate the pack. Installation may need dependency-download access.

## Skill behavior

Develop: inspect the indexed factory, manifest, prompt and tests; implement the requested
change in the current API; verify the selected agent and report the evidence scope.

Tool: constrain inputs, declare the real effect, bound outputs/errors, test rejection and
failure paths. Unsupported external writes remain unsupported rather than relabeled reads.

Runtime skill: distinguish developer discovery from runtime loading; define activation,
nonactivation, dependencies and outcomes, package the resource, and exercise its consumer.

Evaluate: map requirements to cases and graders; separate outcome from trajectory checks;
prefer partial-order invariants to one exact golden path; keep holdouts independent;
report errors/skips/unscored cases separately from measured accuracy.

Debug: reproduce a failure without credentials, preserve unknown outcomes, identify the
first bad observable boundary, and add a regression case without weakening the contract.

Optimize: compare identical datasets, model settings and runner modes with repeated
trials; measure actual tokens/latency/quality and reject unsupported improvement claims.

Release review: inventory exact source/artifact/test/eval evidence, distinguish local
preparation from production admission, and report blockers without manufacturing passes.

## Validation

Exercise the synchronizer through subprocess tests against disposable repositories:
missing mirror, canonical updates, conflicting local edits, unrelated skills, malformed
inventory, unsafe paths and symlinks. Render through both supported paths, run the
generated project's tests, verify pack checks, build a wheel, and check its contents.
Run existing canonical-template and SDK suites where dependencies permit.

Behavioral scenarios for each skill are maintained separately from runtime evals. Record
assistant/model/version, task, inspected artifacts, actions, output, and limitations.
Baseline and guided samples are smoke observations, not an efficacy benchmark. Both
Claude Code and Codex need repeated isolated trials before claiming measured uplift.

## Wider platform roadmap

1. **Local agent core and starter:** unify the existing SDK/template and active platform
   factory contracts; default no-Temporal execution, optional local Temporal, explicit
   platform adapter. Shared prompt/tool/resource assembly and typed tool contracts.
2. **Quality contract and evaluator:** CLI explains requirements derived from capabilities
   and risk; CI independently enforces mandatory component, outcome, trajectory, security,
   recovery and performance coverage. Developer-owned domain criteria can strengthen it.
   Skips, missing cases and unscored errors cannot inflate passing scores. Required
   Temporal lanes fail when their server is unavailable. Include repeated trials,
   calibrated graders, held-out data, dataset lineage and baseline comparisons.
3. **Release evidence and policy:** evaluate the exact candidate digest, bind results to
   source/model/tool/skill/policy versions, enforce reviewed capability intersections,
   and promote via GitOps with independent approval, provenance and rollback evidence.
4. **Data-driven improvement:** redacted traces, reviewed failure-to-regression cases,
   representative online labels, drift detection, shadow/canary comparisons and explicit
   cost/latency/quality budgets. Infrastructure investigation and general user-facing
   agents are equal reference workloads.
5. **zetra-ui:** separate repository; responsive near-black/lime design; fleet and detailed
   agent quality, skills/tools, release evidence and run pages. Show online correctness
   coverage separately from runtime success and offline evaluation. Agent creation,
   management and release definitions remain in code. No New Agent button. UI approval
   actions require a separately specified authority/audit contract; initial screens
   display evidence and state. The accepted mockups remain the visual reference.

Each milestone needs its own detailed implementation plan and end-to-end acceptance
tests. Shipping this pack does not implement those later runtime or platform features.
