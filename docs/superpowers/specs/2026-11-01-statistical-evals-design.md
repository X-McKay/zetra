# Statistical and live-model evaluation

The user authorized this milestone after runtime unification, then explicitly asked
us to pause before production qualification. Implement and validate in the existing
isolated follow-up worktrees; preserve the runtime-unification draft PR branches.
No cluster, rollout, signing, release-policy or UI changes belong to this milestone.

## Intent and approach

Keep the fast deterministic quality gate and the shared local agent runtime. Add
code-owned, fixed-sample live experiments, descriptive optimization metrics and
conservative comparisons. Agent authors edit ordinary JSON cases/config and Python.
Alternative: flatten repeated trials into the existing case rows; rejected because
quality requirement accounting assumes one row per case. Alternative: add a hosted
eval service/model judge; deferred because credentials, calibrated rubrics and a
new service are unnecessary for these deterministic, source-bounded references.
These implementation choices are the builder's calls within the approved scope.

## Public workflow

`zetra eval PATH --agent NAME --mode local --experiment NAME --model-config FILE`
loads `evals/AGENT/experiments/NAME.json`. `--mcp-fixtures FILE` remains explicit.
`--output FILE` uses existing private, non-overwriting JSON output. Default eval
and quality behavior remains compatible. Experiments are local-model only, never
fixture or sandbox qualification. A synthetic HTTP endpoint can exercise transport
but must not be represented as real-model evidence.

An experiment declares schema_version=1, dataset (a .jsonl file relative to the
agent eval directory), trials (1..1000), seed, confidence (0.8..0.999),
min_case_success_lower_bound (0..1, zero means smoke only), max_regression (0..1),
required_tags, and limits: max_invocations (1..2000), model_calls_per_trial (1..32),
trial_timeout_seconds (1..600), max_duration_seconds (1..86400), max_total_tokens
(positive integer or null). Case count times trials must fit the invocation cap.
Validate the entire configuration/dataset before importing or executing agents.
Reject duplicate JSON keys, malformed/nonfinite values, paths escaping the agent
eval directory, symlinks, oversized inputs, unknown case references and absent tags.

Version-3 cases add nonempty unique tags and a cluster identifier to existing
version-2 assertions. Preserve old formats. Add deterministic exact-text/file
checks, absent-file checks, successful tool-result subset checks and input/output/
total-token ceilings. Missing or malformed evidence fails. Tool-result checks can
verify runtime-skill activation/nonactivation via explicit result evidence; no
implicit skill instrumentation or LLM judge is claimed.

## Execution and accounting

Use the same local_run and grader as ordinary evaluation, not a new agent loop.
Each (case, trial) gets fresh child/workspace state. Predeclare the complete schedule;
shuffle cases in each repetition with a recorded seed to distribute temporal drift.
No retry-to-pass, sample extension, fixture fallback or unknown-effect redispatch.
Errors and not-run trials stay in the denominator. A deadline/token stop leaves
all remaining schedule entries explicitly not_run and makes the report incomplete.
Cancellation terminates the child via existing process supervision; preserve a
partial report for graceful cancellation. SIGKILL cannot promise a final report.

Enforce model-call limits before dispatch in the shared local driver (optional
argument; ordinary callers unchanged). This is a request-count bound, not an exact
billing cap. Token budgets use observed usage after each trial; stop before another
trial when exhausted or unavailable. The last bounded trial can overshoot that
observational budget, and unknown provider usage cannot become zero. Every trial
also has a bounded timeout and provider output-token cap. No monetary estimate
without explicit price data; this milestone reports tokens rather than guessing.

Reports omit raw prompts, final text, tool arguments/results and raw reasoning;
retain case/trial IDs, assertion booleans, status/error types, tool inventory,
metrics, tags and cluster IDs. Detailed debugging remains explicit `zetra dev`
output. Source, dataset, experiment, model config, MCP binding and SDK evaluator
implementation identities are recorded and checked for mutation before/after each
trial. These are unsigned local evidence, not attestations or authorization.

## Statistics and decisions

Keep one aggregate row per case with all planned trials beneath it. The measured
success rate is the mean of per-case pass fractions, not a population accuracy
claim. Report all-trials-pass and at-least-one-pass fractions separately; neither
is a general reliability guarantee. Also report per-tag and per-cluster summaries.
Never pool case x trial rows into one binomial confidence interval.

