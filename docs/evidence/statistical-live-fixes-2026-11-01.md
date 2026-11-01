# Live smoke failure investigation

The first actual-model smoke study used the published statistical-eval branches:
SDK `389e7ab9012513ce11020b063b11a54bb12858d3`, agents
`0357130c2dc79a1e110db0c89ab66272446de441`, and canonical template
`4aeb75b15b4bbc6fd94bb8c66aab940a4bbc6117`. It completed all 20 invocations
without execution errors: starter 2/6, document assistant 3/8, investigator 0/6
met every assertion. Observed token usage was 61,480. All three quality gates
returned nonzero. Those failed artifacts are preserved privately; they are not
rewritten or reclassified.

## Diagnosis

Bounded, separately labeled diagnostic invocations used the same advertised
`Qwen3.6-35B-A3B-NVFP4` at `https://llm.almckay.io/v1`, temperature 0,
1,024 output tokens per request and a 30-second provider timeout. Synthetic
guides and fixture-backed MCP observations were used throughout. No cluster,
deployment, release approval or production qualification was involved.

- Starter and document-assistant refusals contained `\n\nABSTAINED`.
- Investigator's acknowledgement contained `\n\nReport ready.`.
- The starter's review-steps answer quoted only the checks, omitting the
  introductory words that are part of the returned guide text.
- The first investigator outside-scope case attempted a forbidden pod name;
  the tool's existing name guard rejected it before MCP access. That attempt
  did not recur in the single diagnostic invocation. Temperature zero does
  not make this behavior deterministic.

The diagnostics expose visible answers and tool calls only, not private model
reasoning. They are development observations, not reliability evidence.

## Corrections and study boundary

`text_equals` remains exact by default. An explicit `whitespace: trim` policy
ignores only surrounding ASCII space, tab, CR and LF on expected and observed
text. The acknowledgement cases opt into it; internal whitespace, case,
additional prose, Unicode/invisible characters and file contents remain strict.
Original answers are not modified. Tests first rejected the new policy, then
passed after implementation, including strict-default and malformed-policy cases.

This is an intentional correction to the acknowledgement evaluation contract,
not evidence that the model improved. The case/dataset/evaluator identities
change, so the old and new reports cannot be used as a compatible baseline
comparison. Original failures remain failures under their original contract.

The starter instruction now requires the complete returned text for a verbatim
quote, including introductory words. Its diagnostic quote failed before the
change and passed afterward. The investigator instruction explicitly permits
only `investigator-demo-` names returned by `list_pods`, and prohibits probing a
forbidden name even if the tool would reject it. The deterministic tool guard
and the forbidden-call assertion remain in place.

No confidence targets, trial counts, tool/file checks, budgets, failure handling
or cases were removed. A fresh passing smoke run is required before the user's
authorized 800 additional reliability invocations (40 trials per case). Any
failures in that fixed study must be retained; it is not rerun until green.

## Verification

The focused grader/statistics/comparison suite passed 99 tests after the failing
regressions. Full SDK `just check` passed 557 tests, with 46 cluster tests deselected
and the existing ADK experimental warning; lint, format, lock, docs, examples and
quality gates passed. A fresh independent review found no critical, important or
minor issues. Dependency refresh and actual reliability were explicitly left for
subsequent verification; the reviewer did not rerun tests or independently audit
private diagnostics. Those limits stand and no production-readiness claim follows.

Installed-dependency, template, reference-agent and live-study results will be
recorded when complete. Production qualification remains paused.
