# Draft upstream RFC: close sandbox create admission

Status: local proposal for upstream discussion; not submitted and not implemented.
Baseline: OpenShell v0.1.4. All API names and guarantees introduced below are
proposed, not descriptions of the released API. No cluster qualification was run
for this document.

## Problem and requested guarantee

A durable orchestrator must be able to retire one exact sandbox generation even
when its create RPC has no acknowledged result. Observing that no sandbox exists,
cancelling an RPC, or deleting a known sandbox cannot prove that a delayed create
will not subsequently produce another resource. We request a native, durable
**close-create-admission** operation for an existing create request identity,
including identities that have never reached the gateway.

After `CLOSED` is returned, no request bearing that identity may create another
sandbox or associated backend resource. The response must enumerate every
immutable resource identity that the admitted create did produce. The caller then
uses ordinary immutable-instance deletion and its existing confirmation protocol.
Admission closure and confirmed deletion are different facts; neither establishes
that an agent's external effects did not happen.

Zetra derives one nonzero native create UUID from each exact Temporal run and
sandbox generation. Temporal is its sole progress engine. Replacement workers
reuse that UUID and payload. This proposal should let a small SDK adapter close
that generation and delete returned identities; it must not require an SDK
registry, leases, ownership takeover, continuous janitor, or second workflow engine.

## Released behavior and sources

