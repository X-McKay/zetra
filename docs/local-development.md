# Local development and quality gates

The current SDK runs the same admitted ADK agent core without Temporal, OpenShell
or a cluster. Agent factories, tools and pytest tests execute trusted Python on
the host. A fresh process and disposable workspace are not a sandbox.

## Run an agent

From this SDK checkout with its locked development environment installed:

```sh
uv run --locked zetra dev examples/workspace_reporter --input 'Inspect this workspace.'
uv run --locked zetra quality examples/workspace_reporter --explain
uv run --locked zetra quality examples/workspace_reporter
```

The example uses deterministic fixtures. A generated project still pins the older
published SDK; see the starter upgrade section before using these commands there.
`dev` uses the shared Definition/Runtime/Dispatcher with at most 64 model/tool
boundaries. Each effect gets one attempt; unknown outcomes remain unknown. It
never connects to Temporal or falls back from a failed provider to fixtures.
Omitting model configuration is allowed only for a fixture manifest. The default
invocation timeout is 60 seconds; `--timeout` accepts up to 600 seconds.

To use an OpenAI-compatible model, create an explicit JSON configuration:

```json
{
  "base_url": "http://127.0.0.1:8000/v1",
  "model": "your-model-name",
  "max_tokens": 2048,
  "timeout": 30,
  "temperature": 0
}
```

```sh
uv run --locked zetra dev /path/to/agent --input 'Read the guide.' --model-config /path/to/model.json
uv run --locked zetra eval /path/to/agent --mode local --model-config /path/to/model.json
uv run --locked zetra quality /path/to/agent --mode local --model-config /path/to/model.json
```

Use the provider model name without the SDK's `openai/` routing prefix. Add
`"api_key_env": "LOCAL_MODEL_API_KEY"` for authentication and supply that variable
through your development secret manager or environment. Inline credentials, URL
credentials and query parameters are rejected. HTTP is limited to loopback;
remote endpoints require HTTPS. Endpoint and model names appear in reports: do
not put secrets in URL paths or model identifiers. Overrides apply only locally,
never to production AgentSpec or Release settings.

Models must support the admitted serial tool-calling profile. Parallel calls,
unknown tools and incomplete responses fail. Dependency installation and remote
models may need network access; fully offline inference requires a running local
model with downloaded weights. MCP tools still need their configured transport
and development credentials.

`--workspace INPUT_DIR` copies a bounded, link-free snapshot into disposable state.
`--output NEW_DIR` exports the final workspace and private `report.json` outside
the source/input trees. Existing output directories and symlinked paths are
rejected. Failures can leave partial exports; inspect status before using them.
Reports include source/config identities, admitted tools, final text, bounded
file text, observable tool arguments/results and timing/token metrics. Raw model
reasoning and sessions are not exported. Unavailable token counts are null.
Selective key masking does not scrub free text, files or arbitrary tool results;
use synthetic data and keep sensitive reports out of Git. Quality reports omit
pytest stdout/stderr; rerun a failing selector directly to debug it.

## Required evidence

Each agent owns `evals/NAME/quality.json`, mapping requirements to actual pytest
selectors or case IDs. `quality --explain` validates metadata without importing
agent/test code. It identifies missing coverage; it cannot prove test semantics.

| Component | Minimum kinds |
| --- | --- |
| Agent | `outcome`, `performance`; `trajectory` when tools are declared |
| Each tool | `contract`, `security`, `recovery` |
| Each declared runtime skill | `contract`, `activation`, `nonactivation` |

For example, map `tool:read_document/security` to an actual rejected-access test.
The `tools` map declares effects; `skills` maps names to packaged
`package:resource` references. Runtime skills are explicitly declared, not
automatically discovered or instrumented. Developer skills in `.agents`/`.claude`
are separate. Contract/security/recovery/activation/nonactivation require tests;
outcome/trajectory/performance can use tests or cases with suitable assertions.
See the working example's quality.json for the complete schema.

Add domain cases for authorization, injection, disclosure, timeout, recovery,
abstention and conflicting instructions where relevant. The baseline is a floor,
not exhaustive assurance. Review assertions and challenge them with plausible
faults; names and labels alone cannot establish meaningful semantic coverage.

`quality` runs mapped pytest selectors in a fresh process and the entire dataset
freshly. Required tests need actual collection and successful setup/call/teardown.
Missing, skipped, xfailed, xpassed, failed or errored evidence blocks the gate;
all parametrizations of a referenced function must pass. Cases must all pass,
observed tool names/effects must exactly match the contract, and source/tests/
configuration/contract/dataset identities must remain unchanged. Setting
`live_model_required: true` prevents fixture-only acceptance. Keep a separate
full-project test step: this gate runs only mapped selectors. Its nonzero exit
blocks a CI job using the command.

These reports are local unsigned evidence. Repository authors control contracts,
tests, hooks and dependencies. Production requires independently protected policy,
review and attestations; no existing promotion policy is changed here.

## Evaluation cases

Legacy `id/input/contains/files` cases remain supported. Version 2 uses explicit
assertions; the whole dataset is validated before executing any case:

```json
{"schema_version":2,"id":"guide","input":"Read the guide.","assertions":[{"type":"text_contains","value":"Completed."},{"type":"tool_called","tool":"read_document","arguments":{"document_id":"guide"}},{"type":"max_steps","maximum":3}]}
```

Supported assertions are `text_contains`, `text_not_contains`, `file_contains`,
`file_exists`, `tool_called`, `tool_forbidden`, `tool_before`, `max_steps` and
`max_duration_ms`. Text uses `value`; files use `path` and optionally `value`;
tools use `tool`; calls optionally use an `arguments` subset; order uses `tool`
and `before`; limits use `maximum`. Required calls/order need successful outcomes.
Forbidden-call checks include failed attempts. Missing evidence fails.
`max_steps` counts accepted model/tool effects; milliseconds are local elapsed
time, not production latency. All planned cases receive passed/failed/error
outcomes; exceptions cannot disappear from the denominator. Input mutation fails
the report. Sandbox cases also require confirmed deletion; trajectory assertions
fail if the client does not expose the necessary evidence.

Fixture cases prove deterministic plumbing only. Model-quality claims require
provider-backed cases, meaningful criteria, held-out data and repeated trials.
Judge calibration, token/cost budget assertions, trial aggregation and statistical
regression decisions remain future work.

## Starter activation and remaining adapters

The canonical starter retains its reachable older SDK pin, lockfile and supported
CI commands. Prepared cases/contract live under its `docs/quality-upgrade/`.
Rehearse in a disposable rendered project using the candidate SDK, then publish
the prerequisite SDK commit, update the pin and lock together, activate the
prepared files and add `zetra quality .` as a required CI step. The older starter
does not enforce this new gate yet.

No-Temporal execution is implemented. Local Temporal needs a separate host
lifecycle/transport adapter; pointing the current production worker at localhost
still requires OpenShell. The different `platform/agent-runtime` engine is not
migrated here. Local tests do not establish live sandbox or real model quality.
