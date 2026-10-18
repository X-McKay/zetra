# Security boundaries

Each session runs in a fresh OpenShell sandbox and writable volume. Landlock,
seccomp and reviewed network policy constrain the workload. Agent code, bridge and
tool bodies share its security domain. Python admission checks apply to cooperative
registered code; they are not isolation against hostile Python.

Temporal credentials and Activity completion authority remain outside the sandbox.
Temporal history is the sole authority for accepted progress. Immutable artifact
bytes, including unaccepted candidates, do not select progress. Execute Activities
have one attempt; a lost completion remains `OUTCOME_UNKNOWN` and does not permit
automatic redispatch.

Callers configure Temporal TLS, authentication and payload codecs, an authenticated
OpenShell client, and durable artifact storage. Store methods are blocking,
thread-safe and hash/size verifying; SDK asynchronous paths offload their I/O to
threads. Protect retained artifacts and history according to their contents.
Neither hashes nor locally built image labels are production provenance signatures.

Workers on one queue must share a stable runtime identity, gateway identity,
artifact backing and persistent secret of at least 32 random bytes. Use separate
queues for different configurations. Per-execution capabilities derive from that
secret and exact execution identity; SDK sandbox authority labels reject a worker
with the wrong key before adoption or deletion. The configuration binding is a
caller contract. Key rotation is not versioned: retain the original key and runtime
configuration for retained executions. Align history and artifact retention with
recovery and cleanup requirements.

Handles pin exact runs. Workflow reset, retry, continuation and cron lineages are
unsupported. Replacement workers recover through Temporal and artifacts. Successful results require confirmed deletion of the
immutable sandbox instance. Cancellation and hard termination do not establish
that deletion or resolve effect uncertainty.

Explicit durable cleanup reconciles terminal executions, including normal cleanup
failures. Transient cleanup errors retry within a ten-minute Temporal window;
explicit gateway authorization/request failures stop promptly. Neither retries nor
elapsed time promote uncertainty to confirmed deletion. Hard-terminal callers must wait for that cleanup result. Status inspection
is read-only. A retired or unacknowledged provisioning attempt can outlive its
Activity; observed deletion alone cannot rule out a later sandbox creation. Such
history remains `NOT_CONFIRMED`. Native create IDs deduplicate a generation within OpenShell's
retention contract, but neither those IDs nor a client RPC deadline fence late
admission. Stronger closure requires a gateway-side lifecycle fence or platform
reconciliation; the SDK implements no continuous janitor.

The admitted profile is serial, nonstreaming and bounded to 64 model/tool
boundaries in one invocation. External business writes, MCP actions, arbitrary
callbacks, parallel tools and unmanaged background processes are not admitted.
Future AgentGateway integration requires scoped receiver identity, authorization,
idempotency and reconciliation bound to each operation. A gateway cannot resolve
commit ambiguity by itself or replace Temporal's acceptance authority.

Local fixture tests execute trusted project code on the host. Static checks read
metadata without importing factories. Evaluation, enforcement and recovery evidence
are separate. Do not commit keys, confidential traces, artifacts or kubeconfigs.
Report security issues privately to the repository owner. Resource deletion does
not prove secure physical storage erasure.
