# Runtime unification implementation plan

> **For agentic workers:** Use superpowers:executing-plans in this session, with one independent whole-change review before delivery.

**Goal:** Run the same packaged SDK factories locally and through the versioned platform transport, with offline fixtures and strict accepted-history semantics.
**Architecture:** Reuse the SDK core; inject trusted platform bindings; add a new guest protocol and an explicitly selected workflow branch. Preserve the legacy protocol and history path.
**Tech Stack:** Python 3.12, pinned ADK/OpenAI/Temporal, existing platform gateway and Kata transport, pytest and real local Temporal.
**Spec:** ../specs/2026-10-31-runtime-unification-design.md

## Global constraints

- Exactly one authoring API and core for new agents; no extracted-tool second loop.
- New profile `zetra-core-v1`, 1 MiB core control bound, 64 effects maximum.
- Exact accepted Position comes from Temporal, never a guest latest/candidate pointer.
- Unknown execute or failed post-effect checkpoint never authorizes redispatch.
- Agent model/MCP access uses fixed gateway host/CA/auth; no unbound fallback.
- Legacy release policy digests, old Activity ordering and immutable images remain usable.
- SDK dependencies must be published and reachable before downstream lock/pin activation.
- No cluster stages, merge, tag, signing keys or production credentials changed.

## Review focus

- Wrong release/session/run/generation and task cannot reuse checkpoint or execute admission.
- A lost response, late process or corrupt snapshot cannot select a candidate as accepted.
- Explicit 403/429 refusals stay terminal; arbitrary tool/transport errors stay unknown.
- Drift between packaged definition, platform envelope, policy and actual tool inventory fails closed.
- Actual installed factories, prompts and dependencies work without checkout path substitution.

### Task 1: Shared bindings and offline MCP development

**Files:** src/zetra/agent.py; _runtime/definition.py, chat_model.py, dispatch.py, artifacts.py, activities.py; new bindings.py; local.py, local_temporal.py, evaluation.py, quality.py, cli.py and testing/local_temporal.py; focused tests/sdk and tests/provider tests.
**Interfaces:** Definition(..., mcp_binding=None, required_tools=None); AgentContext.mcp(backend, tool, arguments); MCPFixtures validated mapping/bind/provenance; ChatExecutor internal client_factory; shared Tape.validate_receipt(previous, operation, receipt).
- [x] Write failing tests for MCP scope/unbound/changed inventory, strict fixtures/provenance across local and Temporal, injected transport and receipt mismatch.
- [x] Implement context-scoped MCP execution, explicit fixture plumbing and strict reusable receipt validation.
- [x] Verify default local behavior, quality accounting, credential non-disclosure and existing provider/capture regressions.
- [x] Record results and commit SDK prerequisites; publish only when verified so platform dependencies can pin the exact revision.

### Task 2: Core guest protocol and authenticated model adapter

**Files:** platform/agent-runtime/agent_runtime/core_protocol.py (strict protocol primitives); platform/agent-runtime/agent_runtime/core_step.py and core_model.py; gateway.py; pyproject/lock; tests/test_core_step.py and test_core_model.py.
**Interfaces:** versioned release identity and prepare/execute commands; `core_step` length-prefixed subprocess; replies carry operation/done or candidate Receipt. Objects live under state/core/objects, lease and immutable attempt admission under state/core.
- [ ] Write failing real subprocess tests for model/tool/model, cold reconstruction, wrong identity/operation, duplicate attempts and arbitrary tool failure.
- [ ] Implement strict packaged definition envelope loader, trusted pod binding, shared Runtime adapter and bounded sanitized output.
- [ ] Inject authenticated async gateway transport into ChatExecutor; test Host, route, auth refresh, redirect refusal, CA settings, 403/429 and transport loss.
- [ ] Verify no candidate is passed as accepted, no unknown effect repeats, and legacy step tests remain unchanged.

### Task 3: Versioned platform runner and checkpoint validation