Per-case two-sided Wilson intervals use the planned number of attempts; incomplete
reports carry no passing statistical decision. Use Bonferroni correction across
all cases for the requested family confidence. Assumption: independent, stable
model draws for each fixed case. Same-case dependence/provider drift can violate
it; zero temperature does not prove determinism. Curated synthetic cases cannot
establish unseen-task or deployment accuracy. No confidence interval on the suite
mean is claimed. A smoke experiment has threshold zero and is explicitly labelled
smoke. Reliability experiments require all observed assertions to pass plus every
per-case lower bound meeting the configured threshold. Small all-pass samples are
INCONCLUSIVE, not PASSED. Any observed error/failure or integrity failure is FAILED;
unexecuted trials are INCOMPLETE. Only PASSED exits zero.

Metrics report observed/missing counts, total, mean, median, maximum and nearest-
rank p95 across all executed trials; include failed trials, and expose coverage.
Never impute missing usage. No statistical performance-improvement claim from p95.

`zetra eval-compare BASELINE CANDIDATE` parses bounded data-only experiment reports,
validates complete schedules/unique identities/counts/finite metrics/digests and
recomputes summaries, ignoring no tampered stored decisions. Require the same
agent, experiment/dataset, SDK evaluator and MCP identities; source/model config
may intentionally differ. Report paired per-case pass-rate and metric deltas.
For a conservative per-case difference interval, subtract baseline/candidate
Wilson endpoints with Bonferroni across both configurations and all cases.
A new failure in any required case fails comparison. Otherwise pass only when
all difference lower bounds exceed -max_regression; intervals crossing the margin
are INCONCLUSIVE. This is not a paired trial claim, optional stopping, or proof of
noninferiority from a nonsignificant p-value. Endpoint model aliases remain the
caller's responsibility; reports preserve requested model config.

## Integration and coverage

Optional quality schema version 2 adds `experiments`: a nonempty unique list of
experiment names. `quality --explain` validates every declared experiment and
explains planned sample size and attainable all-pass confidence before code import.
The ordinary offline quality command still runs its mapped tests/cases and validates
these plans; it does not pretend to execute live models. Explicit `quality --mode
local --experiment NAME` first runs the existing deterministic requirement gate,
then the chosen declared live experiment. `live_model_required` continues to block
fixture-only acceptance; unsupported combinations fail before tests/inference.

Reference agents and the canonical starter include separate synthetic live cases
and smoke/reliability experiments. Cover supported tasks, exact arguments/tool
ordering, unsupported requests/abstention, access/instruction attacks and skill
activation evidence as relevant. Tool-output injection uses explicit per-experiment
MCP fixtures through the existing CLI option and identity checks; reference suite
can choose a separately reviewed fixture file. No external MCP/cluster needed.
Baseline pytest contract/security/recovery coverage remains mandatory.

Keep regular CI deterministic and secret-free. Add explicit manual live-eval
workflows on trusted branches, with bounded selected experiment/model endpoint,
optional dedicated EVAL_MODEL_API_KEY, minimal permissions and private reports.
Never expose signing/registry credentials or run untrusted PR code with secrets.
Check dataset/plan validity in normal CI. Pin published SDK prerequisites, refresh
locks, render/test canonical template, then rebundle exact provenance. Update
SDK docs/conformance and developer eval guidance with claims and limits.

## Validation and boundary

Test statistical reference values/extremes, multiplicity, missing usage, malformed
reports, inconclusive decisions, repeated fresh state, deadline/budget exhaustion,
provider errors, mutation and cancellation. Real HTTP transport integration must
exercise full ADK/tool execution; do not mistake that synthetic server for a model.
Run a real-model smoke/reliability experiment only against the user's specified
endpoint/model and budget; this question is pending, independent implementation
continues. Preserve failures honestly, and do not repeatedly optimize on a supposed
holdout. These shipped datasets are development examples, not hidden holdouts.

Sources: Anthropic statistical methodology
https://www.anthropic.com/research/statistical-approach-to-model-evals;
agent eval methodology https://www.anthropic.com/research/demystifying-evals-for-ai-agents;
NIST Wilson interval https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm.
