# Local execution and quality gates

Approved direction: continue the agent-development roadmap with cluster-free execution
and enforceable local quality requirements. This increment uses the existing ADK SDK
core. It does not silently substitute the different platform/agent-runtime engine.

## Decisions

The fixture runner already uses Definition, Runtime, WarmRunner and Dispatcher from
production. Extract its host driver and inject an explicitly selected model binding.
Keep production model defaults, policy admission and durable orchestration unchanged.
A second agent loop would diverge; migrating the platform engine is a separate change.

`zetra dev PATH --input TEXT` executes trusted host Python in a fresh child with no
Temporal/OpenShell connection. With no model configuration, only fixture manifests
are accepted. `--model-config FILE` explicitly selects an OpenAI-compatible endpoint,
model, timeout, output-token bound and optional API-key environment variable. Plain
HTTP is allowed only for loopback; remote endpoints require HTTPS. No secret value
appears in the configuration identity or run report. Endpoint/model settings override
the development execution only, never the production AgentSpec or Release.

Reports contain source/config identities, actual registered tool inventory, local
elapsed time, provider usage when available (null otherwise), normalized observable
model/tool events, final output and bounded file text for grading. They are local,
unsigned development evidence. Raw model reasoning is not exported. Unknown tool
outcomes are retained, never retried. Initial workspaces are copied into disposable
state; optional explicit output export must not overwrite existing data or overlap
source/input. Fixture mode remains clearly distinguished from provider-backed local
mode. There is no automatic fallback to fixtures after a provider failure.

## Evaluation and quality

Retain legacy fixture cases for compatibility. Add version-2 cases with explicit
assertions: text contains/not-contains, file contents, tool called/forbidden, required
tool ordering, accepted-boundary limit and elapsed-time limit. Validate the entire
suite before execution; duplicate IDs, unknown assertions and invalid paths fail
without running any agent. Account for each planned case/trial as passed, failed or
error; exceptions cannot disappear from the denominator. Preserve separate input
digests for source, dataset, model binding and quality contract.

Per-agent `evals/NAME/quality.json` declares tool effects, runtime-skill resources,
and requirement-to-test/case mappings. `zetra quality PATH --explain` is data-only.
It explains missing baseline coverage: outcome/performance for every agent,
trajectory for tool-using agents; contract/security/recovery for every tool; and
contract/activation/nonactivation for each runtime skill. Domain requirements may
add more coverage. Missing references, empty evidence mappings and invalid schemas
fail before code import. An optional live-model requirement cannot pass on fixtures.

`zetra quality PATH` executes the mapped pytest nodes through an SDK reporting plugin
and executes the evaluation suite fresh. Missing, skipped, xfailed, unexecuted,
errored or failed required tests/cases block the gate. Reconcile actual runtime tool
inventory against the contract, and fail if source, tests, dataset or contract change
during the gate. The report is reproducible local evidence, not an independent
production authorization. Repository owners can edit their tests/contracts; production
still needs externally controlled required policy and trusted artifact attestations.

## Delivery boundary

Ship the SDK commands, shared driver, assertion/quality machinery, tests, a working
example, and updated documentation. Update the canonical starter with compatible
quality data and instructions. Do not change its SDK Git pin to an unpublished or
unreachable commit. Activating new CLI commands in the starter's normal CI requires
the prerequisite SDK commit to be available remotely and the new pin/lock to be
validated together. Until that activation, explicitly document the compatibility
boundary rather than silently skipping a required gate.

Local Temporal requires a host lifecycle/transport adapter around the shared Runtime;
pointing the current production worker at localhost still requires OpenShell. Deliver
that adapter separately. Likewise, provider-backed tests here use a synthetic local
HTTP server to verify real transport/tool/trajectory behavior; they do not establish
quality of any particular model or live isolation.
