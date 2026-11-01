# Statistical and live-model evals

Named experiments run the same local agent factory and tools without Temporal or a
cluster. Ordinary `zetra quality .` remains the fast deterministic gate. Version-2
quality contracts also validate their declared live plans and datasets in offline
CI, without contacting a model or satisfying live requirements.

## Run and compare

Use the canonical starter or either reference agent with an explicit model config:

```sh
uv run --locked zetra quality . --explain
uv run --locked zetra quality .
uv run --locked zetra eval . --mode local --experiment smoke --model-config /private/model.json --output /private/smoke.json
uv run --locked zetra quality . --mode local --experiment reliability --model-config /private/model.json --output /private/quality.json
uv run --locked zetra eval . --mode local --experiment reliability --model-config /private/model.json --output /private/baseline.json
# After a code/prompt/model change, preserve the same fixed plan and cases.
uv run --locked zetra eval . --mode local --experiment reliability --model-config /private/candidate-model.json --output /private/candidate.json
uv run --locked zetra eval-compare /private/baseline.json /private/candidate.json
```

Run only the commands needed: `quality --experiment` already runs the selected live
experiment after the deterministic requirement gate, and nests its report under
`experiment`. For comparison, use standalone eval reports (or extract that nested
experiment object); do not rerun a paid experiment solely to change the wrapper.
`--output` on a named experiment/comparison requires a new file outside the project
in an existing directory; it writes mode 0600 and never replaces an earlier report.
Default evals remain available without `--experiment`.

The investigator additionally requires explicit
`--mcp-fixtures evals/investigator/mcp-live.json` for its live suite. The fixture
includes synthetic instruction attacks in tool output. This exercises real model
choices against synthetic remote observations without any cluster access. Use
`mcp.json` for the original deterministic gate. Changing MCP bindings makes reports
incompatible for baseline comparison; hashes do not attest live remote behavior.

The live command uses an OpenAI-compatible endpoint from `--model-config`. See
[local configuration](local-development.md). Credentials stay in an explicitly
named environment variable. No implicit provider or fixture fallback exists. Pin
provider model revisions where available; reports identify the requested model,
not an independently verified provider build. Temperature zero is not determinism.
Compatible Qwen/vLLM endpoints can explicitly select `"enable_thinking": false`
for bounded direct-response tasks. Omission keeps the provider default. The mode
is part of the model configuration identity; changing it starts a new configuration,
not a continuation of an interrupted study. Truncated responses remain errors and
unknown usage still stops a token-budgeted study.

## Fixed plans and coverage

Each `evals/AGENT/experiments/NAME.json` selects a version-3 JSONL dataset relative
to that agent's eval directory. It declares trials per case, a schedule seed,
confidence, a per-case lower-bound target, a comparison regression margin, required
tags and explicit budgets. `quality --explain` shows invocation/request ceilings
and the best all-pass lower bound before spending inference tokens. Validate the
whole plan/dataset before execution; invalid or duplicate fields and unsafe paths
are rejected. Case order is shuffled within each repetition using the fixed seed.

Version-3 cases retain version-2 assertions and add `tags` and `cluster`. Use cluster
IDs to identify related cases from one document or scenario. Tags describe reviewed
coverage; labels alone do not prove semantics. The standard examples cover:

- Supported outcomes, exact source quotes/citations and observable tool ordering.
- Unsupported requests, abstention, document/namespace scope and instruction attacks.
- Explicit skill activation in document-tool results, alongside the existing required
  pytest contract/security/recovery and skill activation/nonactivation checks.
- Step and token limits. Missing usage fails a requested token assertion.

New graders are `text_equals`, `file_equals`, `file_absent`, `file_not_contains`,
`tool_result` (nonempty JSON result subset), and `max_input_tokens`,
`max_output_tokens`, `max_total_tokens`. `tool_forbidden` optionally takes an
`arguments` subset and counts rejected/unknown attempts too. Existing text/file,
required-call and partial-order graders remain supported. Missing or malformed
observations fail; absent-file assertions need an observed file inventory.
`text_equals` is exact by default. For human-readable acknowledgements, an explicit
`"whitespace": "trim"` option ignores only surrounding ASCII spaces, tabs, CR and LF
on the expected and observed text. It does not ignore case, internal whitespace,
additional words, Unicode spacing or invisible characters. File equality remains
exact, and recorded outputs are never rewritten. A whitespace-policy change alters
the case/dataset identity: preserve the previous failures and start a new study;
reports from the two policies are not comparable as an agent improvement.
See the [first live-smoke investigation](evidence/statistical-live-fixes-2026-11-01.md)
for the observed failures and the explicit contract correction.
For forbidden-argument checks, redacted or truncated compared fields cannot prove
that a call was allowed. They fail conservatively unless another observed field
proves the forbidden subset did not match. Use non-sensitive observable identifiers
where possible; redaction can otherwise make a valid call fail its evaluation.

