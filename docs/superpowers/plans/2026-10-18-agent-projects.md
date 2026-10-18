# Simple agent projects implementation plan

**Goal:** Make the approved nested agent project structure runnable through the public SDK, with one canonical template and no agent-owned lifecycle engine.

**Approved design:** A project contains pyproject.toml, uv.lock, a Justfile, README/AGENTS, thin CI workflows, src/<package>/agents/<agent>/{agent.yaml,factory.py,instructions/base.md,tools/}, agent-specific tests and evals. Extra dependency/model/toolset/runtime/platform layers remain optional. Temporal remains the sole progress authority; OpenShell owns sandbox enforcement.

**Execution:** The user explicitly authorized implementation, Sol review, full qualification and PR publication. Implement in the existing dedicated SDK clone/feature branch and a dedicated template clone/feature branch. Keep a progress ledger under .tools/project-layout. No additional approval gate is needed for the approved work.

## Constraints and interfaces

- Python 3.12; existing pinned ADK/Temporal/OpenShell versions.
- `[tool.zetra.agents]` maps names to nested manifest paths. `load_agent(root, agent=None)` uses this index without importing Python. One entry is inferred; multiple require selection. No root-manifest fallback.
- `AgentSpec.instructions` is an optional package resource reference `package.module:instructions/base.md`. SDK resolves it inside sandbox before calling factory; `ctx.instruction` is static text. No arbitrary file/network reads from factories.
- CLI check/build/release/eval take optional `--agent`. Evaluation cases live at evals/<agent>/cases.jsonl. Runtime inputs/effects remain unchanged.
- Source digest and snapshot use the same exclusions: generated artifacts, state, caches, VCS, virtualenvs, build outputs. Excluded directories are not release inputs; runtime source/resources/manifests and lockfile are.
- Agent build installs its own locked environment via uv sync --locked --no-dev --no-editable, with immutable public Git SDK revision. No injection of current SDK source. Release records lock digest and SDK revision. Published-wheel pins can be added separately; this repository currently uses Git.
- Canonical starter is maintained in zetra-template; SDK bundle is generated from it and verified by digest. Rendering substitutes only known tokens, supports a qualified SDK revision and preserves dependency pins while updating project metadata. Validate generated lock before release.
- No automatic external actions, skills execution, custom workflows, platform deployment, attestation/promotion claims or permissive policy compilation.
- Preserve one-attempt effects, unknown outcomes, exact execution handles, fresh workspaces and confirmed deletion.

## Review focus

Static discovery must reject ambiguous selection, duplicate/escaping/symlinked paths and agent name mismatch without importing factories. Packaged instruction changes must affect release identity and missing resources must fail. Output generation must not mutate source identity. Lock disagreement or mutable SDK revision must fail before image publication. Template generation, installed wheel and kind image must exercise compatible actual SDK code.

## Tasks

1. **Project discovery and packaged instructions** — add focused failing tests; implement manifest selection/resource validation, static context instructions, per-agent eval/CLI selection; migrate both examples to one contract, keeping reporter behavior unchanged. Verify targeted SDK and fixture tests.
2. **Locked image builds and source boundaries** — share snapshot/hash inclusion logic, add lock identity to release, build from agent lock only. Test ignored outputs, unsafe paths, missing/stale locks, mutable SDK pins and resource packaging. Preserve SDK lab image separately.
3. **Canonical template** — replace template primitives with real document reader, shared SDK bundle generation/check, root tooling/CI/docs, tests/evals and lock. Keep optional features documented rather than empty. Add offline generated-tree/installed-resource tests. Commit SDK implementation and pin template/example locks to its published branch commit before image qualification.
4. **Review and qualify** — independent Sol correctness/simplification review; fix meaningful findings with focused regressions. Run locked SDK local gate, template checks, wheel outside checkout, rebuild all images, real kind/live vLLM suite, latency and provisioning benchmarks, and confirm no session resources remain. Bind evidence to actual installed SDK runtime source and locked revision; retain expected red runs and incidental failures.
5. **Publish** — update existing SDK PR #5 and open template PR, attach both, cross-link exact dependency commits, wait for CI. Do not merge or publish a package/platform deployment.
