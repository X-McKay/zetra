# Shared agent authoring and platform execution

## Intent and authorized scope

Continue the accepted developer-platform roadmap with one Python factory, prompt,
registered tool set and execution core for default local, local Temporal and the
Agent Runtime Platform. Use the infrastructure investigator and a document-answering
assistant equally as reference workloads. Code and reviewed Git remain authoritative.
This increment prepares and tests changes; it does not merge PRs or activate cluster
stages. Stronger statistical evals, production rollout, telemetry and zetra-ui remain
subsequent milestones, rather than being represented as completed by this integration.

## Approaches and decision

Aliasing Tool types while extracting SDK tools into the platform transcript loop is
small but discards admitted ADK semantics and retains divergent execution. Teaching
the local SDK that second loop creates the same problem in reverse. The selected
implementation approach is an explicit versioned platform transport over the existing
Definition/Runtime/Dispatcher/Tape. This follows the approved one-core direction;
protocol details below are implementation decisions, not prior user-specified APIs.
Existing immutable legacy images and histories keep their old version. New migrated
agents use `zetra-core-v1`; do not translate legacy message arrays into operation tapes.

## Authoring and local bindings

Keep `zetra.AgentContext`, `zetra.Tool` and the admitted serial ADK LlmAgent factory.
Add `await ctx.mcp(backend, tool, arguments)` with an injected callable; no URLs or
credentials are available through this API. Unbound calls fail explicitly. Allow the
binding only while a registered remote_read executor is running, not from factory
construction, reconstruction or a workspace-only tool. Propagate it through every
Definition context. Required deployed tool/effect inventory must match exactly.

Local dev/eval/quality and local Temporal accept an explicit `--mcp-fixtures FILE`.
The bounded, strict version-1 JSON file maps exact backend/tool/argument triples to
JSON results; unknown calls fail. Responses are copied, deterministic and never reach
network. Bind its digest into runtime/report/quality identities and expose fixture
MCP provenance separately from model mode. Fixture MCP is never live-tool evidence.
No implicit fixture selection, credential fallback or automatic remote MCP connection.

Keep ChatExecutor's official ADK/OpenAI conversion and strict one-response/one-call
validation. Add an internal client-factory seam. The platform supplies AsyncOpenAI
through its fixed gateway Host, BearerAuth, CA validation, no redirects and zero
application-level retries. Existing one-refresh 401 handling stays bounded; 403 and
429 produce typed terminal refusals. Transport loss and arbitrary tool exceptions
remain unknown. Refusals do not create a candidate or increment accepted progress.
A model-only definitive refusal terminates with DENIED/BUDGET_EXCEEDED. A refusal
during a tool remains OUTCOME_UNKNOWN with a separate refusal reason, since prior
work inside that tool may have happened. Neither path retries or asserts rollback.

## Versioned deployment metadata

A schema-version-2 platform envelope references the installed SDK AgentSpec resource
(`package:resource`), and declares the deployed model binding and exact tools. The
project index and platform reference identify the same packaged definition. Platform
loading always supplies the gateway model binding; it cannot fall back to fixtures.
The envelope and referenced resources are in the immutable agent image. Validate
manifest size, resource paths, names, profile and tool inventory before any effect.

New AgentRelease.spec.runtimeProfile is optional, immutable, and admits only
`zetra-core-v1`; absence retains legacy. Bind a nonlegacy profile into the policy
bundle so existing signed policySha256 protects it. Do not change legacy digests.
Copy the CRD schema change to Kubani without enabling any stage. Controller-generated
pod configuration binds profile, image, manifest hash, policy hash, release and agent;
the runner supplies the entire core binding (namespace, Workflow/run, task digest,
session and generation) to the pod through a runner-controlled environment value.
Adoption requires that exact value; the guest compares every command against it.
Core workflows require their canonical workflow ID, attempt 1, original/first run
IDs equal to the current run, and no continuation or cron before activation.
Existing release outputs stay unapproved.