A schema-2 quality contract adds `"experiments": ["smoke", "reliability"]` to the
schema-1 fields. The chosen name must be declared. Explicit local experiment quality
first runs the fixture requirements, then the live experiment, checking exact tool
inventories and unchanged inputs throughout. A fixture AgentSpec is required for
that combined command; model-only projects can use standalone experiments plus
separate unit/contract tests. `live_model_required: true` still blocks fixture-only
quality acceptance. Repository authors own these requirements; this is not a
protected production promotion policy.

## What a result establishes

Smoke plans make one attempt per case and have a zero confidence-bound target.
`PASSED` means every smoke assertion passed; it makes no reliability claim.
The example reliability plans make 40 attempts per case, require every assertion
on every attempt to pass, and require a per-case lower bound of at least 0.8 at a
requested 95% family confidence. These are illustrative development requirements,
not enterprise acceptance thresholds. Increase the target/sample plan before a run
when the use case needs stronger evidence.

Intervals are approximate two-sided Wilson score intervals, Bonferroni-adjusted
across the fixed cases. They assume independent stable draws for each case; provider
drift, caching or shared state can violate that assumption. A small perfect sample
still has a wide interval and can be `INCONCLUSIVE`. No pooled binomial interval
counts repeated prompts as extra independent tasks. Suite success is a descriptive
mean of per-case pass fractions, with per-tag/cluster summaries and distinct counts.
At-least-one-pass and all-trials-pass fractions are explicitly separate; pass@k is
not operational reliability. These curated examples are development cases, not a
hidden holdout or evidence about unseen tasks.

Reports retain every planned attempt. Execution failures remain errors; canceled,
unstarted or budget-stopped attempts never vanish from the denominator. Only
`PASSED` exits zero. `FAILED`, `INCOMPLETE` and `INCONCLUSIVE` block a selected gate.
Any observed assertion failure blocks even when the average score looks good.

Comparison requires complete reports with identical dataset, case assertions,
experiment, agent definition, SDK/evaluator dependency identity and MCP binding.
Source and requested model/config may differ and are displayed. Per-case success
and token/latency differences are paired by case ID, not by provider random seed.
Conservative difference bounds subtract Wilson endpoints with a union correction
across both configurations and all cases. A candidate must pass its own gate and
every lower difference bound must exceed the predeclared negative regression
margin. A nonsignificant difference is not noninferiority. The example 40-trial
plan may still be inconclusive for its tighter 5-point comparison margin even if
both configurations pass every case; plan a larger fixed study when necessary.

Do not add trials, rerun until green, drop failures or tune thresholds after seeing
results. Preserve failed observations. When changing the plan, treat it as a new
study and rerun both configurations. Choose candidates on development data and keep
an independently reviewed holdout for final claims. No sequential-testing guarantee,
calibrated model judge, statistical latency improvement or production accuracy is
implemented here.

## Budgets and artifacts

Plans cap invocations, per-trial model requests (before dispatch), per-trial time,
experiment duration and optionally observed aggregate tokens. Provider configuration
also caps output tokens per request. Token budgets are checked after each bounded
trial: the last trial may overshoot; unavailable usage stops a token-budgeted run.
If any dispatched model request loses its response or usage, earlier known token
counts cannot establish the trial total. A request refused before dispatch by the
request ceiling does not invalidate already observed usage.
This is not a hard monetary cap. The report never invents token counts or prices.
Metric summaries expose observed/missing counts, mean, median, maximum and nearest-
rank p95 over executed trials including failures. Missing values are not zero;
comparison means are withheld when coverage is incomplete.

Named experiment reports, including the fixture evidence in combined live quality,
contain identifiers, hashes, assertion booleans, tool inventory and metrics,
not prompts, answers, tool arguments/results or private reasoning. Metadata such as
case IDs and endpoint names can still be sensitive. Detailed debugging remains an
explicit `zetra dev --output` action; that output and ordinary fixture evaluation
reports can contain sensitive data.
Protect artifacts and avoid public uploads. A graceful cancellation during live
trials saves incomplete evidence; SIGKILL cannot promise a final report. Experiments
execute trusted host Python, not a hostile-code sandbox. Hashes and unsigned local
reports are not independent attestations or production authorization.

The manual live-eval workflows are restricted to the default branch, have only
read permissions and use a dedicated model secret. They do not run untrusted PR
code with credentials. Regular CI remains offline. There is no deployment or
production qualification step in these experiments.

## Method references

[Anthropic's statistical evaluation methodology](https://www.anthropic.com/research/statistical-approach-to-model-evals)
explains per-question means, clustering and paired comparisons.
[Anthropic's agent evaluation guidance](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)
distinguishes repeated success metrics and recommends outcome/trajectory coverage.
[NIST's binomial interval guidance](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm)
describes Wilson intervals and small-sample limits. This implementation intentionally
reports per-case intervals rather than a generalization interval for curated tasks.
