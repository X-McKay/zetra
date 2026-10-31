---
name: zetra-evaluate
description: Use when designing, extending, running, or interpreting tests and evaluations for a Zetra agent, tool, or runtime skill.
---

Read [current evaluation commands and evidence limits](../../../docs/development.md).
Inspect the changed capabilities, existing tests/datasets, and installed evaluator
before selecting a grader. Do not invent trajectory flags or quality-gate APIs.

Map each requirement to a component, scenario, assertion/grader and runner. Cover
outcomes, tool arguments/effects, runtime-skill activation/nonactivation, required
trajectory ordering, failure recovery and relevant safety boundaries. Separate
deterministic contract tests from model-behavior evals and live enforcement tests.

For example, an investigation can require an evidence artifact before a conclusion
and forbid a write tool while allowing multiple valid read sequences. Check both
the final answer and those observable events; do not grade private reasoning or
require one exact tool sequence without a correctness reason.

Use representative reviewed cases, preserve independent holdouts, and calibrate
judges against labels. Keep baseline/candidate dataset, model settings and runner
comparable. Count expected, executed, passed, failed, skipped and unscored/error
cases separately; incomplete evidence cannot become a passing requirement.

Run supported checks and report the actual scope with source/dataset/model/grader
identities and sample counts. A fixture `PASSED` means deterministic fixture cases
passed, not 100% model accuracy. If tooling cannot measure a required property,
report the missing test/evaluator and release implication instead of weakening it.
