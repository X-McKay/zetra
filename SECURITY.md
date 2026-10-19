# Security boundaries

Each session runs in its own OpenShell sandbox and writable volume. Landlock,
seccomp and agent-specific reviewed network policy constrain the workload. The
agent, bridge and tools share the sandbox's security domain. Python admission
checks are for cooperative registered code, not hostile arbitrary Python.

Temporal credentials and Activity completion authority remain external. The
single-host runner keeps a private session key and immutable artifacts in its
state directory. Preserve that directory to resume. A local advisory lock rejects
concurrent owners using the same state directory; it is not a distributed lease.
Stable session IDs and Workflow-start validation prevent a different local owner
from adopting the same Workflow. No execute Activity is automatically retried.
An effect with a lost completion is OUTCOME_UNKNOWN.

Terminal success requires confirmed immutable-instance deletion. Hard Workflow
termination bypasses Workflow cleanup. The active SDK runner (or an explicit
resumption using the same private session record) reconciles observed terminal
Workflows and persists cleanup separately from effect uncertainty. If that runner
is unavailable, cleanup still requires external reconciliation.
Continuous reconciliation, independent expiry/leases and multi-host fencing are
production gates. The standard runner uses an explicit loopback Temporal endpoint;
remote service identity, authentication and TLS need separate qualification.

The qualified profile is serial, nonstreaming and bounded to 64 model/tool
boundaries and one invocation. External business writes, arbitrary callbacks,
parallel tools and unmanaged background processes are not admitted. A tool effect
declaration cannot replace independent backend authorization.

Local fixture tests execute trusted project code on the host. Static check reads
metadata without importing code. Model evaluation, sandbox enforcement and
recovery evidence are different categories. Hashes bind data but are not trusted
signatures. Source labels in a locally trusted image builder are not production
provenance attestations.

Do not commit keys, session state, raw confidential traces or kubeconfigs. Report
security issues privately to the repository owner. Qualification proves resource
removal; it does not prove secure physical storage erasure.
