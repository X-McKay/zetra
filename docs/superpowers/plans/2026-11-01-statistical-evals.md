# Statistical and Live-Model Evals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Fixed-sample, bounded local live-model experiments with honest confidence and regression decisions, integrated into the standard developer workflow.
**Architecture:** Reuse the current local runtime/grader; separate strict experiment data, statistical summaries and comparison from default one-case evals. Extend quality with opt-in declared experiments.
**Tech Stack:** Python 3.12, existing Pydantic/ADK/pytest; standard-library statistics only.
**Spec:** docs/superpowers/specs/2026-11-01-statistical-evals-design.md

## Global Constraints

- No production qualification, deployment, cluster access or approval-policy changes.
- No fixture fallback, retry-to-pass, erased failures, repeated draws counted as new cases, hidden credentials or raw outputs in experiment reports.
- Default eval and quality remain compatible. Input mutation and missing required evidence fail closed.
- Only published exact SDK/template commits may be used for shared dependency pins.
- Real-model endpoint/model/budget is pending; implement independent work and never label synthetic HTTP tests real-model evidence.

## Review Focus

- Correlated cases/trials, all-pass small samples, repeated looks and multiple comparisons must not create unsupported confidence claims (Task 1/2).
- Interrupted/budget-exhausted schedules, unknown usage and malformed partial results must retain every planned trial (Task 2).
- Mutated inputs, extra/duplicate report fields and incompatible unsigned baselines must not yield a passing comparison (Task 2).
- A static quality explanation must never import factories; local/fixture/experiment combinations must not silently downgrade (Task 3).
- Live CI must never send secrets into untrusted PR code or upload raw prompts/tool results (Task 4).

### Task 1: Strict cases, experiments and statistical primitives

**Files:** src/zetra/eval_cases.py; new src/zetra/eval_statistics.py and src/zetra/eval_experiments.py configuration; tests/sdk/test_eval_cases.py, new tests/sdk/test_eval_statistics.py, tests/sdk/test_eval_experiments.py.
**Interfaces:** load_cases(root, agent, *, dataset='cases.jsonl'); grade(case,result) unchanged; wilson(successes,total,confidence); metric_summary(values); load_experiment(root,agent,name) returns validated plan/cases/raw identities.

- [ ] Write failing tests for v3 metadata, exact/absent-file/tool-result/token assertions; bool/nonfinite/missing evidence; duplicate/escaping/oversized plan/dataset; planned invocations and missing tags.
- [ ] Run targeted pytest; expected failures for absent interfaces/behavior.
- [ ] Implement strict validation and standard-library Wilson/metric summaries; verify known 5/10 95% bounds ~0.23659/0.76341, 0/n and n/n bounds nondegenerate, null metrics remain missing.
- [ ] Run affected tests; expected all pass. Commit task.

### Task 2: Repeated execution, budgets, evidence and baseline comparison

**Files:** src/zetra/evaluation.py, src/zetra/local.py, src/zetra/testing/local.py (if needed), new src/zetra/eval_reports.py and src/zetra/eval_comparison.py; tests/sdk/test_eval_experiments.py and test_eval_comparison.py, test_local.py.
**Interfaces:** async evaluate_case(...) shares existing execution/grading; async run_experiment(root, *, agent, experiment, model_config, mcp_fixtures=None); local_run(..., model_call_limit=None); compare_reports(baseline,candidate) returns recomputed decision.

- [ ] Write failing tests for fresh repeated state, complete row accounting, safe compact report, errors/no retry, request cap, token/deadline stops, cancellation and mutation; real HTTP/tool integration.
- [ ] Run tests; expected failure for missing behavior.
- [ ] Implement predeclared seeded schedule, invocation/time/request bounds, observed token stops, compact typed trial records, simultaneous per-case intervals and PASSED/FAILED/INCOMPLETE/INCONCLUSIVE decisions.
- [ ] Write failing comparison tests for worse cases, exact compatibility requirements, malformed/duplicate/forged summaries, missing metrics and small-sample inconclusive results.
- [ ] Implement bounded typed evidence validation and conservative baseline/candidate difference bounds; descriptive per-case metrics.
- [ ] Run targeted and full SDK non-cluster suites; expected green, record existing warnings. Commit task.

### Task 3: CLI and quality integration

**Files:** src/zetra/cli.py, src/zetra/quality.py; tests/sdk/test_cli.py (existing location discovered), test_quality.py and experiment CLI tests.
**Interfaces:** eval --experiment NAME; eval-compare BASELINE CANDIDATE; quality --experiment NAME with mode local. Quality v2 experiments list; explain validates plans data-only.

- [ ] Write failing tests for CLI exit status and private non-overwriting output, static explanation, missing plan coverage, invalid mode combinations, live-required handling and deterministic+live evidence separation.
- [ ] Implement dispatch and quality integration; keep v1 callers/tests compatible.
- [ ] Run full SDK just check; expected green. Commit prerequisite SDK changes and publish exact pin only after verification.

### Task 4: Reference suites, canonical starter and delivery

**Files:** zetra-agents agents/*/evals, pyproject/locks, scripts/workflows/docs; zetra-template template/evals, docs, scripts/just recipes/workflow, pins/lock; SDK bundled template/provenance, docs/local-development.md, docs/playbook-conformance.md, evidence.
**Interfaces:** references and rendered starter consume the published SDK commit; SDK rebundles canonical template commit without changing runtime profile/deployment.

- [ ] Add reviewed synthetic live cases with meaningful deterministic outcomes, trajectories, negative cases and skill/tool-result checks; smoke and reliability fixed-sample plans.
- [ ] Add validation tests/CI that reject missing or malformed plans. Manual trusted-branch live workflow uses only dedicated model credentials and selected known experiment; no signing/registry keys, no PR execution with secrets.
- [ ] Run reference full checks, template tests/rendered validation, bundle provenance/scaffold tests. Validate real HTTP transport and, if endpoint/budget provided, bounded actual model run; preserve actual failures.
- [ ] Update docs/developer guidance/evidence, publish pinned prerequisites and final draft PRs; no merges/deploys.
- [ ] One fresh whole-change review; fix material findings with failing regression tests then green affected/full suites. Verify final hosted CI and stop before qualification.
