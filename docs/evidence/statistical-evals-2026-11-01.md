# Statistical evaluation implementation evidence

Scope: the user requested stronger statistical/live-model evaluations, then a pause
before production qualification. No cluster context, live MCP connection, signing,
release approval, merge, image push or deployment was performed for this milestone.
A real-model endpoint/model and budget were requested; no answer is available yet.
No actual provider-quality or enterprise-readiness result is claimed.

## Implemented

- Strict code-owned experiment plans, version-3 tagged/clustered cases, fixed seeded
  repeat schedules and fresh local runtime state per trial.
- Complete success/failure/error/not-run accounting, pre-dispatch request ceilings,
  per-trial/global time limits and observed aggregate token stops. Unknown usage is
  not zero; the last bounded trial can overshoot the observational token limit.
- Exact/negative outcome checks, forbidden-argument trajectories, observed tool-result
  subset checks and input/output/total-token assertions.
- Approximate per-case Wilson score intervals with Bonferroni correction; no pooled
  independent-task confidence claim from repeated prompts. Small samples remain
  inconclusive. Every observed assertion is still required to pass.
- Bounded typed report validation, compatible-baseline checks, per-case differences
  with conservative endpoint subtraction, and descriptive token/latency summaries.
- Data-only quality explanation, schema-2 declared experiments, explicit combined
  fixture/live quality and new private report files. Existing default evals remain.
- Synthetic live development datasets: 8 document-assistant cases, 6 investigation
  cases, 6 starter cases and 2 SDK workspace-example cases. The investigator's
  explicit live MCP fixture contains instruction attacks. These are not holdouts.
- Default-branch manual workflows with an administrator-configured evaluation
  environment and dedicated model key, separate from release credentials. The
  environment/secret protection is an administrative prerequisite, not configured
  or attested by these changes. Routine CI remains offline.

## Observations and validation

Initial eval/quality baseline: 72 passed. Task 1 validation/regressions: 115 passed.
Task 2 full SDK: 510 passed, 46 cluster tests deselected, one existing ADK experimental
warning. Task 3 full `just check`: 529 passed, 46 deselected, same warning; lint,
formatting, lock, documentation links, examples and required quality checks passed.

Actual synthetic HTTP tests exercised ADK/provider conversion, tool execution,
fresh workspaces across repeated trials and a model-call limit enforced before a
second provider request. These are transport/lifecycle tests, not real models.
Native CLI SIGTERM first exited -15 without a report; after signal handling was
added, the test preserved incomplete live evidence and returned nonzero.
A final-trial token total exactly at the budget initially failed; a failing
regression led to distinguishing completed exact-budget schedules from exhaustion.

Runtime package at the new SDK pin: 92 passed, with the existing pinned LiteLLM
async logging teardown warning. No assertion failures. Canonical starter complete
validation: 30 tests plus offline quality and wheel/resource checks. Standalone
renderer: 13 passed. A freshly rendered project at /tmp/zetra-statistical-starter
passed `just validate` including quality and wheel checks.

Reference repository `just check`: tooling 24, document assistant 17, investigator
19 tests passed, with the existing ADK declaration warning in agent checks; both
required offline quality gates passed and validated the declared live experiments.

Bundling the canonical starter exposed a test-fixture assumption that the
experiments directory was absent: 31 setup errors. The fixture now accepts the
shipped directory. The combined scaffold/experiment/comparison/CLI suite passed
79 tests afterward. No product behavior or gate was weakened to resolve it.

Whole-change independent review and final hosted-head checks follow this record;
any material findings and corrections will be appended. No live inference was
substituted with fixture results, and no qualification skip is reported as a pass.

## Dependency integration

Published SDK prerequisite: `2c70149d7c0472e79551ce0ce124a885dbf3eaea`.
Published runtime package prerequisite: `1b7e7733f43d0d5487dc4e3b023eecbd2b33cbdb`.
Canonical template: `93bf512deba78ef223bb2a37ce84203753f6dd8c`.
References and canonical starter consume the published prerequisites, not local
editable overrides. SDK bundle provenance records the exact canonical tree.
Preserve these commits as reachable ancestors when integrating the stacked PRs.

## Limits retained

Reports are unsigned trusted-host development evidence; hash consistency does not
attest authorship, grader semantics or hostile-code isolation. Provider model
revision is not independently verified. Wilson intervals are approximate and assume
stable independent per-case draws. No calibrated judge, suite generalization
interval, sequential-testing guarantee or billing estimate is implemented. The
examples' confidence targets are illustrative, not production policy. Real model
runs and independently reviewed holdouts are still needed for substantive quality
claims. Production qualification/rollout, telemetry/continuous optimization and
zetra-ui are explicitly outside this milestone.