The [v0.1.4 durable admission contract](https://docs.nvidia.com/openshell/v0.1.4/sdk/api-errors#durable-request-admission)
scopes request IDs to authenticated identity provider, issuer, subject, method and
requested workspace. It admits a durable owner before mutation. Cancellation or
disconnection does not stop admitted work. Errors, interrupted processes and
failed success recording leave an unresolved admission; no replica automatically
takes it over, even after restart or 24 hours. Successful receipts replay for 24
hours from durable completion, after which the same ID can execute again.
Unresolved admissions never expire. Deletion of the original sandbox can make
create replay unavailable during retention without making a new create safe.

Primary implementation references pinned to the baseline:

- [API error contract source](https://github.com/NVIDIA/OpenShell/blob/v0.1.4/docs/sdk/api-errors.mdx): scope, cancellation, retention, errors, quota and key material.
- [Mutation admission implementation](https://github.com/NVIDIA/OpenShell/blob/v0.1.4/crates/openshell-server/src/grpc/mutation_replay.rs): success TTL and per-caller admission bound.
- [Ordinary mutation receipt handling](https://github.com/NVIDIA/OpenShell/blob/v0.1.4/crates/openshell-server/src/grpc/mutation_replay/ordinary.rs): actual-write resource facts and immutable references.
- [Sandbox creation handlers](https://github.com/NVIDIA/OpenShell/blob/v0.1.4/crates/openshell-server/src/grpc/sandbox.rs): workspace checks and create path.
- [Workspace lifecycle](https://github.com/NVIDIA/OpenShell/blob/v0.1.4/crates/openshell-server/src/grpc/workspace.rs): termination checks, blocker scan and conditional deletion.
- [Public protobuf](https://github.com/NVIDIA/OpenShell/blob/v0.1.4/proto/openshell.proto): baseline request and response surface.

These sources were inspected through `git show v0.1.4:<path>` in the pinned
checkout. They establish existing admission deduplication, not the closure
invariant requested here.

## Minimal proposed surface

Add one idempotent RPC, schematically:

```text
CloseSandboxCreateAdmission(workspace, create_request_id)
  -> { state: CLOSING | CLOSED,
       resources: [immutable sandbox/backend resource references] }
```

The request selects the original `CreateSandbox` admission namespace, not a new
close request-ID namespace. Duplicate close calls inspect or advance the same
closure record, so no second idempotency key is needed. A bounded RPC can return
`CLOSING`; invoking it again obtains current durable closure progress. A separate
watch API is unnecessary for the initial feature. Fields are illustrative; wire
names, stable resource-reference format and structured error details need upstream
agreement.

`CLOSING` means a durable seal prevents fresh admission, but admitted work may
still be able to create resources or its effects are unresolved. Returned resources
are a monotonic partial set and cannot establish complete cleanup. `CLOSED` means
closure is durable, every possible effect-producing path is settled or fenced,
and the returned set is complete and immutable. A closed empty set explicitly
proves that this request can produce no resources. Closing never creates a
sandbox, volume, workspace or other workload resource, and never replays create
to discover its result. Persisting control-plane tombstones is necessary.

A subsequent create against a sealed identity returns a typed
`CREATE_ADMISSION_CLOSING` or `CREATE_ADMISSION_CLOSED` rejection, rather than
falling through to ordinary receipt expiry or allocating a new owner. Successful
close must remain queryable after the original sandbox is deleted. A transport
error, authorization denial or unsupported-feature response is never evidence of
closure. Clients must detect server capability explicitly; older servers ignoring
unknown protobuf fields cannot silently satisfy this contract.

## Required state and ordering

Use a durable per-identity state machine with absent/open, admitted, closing and
closed states. An absent request can transition directly to closed with an empty
resource set: this is the critical absent-request tombstone. It wins against a
sender paused before dispatch, including a request delayed beyond 24 hours.

Admission and closure must serialize on the same authoritative durable key.
Fresh admission is allowed only while that key is open. Seal installation must
also serialize with the final resource commit and every effect-producing backend
step; an ingress-only check leaves a check-then-write race. There are two valid
orderings:

1. Closure wins before admission: persist the tombstone, return closed empty,
   and reject all later creates for that identity.
2. Admission wins: persist closing, deny new owners, settle the original work or
   fence it from all future backend effects, collect its complete resource set,
   and only then durably publish closed.

The implementation must record ownership and resource attribution before an
ambiguous backend boundary can lose them. A gateway database transaction alone
cannot atomically fence a remote driver. An admitted request paused before a
backend call, a driver create whose reply is lost, or a gateway crash between
resource creation and receipt persistence must not produce a closed empty result.
Backend operation identity and reconciliation, or an enforceable driver fencing
epoch, may be necessary. Their implementation belongs in OpenShell's lifecycle
boundary. This RFC does not presume that current drivers supply it.

Durable closure must survive process restart, database failover and concurrent
gateway replicas. Old owner tasks and stale replicas must be unable to commit
resources after closed. A local mutex, cancelled task, RPC deadline, lease timeout,
negative resource listing, or process death is insufficient evidence. Fencing
must cover backend work already dispatched as well as gateway writes. Record all
resulting immutable sandbox and associated resource identities, including partial
allocations and allocations never returned to the original caller; define which
references ordinary deletion subsumes and which require explicit cleanup.

Unknown backend outcome can keep closure in closing indefinitely. Do not relabel
uncertainty as closed after a timeout, and do not transfer execution ownership or
rerun create to resolve it. A partitioned or non-fenceable backend imposes an
honest liveness limit: safety may require operator investigation and upstream
reconciliation. Define that operational path without promising bounded completion
under arbitrary failures.

## Retention and bounded storage

The current 24-hour successful replay TTL cannot be the lifetime of a safety seal:
expiring a tombstone and then treating an old UUID as new reopens admission.
Likewise, unresolved admissions cannot be garbage-collected merely because their
clients appear dead. The minimal proposal retains compact closed tombstones
permanently, with an explicit capacity and operational storage model. This is a
storage cost of the guarantee, not an SDK responsibility.

### Future option: enforceable expiry

If permanent retention becomes impractical, a later protocol could introduce a
server-enforced admission expiry bound carried by every create and bound
immutably to its identity and payload. The system must prevent new admission and
future backend effects after the enforced expiry bound. Closure may finish only
when earlier backend effects are settled or irrevocably fenced. A seal can be
garbage-collected only when no old request can ever be admitted or commit again.

A client deadline or an unsigned client timestamp is not such an expiry contract.
Expiry validation must still work when the admission row is gone: an old caller
must not extend a bound for the same identity and bypass the seal. This likely
requires a server-issued authenticated admission envelope or another verifiable
binding. Clock skew, offline requests, key rotation and supported maximum lifetimes
would need explicit rules. These are future protocol questions, not prerequisites
for the minimal permanent-tombstone proposal. Expiry alone does not settle
already-created or unknown resources.

## Security and capacity

Close must authenticate and authorize against the same principal and requested
workspace scope as the original create, preserving immutable workspace incarnation
binding so same-name replacement cannot redirect authority. It must work for
absent admissions and deleted original resources without leaking another caller's
identities. Define a least-privilege close/reconcile permission, current membership
checks, and an audited administrator recovery path for revoked/deleted principals.
Do not require workspace-administrator authority merely to retire an authorized
create. Never silently widen close into a workspace-wide kill operation.

The baseline has 1,000 durable admissions per caller; successful receipts occupy
quota for 24 hours and unresolved rows persist. Close-first tombstones create a
new storage-abuse vector. Specify charging, bounded payload/resource-reference
size, rate limits and retention accounting. Closing an existing admitted request
must not need a fresh admission slot that a full quota prevents allocating;
reserve or transform its existing record. A rejected absent close at quota must
return an explicit failure, never closed. Capacity exhaustion must preserve seals.

Baseline protected fingerprints derive from gateway JWT or TLS private material;
replicas must share it and key changes can make existing receipts unavailable.
Closure identity and authorization must remain interpretable across supported
rotation. Choose versioned verification material or a durable stable binding,
including migration, rollback and retained old-key requirements. Key rotation,
workspace replacement, changed interceptors or changed payloads must never reopen
a sealed identity. Avoid storing credential-bearing create payloads in receipts.

## Alternatives and SDK integration

Deleting a dedicated workspace is worth investigating, but is not a demonstrated
per-create fence. v0.1.4 checks workspace termination before create and scans
blocking resources before deleting a workspace. A request can pass a check and
pause before a later write or driver call. The visible checks do not establish a
transaction or backend fence spanning that interval. Prove that invariant before
using workspace deletion for admission closure; do not infer it from a comment
that new creates are rejected. Workspace deletion also has broader privileges and
scope, rejects the default workspace and encounters blockers. Names and a later
`NOT_FOUND` observation do not identify all effects of an earlier create.

A TTL by itself, native replay, list-and-delete, name lookup, client cancellation,
or a continuous client janitor cannot establish the proposed closure guarantee.
A platform reconciler could implement the required durable fencing and settlement,
but it should expose this narrow native contract rather than delegate ownership
to every SDK.

After upstream support and qualification, Zetra cleanup can close each known
per-generation UUID, wait durably for closed, delete the returned immutable
instances and confirm deletion. Temporal records this sequence and remains the
only authority for accepted agent progress. Existing unknown effect outcomes stay
unknown. Until support exists, retired or unacknowledged create attempts continue
to produce `NOT_CONFIRMED`; no documentation claim or native replay result upgrades
them to confirmed cleanup. No production-code change is part of this proposal.

## Acceptance tests required upstream

Use deterministic barriers and fault injection, assert backend allocations and
immutable identities, and exercise both persistent-store modes and supported
replica/driver configurations. Observe beyond the final barrier; a successful RPC
response alone does not establish absence of late work.

| Interleaving / fault | Required observation |
|---|---|
| Close first, create not yet dispatched | Closed empty tombstone; released sender cannot allocate. |
| Pause before dispatch, resume after close and after 24-hour replay expiry | Same rejection; no fresh admission despite receipt GC. |
| Pause just after admission, before any resource work | Close remains closing until settled/fenced; resumed owner cannot violate closed. |
| Pause before resource/receipt persistence | Close includes actual allocations or remains closing; never infers empty from missing row. |
| Pause before and during driver create; lose driver reply | Reconcile every backend identity or remain closing; no second allocation to probe outcome. |
| Pause immediately before returning create success | Close returns complete immutable set even if client never receives success. |
| Gateway crash at each above barrier, then restart | Durable seal and attribution survive; no automatic rerun or false closed. |
| Two replicas race create/close; stale owner resumes | One serialized result; stale owner fenced at commit and backend boundary. |
| Duplicate/concurrent close, lost close reply | Same seal and monotonic set; closed set never changes; no workload creation. |
| Delete known sandbox before close; successful receipt expires | Closure still accounts for historical identities and rejects late create. |
| Backend unavailable or outcome unknowable | Typed closing/uncertainty persists; timeout never implies closed. |
| Same UUID, different caller/method/workspace or replacement workspace | Correct scope isolation and authorization; no cross-principal disclosure or accidental reopen. |
| Changed payload/interceptor, revoked permission, gateway key rotation | Explicit authorized/error behavior; no effect re-execution and no seal loss. |
| Full quota, repeated absent close and tombstone GC | Existing close can progress; abuse bounded; GC never permits old identity reuse. |
| Enforced expiry design, if selected: altered deadline, skew, old keys, delayed driver | Old requests cannot extend lifetime or commit after fencing; unsafe GC is rejected. |

## Decisions requested from upstream

Confirm whether create admission is the right native lifecycle boundary and which
backend identities must be returned. Confirm permanent tombstones for the minimal
proposal; defer a complete server-enforced expiry protocol unless retention cost
requires it. Define driver settlement/fencing capability and
which failures can remain closing indefinitely. Agree on response/error types,
authorization after workspace/principal removal, quota treatment and rotation
migration. Then implement the smallest RPC and storage/driver changes that satisfy
the tests; qualify the exact released server/driver combination before any SDK
claims cleanup closure.
