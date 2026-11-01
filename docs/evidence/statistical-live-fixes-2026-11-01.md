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

Installed runtime validation passed 92 tests at SDK
`ee8870bea7a6a7445faa75e923129e8e3b3e975d` (same existing LiteLLM logging warning).
The canonical starter passed 30 tests, required offline quality and wheel checks;
renderer tests passed 13, and a fresh rendered repository passed full validation.
The reference repository passed its tooling 24, document-assistant 17 and
investigator 19 tests plus both required quality gates. The canonical SDK bundle
passed 81 scaffold/experiment/comparison/CLI tests and matched provenance.

The runtime prerequisite is `b1ecb6206f25c760354b80f878751dad7b329f21`, canonical
starter `7449ea970c859a9a7c97125a6953ca5e21592120`, and reference agents
`e646c37042e2d85a7dc860fb699f1119d9ba2e53`. Preserve these reachable dependency
commits when integrating the existing stacked draft PRs.

Fresh installed smoke passed all 20 cases: starter 6/6, document assistant 8/8
and investigator 6/6. This opened the predeclared 800-invocation study. Its status,
final counts, later confirmation smoke and exact artifact hashes are retained in
the draft review records:
[SDK #19](https://github.com/X-McKay/zetra/pull/19),
[agents #5](https://github.com/X-McKay/zetra-agents/pull/5), and
[template #6](https://github.com/X-McKay/zetra-template/pull/6).

The optional local observer delegates unchanged case execution and CLI handling,
then logs only case IDs/outcomes/counts. Its driver hash is retained with the
private run manifest. Larger studies use one sequential trial stream per suite,
up to three suites concurrently on the same model endpoint. Latencies therefore
include shared inference load and are not production benchmarks. Reports remain
unsigned development evidence; no provider-revision or hostile-code attestation
is claimed. Production qualification remains paused.

## Follow-up from repeated trials

The fixed study exposed intermittent starter `review-steps` failures despite the
passing smoke. The next starter candidate uses a simpler quote-only contract:
every supported request returns the whole guide, rather than choosing between
explanation and quoting. This is an intentional behavior change for the baseline;
developers can customize it with corresponding quality requirements. No expected
answer is embedded in the prompt, and no dataset assertion was changed. Five
separately labeled targeted diagnostics passed, which does not establish reliability.

The document-assistant study stopped after 20 passing attempts and one
`OutcomeUnknown`, with 299 planned attempts unrun because usage was unavailable.
The original compact error does not identify its underlying provider cause.
Five separate diagnostics at the original configuration reproduced one incomplete
provider response with `finish_reason=length` at the 1,024-token limit. Another
fixed five-diagnostic batch at 2,048 tokens also contained one truncated response.
This reproduces a cause of the same runtime error, not proof of that original
dispatch's precise cause. None of these diagnostics fill missing study attempts.

The [Qwen model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B) documents default
thinking and its explicit non-thinking chat-template option; NVIDIA's
[quantized model card](https://huggingface.co/nvidia/Qwen3.6-35B-A3B-NVFP4) links to
that base model. A diagnostic batch requesting non-thinking passed all five cases,
using four output tokens per answer. The SDK therefore adds a strict optional
`enable_thinking` boolean, forwarded only as `chat_template_kwargs.enable_thinking`.
Omission preserves old model-config digests; an explicit value changes identity.
The setting requests provider behavior, without attesting it. Incomplete-response
handling, unknown usage, budgets and retries are unchanged. Protected live workflows
also accept an administrator-owned `EVAL_ENABLE_THINKING` variable; no environment
settings or remote workflows were changed or dispatched here.

The new configuration tests first failed in four places. After implementation,
84 focused tests passed; full SDK validation passed 566 tests with 46 cluster
tests deselected and the existing warning. An additional native local-Temporal
HTTP test verified the mode reaches both cold model requests and the final report.
Historical failed and passing smoke reports still validate unchanged. Runtime
validation passed 92 tests, canonical and fresh-render validation passed 30 each,
renderer tests passed 13, and bundled scaffold/experiment/comparison/CLI checks
passed 68. Workflow script checks covered omitted, true, false and three invalid
values in each repository without inference. A focused independent review found
no important issues; it did not establish provider reliability or audit private
study artifacts.

The follow-up SDK prerequisite is `a147cd10898fcb99f218e6596fbf8f5da49ff540`, runtime
`cf158f8979da6c160bc37f6a2b2592ab4e8c0ef2` and canonical template
`51962d450eca53fc6c4406fca62120d69b875ee7`. These are new candidates, separate from
the frozen study. No second 800-invocation study is implied by confirmation smoke.
