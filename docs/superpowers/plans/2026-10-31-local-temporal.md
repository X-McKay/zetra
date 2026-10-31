# Local Temporal and starter rollout implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement sequentially, with a final independent review.

**Goal:** Activate the published quality workflow and run the shared agent core through local Temporal boundaries.
**Architecture:** Canonical pin/lock rollout; development-only router and cold host children reuse SessionWorkflow/Activities/Runtime.
**Tech stack:** Python 3.12, pinned Temporal SDK 1.34.0, Linux flock/prctl, Temporal CLI 1.9.1.
**Spec:** ../specs/2026-10-31-local-temporal-design.md

## Constraints and review focus

- Never weaken production Release/OpenShell admission or replay/unknown-outcome rules.
- No synthetic image release for local code. No whole-agent execute Activity.
- Keep leases/tombstones persistent; late children and stale generations cannot run after cleanup.
- Never import agent code in the worker. Ambient host credentials/permissions remain a trusted-development boundary.
- Verify pinned source/config on each child; failed source checks and missing evidence cannot pass.
- Retain dependency reachability and validate fresh installed wheels, not just PYTHONPATH.

## Task 1: Activate published starter

Files: canonical template pin/lock, evals, Justfile/workflows, docs, developer skills; SDK bundled tree/provenance.
Interface: generated projects install exact SDK e9bdc66 and execute quality before build.
- [x] Publish reviewed branches and verify SDK commit through GitHub API.
- [x] Activate cases/contract, update guidance, regenerate lock and mirrored skills.
- [x] Fresh installed starter validation: 30 tests and all quality requirements, wheel checks.
- [x] Commit canonical update, sync SDK bundle, publish reviewable branches/PRs.

## Task 2: Cold host lifecycle and real Temporal adapter

Files: src/zetra/local_temporal.py, _runtime/local_host.py, testing/local_temporal.py,
_runtime/runtime.py optional objects_root; tests/sdk/test_local_temporal.py.
Interface: LocalTemporal(root, state_dir, agent, model_config); worker(client, task_queue),
submit(client, message, task_queue, session, workspace), result(client, exact_handle).
- [x] Write failing native-server tests for true model/tool Activities, accepted-prefix recovery, unknown result and replay.
- [x] Implement exact-start validation, shared artifact storage, lease/generation/terminal fences and child supervision.
- [x] Verify cancellation, source mutation, duplicate attempt, late-child/cleanup and wrong-handle/config regressions.
- [x] Verify fresh router and persistent server restart; no accepted effect repeated.

## Task 3: CLI, documentation and delivery

Files: cli.py, local development/security/conformance docs, tests/sdk/test_cli.py and local Temporal tests.
Interface: dev --temporal or --temporal-address, optional explicit binary/state; existing default stays no-Temporal.
- [x] Add failing CLI tests, implement owned local-server startup/shutdown and accepted-result reports.
- [x] Test installed wheel with a real local CLI server, full SDK suite, examples/lint/lock/docs.
- [x] Independent whole-change review, fix material findings with regressions, record failures and limits.
- [x] Commit/publish SDK, ensure starter pin exposes tested capabilities, sync provenance and verify the final installed bundle.

After publication, validate final-head remote CI and record its result on the
paired PRs; those checks necessarily run after the source and this plan are committed.

## Ledger

Pre-flight: starter pins a previously published SDK while the final SDK bundles the
new canonical tree; no self-referential commit pin. A later Temporal pin is a
separate reachable revision, validated before starter activation.
Ruling: publish feature branches and draft PRs; do not merge or tag — authorized
publication makes dependencies reachable while preserving review and commit IDs.
Ruling: cold per-boundary reconstruction and Linux in-process tools initially —
keeps the lifecycle small and testable; cost is import/replay overhead and no tool
subprocess support in this mode. Ordinary no-Temporal execution is unchanged.

Independent review: two lifecycle findings reproduced before fixes. SIGTERM now
cancels the Workflow, retains its report and stops the owned server; source
integrity errors preserve unknown/canceled outcomes in both runners. Each failing
regression passed after correction. Startup cancellation also has a real-server
regression. No additional review round is needed for this fix pass.

Delivery: published SDK c128bb199c64386e2fbda224e500ef40451eadcb passed the full
native-enabled suite (418 passed, 46 skipped). Canonical starter
e7417ee595d89e1d3093531dfe9ba93320919eea pins it; fresh installed validation
passed 30 tests, six quality requirements and both default/Temporal runs with
confirmed Temporal cleanup. Other locked dependencies and the factory stayed
unchanged. Final bundle/provenance and remote checks complete the delivery step.
