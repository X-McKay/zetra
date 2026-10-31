# Local Execution and Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Run and evaluate the existing agent core locally, and fail an aggregate quality gate when required evidence is absent or unsuccessful.
**Architecture:** Reuse Runtime/Definition with explicit development model injection; add strict case/contract schemas and fresh pytest/evaluation execution.
**Tech Stack:** Python 3.12, pinned ADK/LiteLlm/OpenAI client, Pydantic, pytest, existing CLI/template pipeline.
**Spec:** ../specs/2026-10-31-local-quality-design.md

## Global Constraints

- No Temporal/OpenShell connection for dev, fixture or local evaluation.
- No changes to production model defaults, admission, retry or unknown-outcome rules.
- Static quality explanation never imports agent factories or test modules.
- Missing/skipped/error evidence fails; local reports are unsigned and do not authorize production.
- No unpublished SDK Git pin in the canonical starter.

## Review Focus

- Alternate provider settings accidentally entering production defaults (Task 1).
- Sensitive provider data or credentials leaking into reports (Task 1).
- Wrong arguments/order passing a plausible final-output case (Task 2).
- Skipped or missing pytest cases counted as successful coverage (Task 3).
- New tools or changed inputs escaping quality requirements (Task 3).

## Task 1: Shared no-Temporal runner

Files: src/zetra/local.py, src/zetra/testing/local.py, src/zetra/_runtime/definition.py,
src/zetra/_runtime/vllm.py, src/zetra/evaluation.py, src/zetra/cli.py;
tests/sdk/test_local.py, tests/provider/test_vllm.py.

Interfaces: `LocalModelConfig` strict model; `local_run(root, message, *, agent=None,
model_config=None, timeout=60, workspace=None, output=None)`; explicit Definition
model binding with unchanged default. JSON run report used by evaluation.

- [x] Write failing config/local-run tests, including synthetic HTTP tool selection,
  isolated same-name projects, no implicit live endpoint and unknown outcomes.
- [x] Extract the existing driver and reuse provider conversion/validation, add CLI dev.
- [x] Run focused tests and existing provider/recovery tests; verify explicit output safety.

## Task 2: Typed assertions and complete evaluation accounting

Files: src/zetra/eval_cases.py, src/zetra/evaluation.py, src/zetra/cli.py,
tests/sdk/test_evaluation.py.

Interfaces: `load_cases(root, agent)` returns validated cases and raw dataset bytes;
`grade(case, result)` returns per-assertion outcomes; `evaluate(..., mode='local',
model_config=None)` returns all rows and counts plus existing legacy report fields.

- [x] Write failing tests for strict prevalidation, tool arguments/order, wrong file
  contents, mixed passed/failed/error rows, no fixture inference claim and input changes.
- [x] Implement typed assertions with deterministic grading and local mode.
- [x] Run evaluator, existing CLI and fixture compatibility tests.

## Task 3: Aggregate requirements and execution evidence

Files: src/zetra/quality.py, src/zetra/testing/quality_plugin.py, src/zetra/cli.py,
tests/sdk/test_quality.py, examples/workspace_reporter/evals/workspace_reporter/**,
canonical template quality data/docs and bundled copy, docs/local-development.md,
docs/playbook-conformance.md.

Interfaces: `explain_quality(root, *, agent=None)` static requirements result;
`run_quality(root, *, agent=None, mode='fixture', model_config=None)` executes mapped
pytest nodes and evals, verifies identities/inventory and returns a gate result.

- [x] Write failing tests for missing category/reference, skipped/xfail/collection
  errors, changed inputs, undeclared tools and live-model requirements under fixtures.
- [x] Implement strict contract and SDK-owned pytest plugin; wire quality CLI.
- [x] Add realistic starter/example quality data and tests using the current shared
  agent API. Preserve the starter pin pending a reachable SDK prerequisite.
- [x] Run fresh rendered-project gate using the candidate SDK, full local SDK suite,
  canonical-template tests, lint/format, wheel/scaffold checks and independent review.
- [x] Record actual evidence and remaining publication, Temporal/platform and model-quality limits.

## Completed evidence

See [the validation record](../../evidence/local-quality-2026-10-31.md).
The canonical starter ships prepared quality files; activation remains explicitly
dependent on publishing the SDK and validating its new pin and lock. Local
Temporal and platform migration remain outside this completed increment.
