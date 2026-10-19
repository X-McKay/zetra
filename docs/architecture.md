# Architecture

`SessionClient` submits and inspects exact Temporal executions. `create_worker`
returns an ordinary Temporal Worker that runs the provision/advance/execute/cleanup
Workflow. Callers supply configured Temporal and OpenShell clients, an artifact
store and a persistent worker secret. The worker reads data-only release metadata
and validates responses; agent factories and effect bodies run inside OpenShell.

Bridge model and tool wrappers describe each boundary. Temporal schedules its
execute Activity with one attempt. The sandbox produces a candidate receipt and
immutable artifacts. External validation checks predecessor, operation, contents
and workspace before completing the Activity. Only the position accepted in
Temporal history selects subsequent progress. Uploaded candidates cannot advance it.

A warm ADK continuation parks at each boundary. An authenticated advance carrying
the accepted position injects its recorded result. Cache loss reconstructs from
the accepted tape without invoking recorded effect bodies. Replacement first
confirms deletion of the old immutable sandbox instance. Unknown execute outcomes
remain unknown even when candidate bytes exist. One invocation admits at most 64
serial, nonstreaming model/tool boundaries.

`SessionHandle` pins namespace, Workflow ID, run ID and runtime identity. Loading
history validates the original start and rejects reset, retry, continuation and
cron lineages. Per-run capabilities derive from the worker secret, exact handle
and release. SDK sandbox labels bind authority, release, image and policy.
Workers validate those labels before adopting, using or deleting an instance.

Worker adapter caches are disposable and bounded; they are not durable ownership
records. Workers polling a queue must share its stable runtime identity, gateway,
artifact backing and derivation key. Different configurations require separate
queues. This dependency binding is caller-managed. TLS, authentication and payload
codecs belong to the supplied Temporal client; the caller owns connection lifetime.
Worker upgrades must preserve replay compatibility. `create_worker` accepts native
Worker options such as `deployment_config`; callers can use Temporal worker
versioning for rollouts. The SDK does not implement a deployment/version registry.
Key rotation has no versioned recovery mechanism yet, so retained executions need
the original key and configuration. Submission deduplication lasts only as long as
Temporal retains the original Workflow ID. A new run after retention is a fresh
invocation with a different sandbox identity, not a recovery of the expired run.

`ArtifactStore` exposes blocking `put(bytes) -> Ref` and `get(Ref) -> bytes`.
Implementations must be thread-safe, immutable and hash/size verifying. Async
worker paths offload store work and receipt validation to threads while heartbeat
coverage spans remote execution and validation. `FileStore` provides a local
implementation; the caller chooses durable backing accessible to replacement
workers. Artifact and Temporal history retention must cover the recovery lifetime.

Small artifacts travel inline within a bounded raw-byte budget. Larger objects use
verified transfer and chunked upload. Activity payloads are checked against the
configured converter's representation. Workspace snapshots reject links, special
files and unsafe paths before acceptance or restoration. Each provision creates a
fresh volume/runtime; writable agent state is not shared between sessions.

## Native create admission

Each sandbox generation uses a deterministic nonzero UUID request ID derived from
its per-run session identity and generation. Retries and replacement workers send
the same create payload through the caller's authenticated OpenShell connection.
An existing-name lookup validates authority but never substitutes for native replay.
The pinned OpenShell 0.1.4 curated Python create method omits `request_id`; one small
compatibility function uses its authenticated `_stub` and `_sandbox_ref` converter.
This private seam is covered by transport and real-gateway conformance tests and
must be requalified when upgrading OpenShell. It adds no service or connection.

One logical Temporal provision/rebind Activity owns each generation. The adapter
checks its original `scheduled_time + 600 seconds` immediately before create and
passes the remaining time as the RPC deadline. Retries and fresh workers cannot
renew that window; a new Activity must not schedule the same generation anew.
Standalone internal fixtures use a window fixed when their adapter is constructed.
The check is a client bound, not a server fence: a paused caller or admitted request
may still finish later.

[OpenShell native admission](https://docs.nvidia.com/openshell/v0.1.4/sdk/api-errors#durable-request-admission)
replays the original successful create for 24 hours after completion. After that
receipt expires, the same ID can execute again. An unresolved admission returns `REQUEST_OUTCOME_UNCERTAIN` and does not automatically expire or
transfer ownership. A deleted original returns `REQUEST_REPLAY_UNAVAILABLE` during
receipt retention; the SDK never switches IDs to recreate that generation. Changed
payloads or authority failures are not treated as permission to allocate another
sandbox. Identity, gateway scope and payload must remain stable across recovery.
OpenShell records resource admission; Temporal remains the sole authority for
accepted agent progress and execution recovery.

The gateway permits at most 1,000 admission records per authenticated caller.
Successful create records consume this quota for 24 hours even after sandbox
removal; unresolved records persist. Each new generation consumes one record (up
to 64 in the admitted session profile). This is a gateway capacity constraint for
operators, not an SDK registry. Stop/delete continue using immutable-instance
confirmation and do not allocate request IDs.

Normal cleanup runs in the session Workflow. Explicit reconciliation schedules a
durable cleanup Workflow for terminal runs, including failed normal cleanup.
Both paths retry transient cleanup failures with Temporal's exponential backoff
(default one-second initial interval, capped at 30 seconds), within the existing
ten-minute schedule-to-close window. Each attempt has a two-minute limit and
heartbeat coverage. No attempt-count cap cuts that window short. Explicit gateway
permission, authentication, invalid-request, failed-precondition and unsupported
operation errors stop retries at the cleanup boundary; identity conflicts remain
terminal. A cleanup Activity result of `NOT_CONFIRMED` does not trigger Activity retry.
The explicit cleanup Workflow fails non-retryably for any result other than
`DELETED`. Its ID reuse policy permits only failed runs to be replaced; Temporal
therefore preserves successful deletion proof atomically across concurrent callers.
Clients validate the exact cleanup run's input and lineage before reusing it.
This proof is available only while Temporal retains its history.
A provisioning or rebind Activity with a recorded attempt above one permanently
prevents deletion proof for that session, even if the later native replay succeeds.
An otherwise successful agent run then reports `REMEDIATION_REQUIRED`. This is
conservative late-admission uncertainty, not evidence that a sandbox remains.
Model/tool execution still has exactly one Activity attempt. Callers can explicitly
reconcile a terminal execution after correcting a failure or restoring access.

Callers wait for cleanup after cancellation or hard termination; status is read-only.
Confirmed deletion does not resolve unknown effects. Provisioning history with a
retired or unacknowledged create attempt prevents deletion proof: cleanup returns
`NOT_CONFIRMED` because a late create could still appear. Native request IDs reduce
duplicate creation but do not close admission or certify that outstanding work has
stopped. A stronger guarantee requires a gateway-side
lifecycle fence or platform reconciliation; there is no custom janitor in the SDK.
The [draft upstream proposal](openshell-admission-closure-proposal.md) requests a
native close-create-admission operation, including absent-request tombstones and
backend settlement/fencing. It is not an implemented capability or a cleanup claim.

## Future external actions

MCP and external business actions remain outside the admitted profile. A future
AgentGateway integration must provide receiver-scoped identity and authorization,
with operation idempotency and reconciliation bound to the accepted descriptor. The minimal
integration seam is the existing tool dispatcher: a registered executor delegates
the authorized request and reports a definitive outcome or uncertainty. It needs
no second execution engine. Gateway transport cannot itself resolve an ambiguous
remote commit and does not replace Temporal's progress authority.