Preflight's legacy successful result remains null. For an explicitly core release,
return the verified immutable identity/configuration. The existing workflow branches
on this recorded result, preserving legacy command order and Activity types on replay.
Do not select a new runtime solely from an untrusted session argument. Keep old images
and worker builds available while retained histories need them.

## Core command and acceptance protocol

Use a new `agent_runtime.core_step` command, distinct from the legacy module, with
bounded length-prefixed JSON (1 MiB control limit). Core commands carry protocol,
immutable release/session/execution identity, generation, task, action, caller-selected
accepted Position, and a pinned operation for execute. The invocation identity includes
the exact Temporal namespace/Workflow/run and task digest; the manifest identity binds
image, policy and envelope. The guest checks its trusted pod binding before importing
agent code and applies existing confinement first.

Prepare is effect-free: reconstruct accepted tape and return either a captured operation
or completion. A zero-cursor initial workspace is captured explicitly. Persist the
prepared operation in Temporal before an execute Activity. Execute reconstructs the
same accepted prefix, compares the complete operation, writes a durable attempt marker
under an execution lease, and executes exactly one shared-core operation. Return a
candidate Receipt only; never advance against that candidate to infer completion.

The host owns accepted progress. The core object store lives under state/core/objects,
which is included in the existing workspace/state archive. Retain token.json exclusion.
Snapshot validation checks the exact predecessor, operation, candidate receipt and full
tape using shared SDK validation; verifies every referenced object and workspace; and
checks archive paths, duplicates, links, sizes and workspace equality. Persist the
hash-verified archive before returning its checkpoint to Temporal. Only then update
the workflow's accepted Position and boundary count. A subsequent prepare can report
completion without inventing or counting an additional effect.

Preparation loss may replace a sandbox only after confirmed deletion. Execute is one
attempt: timeout, transport loss, malformed output or an uncertain tool exception stops
with OUTCOME_UNKNOWN, without re-execution. Snapshot retries may re-upload the same
quiescent candidate; exhaustion never authorizes another effect. A newer local candidate
cannot select the accepted cursor. Unknown outcomes and integrity failures stay separate.

Keep platform preflight, APS generation revocation, ready checks, business deadlines,
cleanup, token/grant handling, encrypted task payloads and reconciliation ownership.
Use separate core Activity names and preserve legacy ones. Retain bounded exit meanings:
0 success, 2 effect-free completion, 3 terminal rejection, 4 budget, 5 pre-effect protocol
failure; an abnormal post-dispatch exit never proves no effect. Agent code remains
cooperative registered code inside the admitted sandbox trust domain.

## Reference agents and delivery

Migrate investigator factory and package metadata without expanding MCP capabilities.
Add a small user-facing document assistant using approved packaged documents, explicit
abstention and tests of tool paths, prompt injection boundaries and unsupported requests.
Both use the canonical indexed SDK definition, versioned platform envelope, normal
editable Python and quality contracts. Local infrastructure runs use reviewed synthetic
MCP fixtures. Keep live/model-quality claims distinct from deterministic checks.

Publish exact SDK prerequisites before dependency pins; validate actual installed Git
revisions and wheels, not a PYTHONPATH stand-in. Preserve unrelated dependency records
where resolution allows; record required changes. Update template guidance/pin only once
the needed SDK is reachable. Correct stale delivery/status docs while preserving the
original failures and historical qualification records.

## Acceptance and limits

Verify identical installed factories, prompts, schemas, operations and workspaces across
local fixture, real local Temporal and the platform core gateway fixture. Test typed
arguments, strict response validation, denial/budget, no fallback, and no agent imports
in static commands. Test accepted-prefix restore in a fresh process/generation, lost
execute completion, snapshot failures, wrong identity/operation, stale/duplicate execution,
corrupt artifacts and extra archive paths. Native Temporal replay must cover full legacy
and core histories, including replacement and cleanup; first-task replay is insufficient.

Run affected SDK, platform runtime/runner/contracts/controller, canonical starter and
agent repository checks; installed artifact validation; and final hosted CI. An independent
whole-change review precedes final publication. Native local tests do not replace Phase B
cluster qualification, hostile-code isolation testing or representative real-model evals.
