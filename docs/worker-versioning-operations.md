# Worker Versioning operator

`agent-worker-versioning` is an operator-only CLI. It never scales or deletes a
worker, namespace, Deployment version or workflow. Its mutating commands are
`promote`, `rollback` and `schedule-route`; each requires a private immutable plan
and a new durable JSONL journal. Every pending record is flushed/fsynced before
its RPC. Uncertain results exit 3, are never retried automatically, and require
readback reconciliation. Guard refusals exit 2. Plans/results are mode 0600 and
never overwrite an existing file. Raw RPC errors are reduced to their type.

Use a verified installed image containing this CLI with its matching source.
Connection uses the same tenant mTLS and AES-GCM codec as the runner. Kubernetes
inventory needs only get Deployment/ReplicaSet and list Pod permissions in
agent-platform, not workload mutation or Secret reads. The optional Kubani
operator template projects existing TLS/payload-key files read-only, grants only
that inventory Role, and waits idle for explicit root commands. No cluster
operation has been performed by these local tests.

`current --role sessions|cleanup --pool legacy|governed-v1 --image PIN` binds a
worker set to exact Temporal routing and fresh conflict token, owner UID chain,
installed image/config, and both Workflow/Activity pollers. A route change during
discovery fails; an arbitrary matching Pod is not current-worker evidence. Legacy
results stay unversioned. Existing conformance observers still submit legacy
payloads by default; their legacy app selectors must not be represented as
observations of governed workers. Explicit governed observers must use this exact
current-version discovery path before their qualification evidence is credited.

`inspect-drain --role ROLE --build-id BUILD` combines the server's real drainage
state, current routing, namespace retention configuration and bounded visibility
with direct per-run queue/version correlation. Temporal1.29.1 visibility uses
`TemporalWorkerDeploymentVersion = "deployment:build"`, unlike deprecated API
version strings `deployment.build`, as defined in the pinned
[primary implementation](https://github.com/temporalio/temporal/blob/v1.29.1/common/worker_versioning/worker_versioning.go).
Empty visibility never substitutes for DRAINED or proves submission closure.
The report always says `retirement_allowed: false`: a point observation cannot
prove continuous seven-day query/replay availability, and this CLI has no worker
retirement command. Preserve all compatible workers and artifacts.

`history-receipt --role ROLE --build-id BUILD --image PIN --workflow-id ID
--run-id UUID` projects only actual WorkflowTaskCompleted structured versions and
ActivityTaskStarted build/identity fields, bound to exact retained Pod UIDs/images
and fresh versioned pollers before/after the read. It never outputs event payloads
or unmatched identity/version strings. A failed correlation exits1. The deprecated
WorkerVersionStamp.use_versioning flag is false for the observed Deployment-based
workers; it is not substituted for structured deployment versions, exact Activity
build IDs and owned poller identities. The
[real local receipt](qualification/worker-versioning-history-receipt-local-2026-11-06.json)
contains four correlated Workflow tasks and two Activity starts. Its initial
strict-flag assertion failed and led to a regression matching the actual pinned
API; no missing version/identity evidence was accepted.

`replay --role ROLE --build-id BUILD --workflow-id ID --run-id UUID` verifies an
exact closed run's queue and structured version before fetching history. It
replays privately against this installed code with the tenant codec; raw history
is neither written nor printed by the wrapper. Output includes only identifiers,
event count, history digest and workflow source digests. Run it inside each
verified retained source/image when qualifying that source; a successful replay
against some other installed image does not prove retained-binary equality.
Open, unknown-status, mismatched-run/queue/version/namespace and overbound
histories fail. SDK nondeterminism remains a failure, never a replay pass.

Example commands inside the reviewed operator environment (all pins are exact
published values, not moving tags):

```sh
agent-worker-versioning --tenant tenant-test current --role sessions \
  --pool legacy --image "$VERSIONING_LEGACY_IMAGE" --output /tmp/versioning/legacy-sessions.json
agent-worker-versioning --tenant tenant-test prepare --role sessions \
  --build-id "$VERSIONING_BUILD_A" --image "$VERSIONING_IMAGE_A" \
  --output /tmp/versioning/promote-sessions-a-plan.json
agent-worker-versioning --tenant tenant-test promote \
  --plan /tmp/versioning/promote-sessions-a-plan.json \
  --journal /tmp/versioning/promote-sessions-a-journal.jsonl \
  --output /tmp/versioning/promote-sessions-a-result.json
agent-worker-versioning --tenant tenant-test schedule-inspect \
  --output /tmp/versioning/cleanup-route-plan.json
agent-worker-versioning --tenant tenant-test schedule-route --pool governed-v1 \
  --build-id "$VERSIONING_BUILD_A" --image "$VERSIONING_IMAGE_A" \
  --plan /tmp/versioning/cleanup-route-plan.json \
  --journal /tmp/versioning/cleanup-route-journal.jsonl \
  --output /tmp/versioning/cleanup-route-result.json
```

Prepare/promote cleanup separately before routing its future Schedule. Rollback
uses a newly prepared retained candidate plan and the identical guarded promotion
path; it never changes existing pinned histories. Future Schedule rollback uses
a fresh `schedule-inspect` plan and `schedule-route --pool legacy`, which requires
healthy retained legacy workers/pollers. Preserve opaque Schedule payloads and
five-minute/SKIP settings. RPC acknowledgment is not Schedule update evidence;
exact readback is required.

## Local evidence and remaining gates

[Operations evidence](qualification/worker-versioning-operations-local-2026-11-06.json)
records actual Temporal1.29.1 DRAINING while a pinned run is open, then DRAINED
after the unchanged default three-minute visibility grace. A's Activities remain
on A after B promotion; after guarded rollback to retained A, new work uses A
while B's existing pinned history still finishes on B. A's closed history replays
and remains immediately queryable. The
[negative](qualification/worker-versioning-replay-negative-local-2026-11-06.json)
uses intentionally incompatible code and fails with NondeterminismError.
Kubernetes inventory is explicitly synthetic in this local integration; exact
UID/image/selector checks have separate unit tests. No local source image was
published, and these results are not production qualification.

The [initial diagnostic failure](qualification/worker-versioning-operations-diagnostics-2026-11-06.json)
is retained: a dot-delimited visibility query missed an open run. Verification of
the pinned implementation and actual metadata led to a colon-delimiter regression
and a fresh successful run. The exact failed local fixture was terminated,
retaining its failed diagnostic history; it is not relabeled a passing run.

The local namespace is configured for only 86,400 seconds of retention. Both the
at-least-seven-day configuration check and seven-day observation remain false,
even after real DRAINED. No clock fast-forward, shortened server grace, empty
visibility inference or worker removal is used to manufacture those gates. Live
qualification still needs exact published/source equality, real Kubernetes
ownership/network/storage evidence, actual SessionWorkflow/CleanupReconciler
histories, future Schedule cadence, and retained query/replay observations through
the seven-day window. Root owns that coordinated plan and all live mutations.
