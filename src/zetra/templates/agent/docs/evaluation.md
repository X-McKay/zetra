# Live evaluations and optimization

`just validate` remains offline and checks the required unit/component tests,
fixture cases, and the structure of every declared live plan. It makes no live-model
quality claim. Start with `uv run --locked zetra quality . --explain` to see the
planned invocations, model-request ceiling and attainable all-pass confidence.

Supply an explicit OpenAI-compatible model configuration outside this project:

```json
{"base_url":"http://127.0.0.1:8000/v1","model":"your-pinned-model","max_tokens":1024,"timeout":30,"temperature":0}
```

Remote endpoints require HTTPS. For authentication add `api_key_env` with the name
of a variable supplied by your secret manager; never put the key in the JSON.

```sh
uv run --locked zetra eval . --mode local --experiment smoke --model-config /private/model.json --output /private/smoke.json
uv run --locked zetra quality . --mode local --experiment reliability --model-config /private/model.json --output /private/quality.json
```

The starter has six synthetic development cases: supported guide quotes, tool
selection, unsupported requests, access/instruction attacks and credential refusal.
They are examples to extend, not a hidden holdout or enterprise acceptance policy.
Tool contract/security/recovery tests remain mandatory. Add domain cases and runtime
skill tests when you add capabilities; retain meaningful graders and failure records.

`smoke` makes one attempt per case and checks integration, without a reliability
claim. `reliability` makes 40 attempts per case and requires every assertion to pass
plus an approximate per-case Wilson lower bound of 0.8 at a requested 95% family
confidence. Bonferroni adjustment covers the fixed cases. Change targets/sample size
before starting a new study. Repeated draws require independent stable model
behavior; related cases are identified by cluster, not pooled as independent tasks.
Only PASSED exits zero. Insufficient samples are INCONCLUSIVE, not successful gates.

For optimization, preserve a complete standalone baseline report, change one factor,
and run the same fixed experiment against the same data/tool fixtures:

```sh
uv run --locked zetra eval . --mode local --experiment reliability --model-config /private/model.json --output /private/baseline.json
uv run --locked zetra eval . --mode local --experiment reliability --model-config /private/candidate-model.json --output /private/candidate.json
uv run --locked zetra eval-compare /private/baseline.json /private/candidate.json
```

The combined quality command already includes its experiment under `experiment`;
extract that object for comparison instead of rerunning a paid study. Compare
quality, failures, tokens and latency together. Missing usage is not zero. A shorter
prompt or passing fixture does not demonstrate a cost/accuracy improvement. The
40-trial plan can still be inconclusive for the tighter 5-point noninferiority
margin, even when both reports are all-pass; a larger predeclared study may be needed.
Do not append trials, rerun until green, discard errors or relax thresholds after
seeing results. Keep independent held-out data for final claims.

Plans cap invocations, per-trial model requests and time. Aggregate token caps use
observed usage after each bounded trial, so the last trial can overshoot; this is
not an exact billing cap. Unknown usage stops a token-budgeted experiment. Graceful
cancellation during live trials retains incomplete evidence; SIGKILL cannot promise
an artifact. Report files must be new, outside the project, and use mode 0600.
They omit prompts, answers, tool arguments/results and reasoning, but identifiers,
endpoint/model names and metrics can still be sensitive. Local execution trusts
project Python and is not a sandbox or production qualification.

The manual `Live model evaluation` workflow uses only the default branch. Before
using it, configure a GitHub environment named `live-evaluation`, restrict its
allowed deployment branches to the default branch, set environment variables
`EVAL_MODEL_BASE_URL` and `EVAL_MODEL_NAME`, and optionally the dedicated environment
secret `EVAL_MODEL_API_KEY`. Keep this key out of repository-wide secrets and keep
signing/registry credentials out of this environment. Endpoint/model settings are
administrator-owned, not arbitrary dispatch inputs. Do not dispatch from untrusted
code. The workflow retains only compact reports for seven days, including failures;
review your repository's artifact access policy. It never deploys or qualifies an
agent against a cluster.
