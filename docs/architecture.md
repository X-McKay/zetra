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

Normal cleanup runs in the session Workflow. Explicit reconciliation schedules a
durable cleanup Workflow for terminal runs, including failed normal cleanup.
Callers wait for cleanup after cancellation or hard termination; status is read-only.
Confirmed deletion does not resolve unknown effects. Provisioning history with a
retired or unacknowledged create attempt prevents deletion proof: cleanup returns
`NOT_CONFIRMED` because a late create could still appear. Fencing those creates
requires a future independent reconciler; there is no continuous janitor today.

## Future external actions

MCP and external business actions remain outside the admitted profile. A future
AgentGateway integration must provide receiver-scoped identity and authorization,
with operation idempotency and reconciliation bound to the accepted descriptor. The minimal
integration seam is the existing tool dispatcher: a registered executor delegates
the authorized request and reports a definitive outcome or uncertainty. It needs
no second execution engine. Gateway transport cannot itself resolve an ambiguous
remote commit and does not replace Temporal's progress authority.