**Files:** platform/runner/runner/core.py (activities and archive validation), workflow.py, activities.py, types.py, worker.py, pyproject/lock; contracts bundle/podtemplate; AgentRelease CRD in SDK and Kubani; new protocol/workflow/contract tests.
**Interfaces:** preflight returns null for legacy or immutable core configuration for core releases; prepare_core, execute_core, snapshot_core Activities; snapshot returns verified Position plus archive digest and effect metadata.
- [ ] Write failing tests for explicit profile binding, stable legacy digest, immutable CRD profile and pod environment identity.
- [ ] Add nonlegacy runtimeProfile to signed policy compilation and release admission without changing legacy policy outputs.
- [ ] Implement versioned workflow branch, effect-free completion, strictly one execute attempt and post-snapshot acceptance.
- [ ] Validate full receipt/tape/workspace and bounded link-free archive before immutable upload; test corruption, path attacks, source/candidate mismatches and token exclusion.
- [ ] Test accepted-prefix replacement, execute loss, snapshot retry/exhaustion, cancellation and cleanup; replay complete legacy and core histories with a real server.

### Task 4: Reference agents and release integration

**Files:** isolated zetra-agents worktree: investigator factory/packaging/manifest/tests/evals; new document assistant package; tools/zetra_agents/layout.py and release.py; CI/justfile/Dockerfiles/docs. Canonical template follow-up if new SDK guidance/pins required.
**Interfaces:** schema-2 platform envelope references a packaged SDK AgentSpec; project index targets same resource. Release builder derives runtimeProfile, hashes envelope and emits unapproved artifacts. Both references own explicit quality contracts and local fixtures.
- [ ] Write negative static/packaging/release tests before migration: missing/mismatched resources, model/tool drift, unsupported profile, imports and approval.
- [ ] Migrate investigator without expanding MCP capabilities; add document assistant with path/abstention/security/recovery and trajectory coverage.
- [ ] Bind published SDK/runtime/contracts revisions, regenerate locks together and verify actual installed distributions.
- [ ] Run offline dev/quality and real local Temporal for both; platform subprocess/gateway fixture parity and image/wheel import checks.

### Task 5: Documentation, review and delivery

**Files:** SDK docs/platform.md, developer-skills.md, local-development.md, architecture/conformance, evidence; agent README; canonical guidance and provenance; PR descriptions.
- [ ] Correct current-vs-historical status and pins, document explicit legacy migration and remaining cluster/model qualification.
- [ ] Run affected full suites, native recovery/replay, static checks and installed bundle validations; retain failed observations.
- [ ] Obtain one independent whole-change review, fix material findings with RED/GREEN regressions and appropriate suites.
- [ ] Publish isolated feature branches and draft PRs, verify exact remote pins and final CI, record remaining milestone boundaries honestly.

## Ledger

Base: SDK 7d5703c2b14c5e2c8d3225df7b8a6c4c5649f810 on codex/runtime-unification;
agent repo a4177fd on isolated codex/runtime-unification. Prior SDK validation was
418 passed/46 skipped with native Temporal enabled; final bundle 18 tests and hosted
SDK/platform workflows passed. Original main checkouts remain unchanged.

Ruling: user authorization to proceed covers this next dependency and routine design
choices. Keep separate roadmap milestones visible; do not claim the UI/statistical
quality/production rollout is delivered by core unification.

Ruling: versioned core profile preserves old immutable snapshots/history and adopts
SDK no-redispatch behavior for migrated releases. A transparent alias cannot preserve
both existing engines' different error/recovery contracts.

Pre-flight: Task1 binding, receipt and converter seams are consumed by Tasks2/3;
Task2 guest protocol is consumed by Task3 and Task4 image checks. The platform envelope
references the canonical SDK definition rather than converting model dictionaries
into a synthetic SDK AgentSpec. Reachable prerequisite pins are a sequential dependency.

Builder check resolved: the full core execution/task binding is runner-controlled pod
configuration and checked during adoption and guest commands, not learned from stdin.
Original-run guards reject resets/retries/continuations before activation. Typed model
refusals terminate without acceptance; tool refusals preserve uncertainty and a separate
reason. No additional user choice was needed for these safety-preserving details.

Ruling: the platform wire protocol belongs in agent_runtime.core_protocol, not in
the neutral SDK. Publish the SDK binding prerequisite first, then the adapter;
the runner consumes the pinned adapter protocol. This avoids a self-referential
Git pin and keeps the core independent of platform transport details.
