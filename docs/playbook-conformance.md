# Playbook conformance

Implementation, observations and production requirements are separate. The SDK
admits one static, serial, nonstreaming ADK invocation with at most 64 model/tool
boundaries. Current qualification results are recorded in
[qualification](qualification.md), including fresh test and benchmark evidence
and the limitations that remain before production deployment.

| Area | Implementation | Boundary or remaining requirement |
| --- | --- | --- |
| Agent contract | Strict manifest, immutable release, typed shared capture/executor registration | Static checks do not execute factories; cooperative registered code |
| Accepted progress | Temporal history selects the accepted position; candidate artifacts cannot advance it | Exact original run only; reset, retry, continuation and cron unsupported |
| Effects | One execute Activity attempt; warm continuation and cold replay | Lost completion remains unknown; recorded effects are not redispatched |
| Worker replacement | Ordinary Temporal Worker; disposable per-execution caches | Shared caller-managed runtime, gateway, artifacts and persistent key per queue |
| Session isolation | Fresh sandbox/workspace; authority/release labels; immutable deletion confirmation | OpenShell/Linux enforcement requires scoped qualification; tools share agent trust domain |
| Artifact storage | Caller `ArtifactStore`; blocking thread-safe immutable hash/size-verified methods | Caller durability and aligned artifact/history retention |
| Connection security | Caller-configured Temporal client and OpenShell client | TLS, authentication, codecs and secret management are caller responsibilities |
| Create admission | Native OpenShell request ID per generation; bounded original Activity window | 24-hour successful receipt retention, unresolved admission and shared caller quota; no lifecycle fence |
| Lifecycle | Normal and explicit terminal cleanup share bounded Temporal retries and permanent-error handling; read-only status | Late provisioning uncertainty remains `NOT_CONFIRMED`; [native admission-closure proposal](openshell-admission-closure-proposal.md) is unimplemented |
| Worker rollouts | Native Temporal Worker options | Replay-compatible changes and native deployment versioning require rollout qualification |
| Key lifecycle | Per-execution capability derivation from persistent worker secret | Rotation is not versioned; retain original key and runtime configuration |
| Evaluation | Fixture and sandbox modes; release-bound evidence | Evidence is unsigned and observations remain scoped |
| Developer workflow | Canonical nested template; data-only agent index; packaged instructions; one project lock used by image build | Optional application layers are not generated; local kind harness is separate from production deployment |
| Build/evidence identity | Image/source/manifest/lock/SDK revision binding; eval agent and dataset identity | Unsigned consistency checks; OS and build-system inputs are not bit reproducible |
| External actions and MCP | Reviewed `remote_read` tools admitted; writes excluded | Native provider policy and backend authorization are deployment-owned; writes require idempotency and reconciliation |
| Multi-agent execution | Not admitted | Requires a separately admitted execution design |
| Agent Runtime Platform | [Kata, gateway, policy service and runner](platform.md) built alongside OpenShell | Unit and integration tests only; no cluster qualification yet |

A future AgentGateway should attach through the existing tool dispatcher rather
than adding an execution engine. It must enforce receiver-scoped capabilities and
cannot resolve ambiguous remote commits alone or replace Temporal's accepted
progress authority. Production deployment, signed provenance and continuous
lifecycle operations require independent design and qualification.

## Agent Runtime Platform image qualification

Kubani's Phase B run 12 (2026-11-06) still failed T26 before either seeded session
accepted boundary 0. The `12369f4` image had the workspace-only restore fix in
`/app/runner`, but its console entrypoint imported an older wheel from
`site-packages` that restored the donor's `state/` too. A diagnostic using the
source copy restored no `state/` and completed boundary 0. The same diagnostic
through the installed package restored donor state and returned rc=5 with
`boundary mismatch`; both sandboxes were deleted. This is not a T26 pass.
The runner image build now explicitly reinstalls the local package and compares
every installed Python module with the copied source under isolated Python.
Protocol failures also record only known framing or boundary-mismatch diagnostics,
never arbitrary guest stderr or stdout. A rebuilt image must be deployed to the
runner, cleanup reconciler and harness before the next T26 qualification run.

The Phase B harness now implements T6 direct engine authentication and T7 model
request capture. T6 requires unauthenticated/authenticated statuses 401/200;
T7 requires session-correlated chat completions, a locally compared static
backend key, absence of an upstream agent JWT, and nonzero committed and gateway
usage. Capture records contain only the key-match boolean, never key material
or its hash. Local HTTP and capture tests exercise refusals, missing evidence and
credential hygiene; they do not establish a live T6/T7 qualification pass. See
[model authentication qualification](platform.md#model-authentication-qualification-t6t7)
for the required mounts, vantage point and capture permissions.
The capture image build uses the target OS and architecture explicitly, so an
ARM64 model-side image can be built on an AMD64 builder without emulation.

T4 now requires exact runtime token claims from the verified committed
snapshot, including both audiences, issuer, agent identity, client type,
300-second lifetime and a JTI tied to the current sandbox generation. T5
authenticates the actual API-audience assertion to Kubernetes first and requires
a signed, unexpired, explicitly configured human fixture for its human-token
case. Both must receive 401 `invalid_client`; missing human evidence cannot
produce a full T5 pass. Previous T4/T5 harness passes did not establish these
stronger checks. New live qualification is required; the local tests use
controlled API/JWKS endpoints and do not constitute genuine live human evidence.

T19 now requires the full 50-cycle MCP alternation in ten seconds with one
pinned token, plus two allowed/forbidden model-route cycles. The harness reads
actual HTTP statuses and monotonic timing from the accepted snapshot's tool
result and correlates all 104 requests with distinct APS decisions and token
`jti`. Model settlement pauses are outside the MCP interval, and 429s remain
failures. Prior five-cycle runs do not establish this stronger gate; the changed
conformance agent and harness require a new live qualification run.

## Review hardening

Worker release allowlists restrict active operations while preserving revoked-release
cleanup. Temporal's failed-only cleanup ID reuse preserves successful deletion
proof across concurrent callers. Standard wheel metadata preserves SDK Git pins;
canonical rendering rejects reserved names and rolls back failed output. Ordinary
agent images exclude build-only Git and qualification canaries. Current test and
benchmark evidence is recorded in [qualification](qualification.md).

Tool registration rejects the pinned ADK's injected context and stream parameters,
including typed aliases, before execution. The executor continues to accept only
model-supplied arguments; no ADK context injection is implemented.

The Kubani MCP POC adds an explicit `remote_read` tool classification and native
OpenShell provider-name attachment in worker configuration. Providers remain a
caller-managed trust boundary: their policies and credentials are not signed or
frozen by Release metadata. Recorded kind qualification predates these additions;
the Kubani POC must supply separate evidence and use its own runtime/queue.

The 2026-10-19 review pass covers 279 distinct passing cases across runs,
including the live model, with all three earlier failed attempts disclosed.
Provisioning retries correctly preserve `NOT_CONFIRMED` despite later execution
success. Latency and fresh-provisioning benchmarks passed; the report makes no
single-clean-full-run or statistically significant performance claim.

Kubani live qualification exercised the real MCP provider, encrypted native
credential refresh, whole-agent sandbox execution and Temporal worker/checkpoint
recovery. The POC report is maintained in X-McKay/kubani PR219. Remote reads are
admitted; external writes remain excluded. Endpoint policy does not enforce MCP
JSON-RPC arguments: the reviewed tool wrappers and backend authorization remain
separate trust boundaries.

The live POC exposed a native cancellation/completion race: Temporal's
WAIT_CANCELLATION_COMPLETED can return a successful Activity after cancellation.
SessionWorkflow now checks native cancellation before further work and after
cleanup. A native Temporal patch marker preserves replay of recorded histories.
Four real-server regressions cover successful provision, advance, execute and
cleanup completion after cancellation, including an empty cancellation reason.
They require CANCELED, no subsequent business boundaries, cleanup and replay.
The original failed live cancellation observation is retained in the POC report.

The shared CLI forwards native provider names, release approvals and Temporal
graceful shutdown. Run exports save the exact handle before waiting, publish
verified workspace files separately from metadata, and use a static local export
ownership marker plus path-overlap guards. These receipts do not select progress
or implement recovery; Temporal and immutable artifacts remain authoritative.

The shared-CLI follow-up passed the full locked local gate (272 cases) and all
42 dedicated kind cluster cases, including live vLLM, provisioning/recovery,
cleanup, cancellation and history replay. Existing immutable sandbox images keep
their recorded pins; the updated external worker runs this SDK revision. Kubani
uses a newly built agent image pinned to the CLI implementation and supplies
separate real MCP qualification. Test-only model/tool completion gates kill the
exact worker child before Activity acceptance; no production fault hook is added.


### Two-phase provisioning

Creation acknowledgement and sandbox readiness are separate Temporal boundaries.
The Workflow records the immutable binding before waiting for readiness; cold
workers use that binding without creating another sandbox. Existing histories use
native Temporal patching and thin combined Activity wrappers. Readiness failures
and cancellations retain normal confirmed-deletion checks. Unacknowledged or retired
create attempts still yield `NOT_CONFIRMED`; the original Kubani supervisor-start
failure remains a failed observation. No gateway-side admission closure is claimed.


Qualification of this split: 285 local tests passed; 46 native cases passed across
42 passes in the initial full run and four cases on a focused rerun. Five focused
creation/readiness recovery cases and five Temporal cancellation cases passed.
The original failed history replays unchanged. All three benchmark cases and
eight fresh Kubani qualifications passed, with final native/Kubernetes session
resource absence and the exact new-image native credential/isolation control.
See [the qualification report](qualification.md) for retained initial failures,
compatibility tuples and measurement limits.

### Agent Runtime Platform admission harness

`platform/runner` adds opt-in T9a, T9b and T22 cases to `agent-conformance`.
These are implementation and local-test coverage; they establish no live-cluster
pass until a retained report records the expected admission evidence. T9b requires
the named Kyverno policy and rule. Every
malformed Pod and ephemeral-container request uses server-side dry-run. T22's
attach request targets a real, valid runner sandbox, which is cancelled and
confirmed absent afterwards. Generic RBAC or API errors never count as admission
enforcement; each batch first requires its signed positive control to be admitted.

T22 can run with the normal `--agent` and `--release`. T9a/T9b additionally require
`--admission-fixture-agent`, `--admission-fixture-release`,
`--admission-unsigned-release` and `--allow-admission-mutations`. The mutable fixture
must be a different agent from the main conformance agent. Its signed release must
be approved, signature-verified, Ready and drift-free; its unsigned release must
be approved, controller-confirmed not signature-verified, not Ready and drift-free.
The harness neither creates releases nor writes status. It temporarily changes
the signed fixture's approval or Agent capabilities, waits for controller-produced
state, and restores the exact original spec with resourceVersion concurrency
checks. A concurrent spec edit makes restoration fail visibly instead of being
overwritten. Run these tests exclusively on that fixture and retain the restoration
result. T9a separately proves generation zero, preflight `Forbidden`, no scheduled
pod-creation activity and no session pod; a real signed runner sandbox is its
positive control.

Required tenant RBAC is Pod `create` (dry-run), `list`, and
`pods/ephemeralcontainers` `patch` (dry-run), ConfigMap `get` restricted to the
fixture's pod-template ConfigMap, Agent `get`/`patch` restricted to the mutable
fixture, AgentRelease `get` restricted to the signed/unsigned fixture releases,
and AgentRelease `patch` restricted to the signed release. No status permission
is required. Existing Temporal client access remains necessary. Kubernetes RBAC
cannot restrict a verb to dry-run; this guarantee is in the harness implementation.

Run15 exposed that Pod Security Admission rejects T22's hostPath and hostNetwork
cases before Kyverno. Those two shapes now accept only their exact HTTP403
Restricted-profile violation messages, while preserving the named Kyverno
alternative. Reports retain the admission layer, policy/profile, exact violation
and denial text. Unrelated PSA violations, generic RBAC/API errors and every T9b
case remain strict errors. The extra-container case clones the valid container,
including its security context, so it reaches the intended container-count rule.
All direct requests remain server-side dry-runs; no cluster policy is weakened.
The original run15 errors remain historical evidence pending a new live rerun.

### T25 protocol fixture harness

T25 is opt-in and requires `--protocol-fixture-agent conformance-protocol`, its
explicit signed/approved Ready `--protocol-fixture-release`, and
`--session-timeout 1800` or longer. The dedicated image intentionally replaces
the installed SDK step module; ordinary tool output cannot exercise these faults
because the SDK normalizes it. A healthy control precedes five fault sessions,
and another healthy session follows each fault. Each fault first commits boundary0,
then repeats the actual wire fault at boundary1 through the three-generation limit.

The verdict decodes real Temporal inputs/results with the tenant codec. It checks
the one durable checkpoint, unchanged restored snapshot, no accepted faulty
boundary, confirmed deletion before generation fencing and creation, final cleanup,
and pod absence. The hang uses the unchanged 290-second exec deadline, observed
by trusted monotonic worker telemetry. No fixture injection or shortened timeout
is added to the production runner.

Every exec boundary records Linux process RSS/high-water and the actual cgroup
memory limit at start/end and at most once per five seconds during heartbeats.
The kernel high-water mark captures peaks between samples. T25 requires complete
observations, one worker process and pod UID/image, unchanged restart count,
at most 64 MiB RSS and high-water growth from the initial baseline, and at least
64 MiB high-water headroom below the cgroup limit. These are process-wide bounds;
run the batch exclusively. Another session's samples, missing telemetry, unreadable
worker logs, a process change, or an unbounded cgroup cannot establish a pass.
The harness needs worker pod/log read access and tenant session pod list/release
get access; it needs no additional exec or fixture mutation privileges.

Local subprocess and history tests establish implementation coverage only. The
fixture must still pass the signed release pipeline and live admission, and a new
retained report must establish the actual T25 gate. Historical failures remain.

### Active-session disable, cancellation and policy drift

Opt-in T15/T20 orchestration now runs in the harness Job against conformance-b.
The bounded probe keeps sending requests during Agent disable/drift and real
Temporal cancellation. Gateway statuses and APS source-replica decisions supply
the verdict; no model summary substitutes for a 403. Tests require both warmed
APS replicas, measured conservative timing bounds, controller-produced drift,
failed new-session preflight and CRD image immutability.

T15 mutates `Agent.spec.enabled: false` on the exact `conformance-b` Agent,
then restores its original spec. `deny.release_unapproved` is the specified APS
reason for a disabled Agent as well as an unapproved release; the reason alone
does not identify the edit. Reports now retain the exact Agent target/UID,
resource versions, spec patch and observed enabled value for disable and restore.
Both writes require current-object readback before their evidence is accepted.
The five-second edit bound, both-replica denial requirement and first-actual-stop
cancellation clock are unchanged. This adds provenance to the existing Agent
mutation path; it does not reclassify earlier run results.

Cancellation deliberately injects a DELETE denial for one exact Pod name and UID
through an operator-created namespaced Kyverno Policy. The harness restores that
rule and the exact Agent spec under shield, refuses concurrent edits, and requires
trusted cleanup to confirm Pod absence. The operator owns independent cleanup
after Job failure. See [platform](platform.md#active-session-revocation-qualification-t15t20)
for permissions, timing semantics and setup. No live T15/T20 pass is claimed by
this implementation.

## Exact budget qualification (T13/T13b, implementation pending live evidence)

The opt-in budget harness uses the signed `conformance-budget-day` and
`conformance-budget-session` fixtures with the dedicated
[`platform/budget-backend`](../platform/budget-backend/README.md). This qualifies the
production gateway/APS/Redis budget path with an enforced300-token bound and actual
100-byte-token responses. It is not evidence for Qwen accounting. Production backend
catalogue bounds remain unchanged. No live pass is claimed by unit tests or image builds.

T13 requires exactly2000 tokens/day, a clean trusted ledger baseline, eighteen
successful100-token responses followed by APS `deny.budget_exhausted`429. At1800 spent,
a300 reserve cannot fit. During a fixed120-second guest pause an authorized operator
must delete the exact two old gateway pods. The harness's read-only watcher prints
`T13 awaiting gateway replacement` and their names/UIDs to stderr, waits at most100
seconds for both old UIDs to disappear and two new Ready pods with unchanged container
configuration/image IDs, then verifies that the next request still receives APS429.
The watcher performs no deletion, Deployment patch, status write or ledger reset.

T13b requires1000 tokens/session, two concurrent sessions under the same agent and
independent trusted session ledgers. After100 spent, ten concurrent attempts (successful
backend responses held5seconds) must produce exactly three allows and seven APS budget
refusals, with all ten distinct decisions before the first response and a trusted
100-spent/900-inflight observation. Actual100-token settlements must appear per decision
within5seconds. Four further responses bring session1 to800 spent; it is refused,
actually writes local state.usage0, is refused again, commits that zeroed checkpoint,
and deliberately emits one malformed wire response. Normal runner deletion, generation
fencing, creation and committed-snapshot restore into generation2 must occur in order;
the restored local usage remains0 and the retry is still refused. Session2 succeeds
before and after this sequence and remains active across it. Exact HTTP statuses,
SHA256-verified committed state, trusted Temporal events, APS/backend logs and ledger
observations must agree. Missing/unreadable/duplicate evidence, wrong bounds, local rate
limiting, nonzero baselines, late settlement, late restart, overspend or missing recovery
cannot pass. No refused request is retried until success.

The APS observation endpoint is disabled by default. Enabling requires both
`APS_BUDGET_OBSERVER_SUBJECT=system:serviceaccount:agent-platform:conformance-harness`
and `APS_BUDGET_OBSERVER_AGENTS=conformance-budget-day,conformance-budget-session`.
GET `/v1/conformance/budgets/{session}?agent={fixture}` authenticates that exact subject
using Kubernetes TokenReview with audience `agent-policy-service`, checks trusted
first-writer session ownership in `tenant-test`, and exposes numeric session/day totals
and generation. No arbitrary Redis key, secret, mutation or reset operation is exposed.
Reads use the existing ledger expiry semantics and can charge expired reservations in
full; “read-only API” does not mean Redis remains byte-for-byte unchanged.

Run with `--tests T13 --budget-day-release <signed-ready-release>` or
`--tests T13b --budget-session-release <signed-ready-release>`. These tests are excluded
from default batches. The harness uses `APS_URL`, the projected `APS_TOKEN_PATH` with
APS audience, and its read-only `ARTIFACT_DIR`. Backend logs default to namespace
`agent-platform`, label `app.kubernetes.io/name=budget-backend`, container
`budget-backend`; namespace/label can be set via `BUDGET_BACKEND_NAMESPACE` and
`BUDGET_BACKEND_LABEL`. Existing Kubernetes RBAC must permit reads of those pod logs,
the agents Gateway pods, fixture Agents/AgentReleases and the relevant sessions. No
Redis credentials go into the harness or sandbox. A used daily fixture cannot be
silently rerun from zero: wait for a new UTC day or separately document any explicit
operator reset outside qualification. The harness has no reset capability.

The APS Redis late-settlement regression now charges an expired reservation in full
at its130-second deadline even when settlement arrives before any Reserve/Ledger sweep.
Previously Redis refunded that hold to actual usage; the Memory store already behaved
correctly. Both stores are tested at the deadline and after it.

Production multi-completion risk remains a separate gate: agentgateway v1.7.0's
[request type](https://github.com/agentgateway/agentgateway/blob/26f5aebe99a36a037fe54f8080a608aca5547e15/crates/llm/src/types/completions.rs)
preserves unknown fields with serde(flatten), including `n`, and its
[OpenAI forwarding path](https://github.com/agentgateway/agentgateway/blob/26f5aebe99a36a037fe54f8080a608aca5547e15/crates/agentgateway/src/llm/mod.rs)
serializes that request. Existing GitOps model pins do not restrict `n`; vLLM supports
multiple completions. A single-sequence maxModelLen reservation is therefore not yet
proven to bound aggregate production usage. The deterministic backend's n=1 rejection
must never be presented as a fix or qualification for the production routes.

### Closed-session cleanup before T24 qualification

The cleanup reconciler now inspects every APS-held session on its five-minute
schedule and checks the actual Temporal execution before acting. The former
fifteen-minute age filter could leave a newly terminated workflow's grant and
generation beyond T24's ten-minute bound. Running workflows remain untouched.
Pod deletion and session revocation start independently, as do stop and grant
deletion; failure outcomes are recorded and retried. A Temporal patch marker
preserves the original activity ordering when replaying older cleanup histories.

APS stop atomically removes the active generation and retains its stopped
tombstone. Generation updates check that tombstone atomically, so a delayed
activity cannot recreate active state after stop. The replacement path still
requires confirmed deletion before provisioning a new generation.

A pending-stop receipt keeps partially cleaned sessions discoverable until
vault deletion, exchanged-token deletion, reservation release and the final
receipt write all succeed. Late vault refreshes/deposits and exchange-cache
writes also reject the stopped tombstone atomically, preventing in-flight
requests from recreating credential state after cleanup.

Local Redis/memory races, HTTP lifecycle tests and Temporal history replays cover
these fixes. They do not constitute live T24 qualification: the retained live
report must measure each fault's cleanup within ten minutes and independently
confirm Pod absence, no grant and no active generation.

## T17: operator-controlled APS outage

T17 is opt-in and exclusive. A newly signed `conformance-b` release containing
`probe_fail_closed` schedules exactly five independent MCP initialize requests per
second through the existing TLS gateway. It pins one agent token with at least
probe duration plus 15 seconds remaining. Duration is 60–240 seconds (default 180).
Slow responses do not postpone later starts; requests are never retried.
Transport errors, missing/duplicate gateway traces, scheduling lateness above
50 ms or inter-request gaps above 300 ms fail. Complete token-free evidence is read
from `workspace/t17/requests.json` in the SHA256-verified accepted snapshot; only a
compact summary enters the next model boundary.

The harness never changes replicas or Flux. The operator prepares ConfigMap
`agent-platform/conformance-aps-outage-control`, with `data.receipt.json`:

```json
{"schema":"t17-v2","marker":"0123456789abcdef","deployment_uid":"ACTUAL-APS-UID","original_replicas":2,"original_generation":6,"original_annotation_sha":"SHA256-OF-JSON-ANNOTATION-OR-NULL"}
```

The ConfigMap UID is pinned. With Deployment UID/resourceVersion preconditions,
the operator appends `pause`, `down`, `up`, then `resume`, once each. Each phase
contains Unix-nanosecond `started_ns`/`acknowledged_ns`, actual
`resource_version_before`/`resource_version`, `generation_before`/`generation`,
`replicas_before`/`replicas`, `annotation_sha_before`/`annotation_sha`, and the
boolean `changed`. The annotation digest is SHA256 of its compact JSON scalar
(or `null` when absent); no annotation value enters the report.

Deployment metadata annotation changes increment generation as well as replica
changes. The regression preserves run27's observed sequence 6→7→8→9→10. Each
phase must link the preceding generation, replica count and annotation digest;
a change advances generation by exactly one. If the original annotation was
already `disabled`, pause and resume are explicit no-ops with unchanged generation
and resourceVersion. Actual resourceVersions are required per phase, but controller
status writes may legitimately advance them between phases and observations.
Previously written receipt fields cannot change. Reads overlapping a recorded
mutation must match one of the recorded adjacent states; no unknown state is
ignored. Final Ready evidence must match the resume generation and original
annotation digest. Older `t17-v1` receipts remain historical evidence and can be
restored by the operator, but cannot qualify a new run.

The harness independently verifies Deployment UID/generations, unchanged spec
excluding replicas, Pod UIDs/image IDs and ready/not-ready Endpoints. Operator,
observer and sandbox clocks must be synchronized; the probe also compares wall
clock to its monotonic schedule.

With ordinary Temporal and artifact-store mounts:

```sh
python -m runner.conformance --tenant tenant-test --agent conformance \
  --release MAIN-APPROVED-RELEASE --tests T17 \
  --fail-closed-fixture-release NEW-SIGNED-B-RELEASE \
  --aps-outage-marker 0123456789abcdef --aps-outage-duration 180 \
  --observe-aps-outage --session-timeout 900 --report /tmp/report.json
```

Wait for stderr JSON `phase: baseline_ready` (the first 26 successful traces), then
scale two replicas to zero and append `down`. Record request timing before waiting
for Pods to disappear: every request starting one second after scale-down request
start, including shutdown, must return real 403 until scale-up starts. Wait for
`zero_confirmed_restore_now` (12 seconds observed zero Pods/empty Endpoints), restore
two replicas and append `up`. Final evaluation requires at least 10 seconds of
continuous zero observations and 50 corresponding requests, then two new Ready Pod
UIDs with matching Endpoints. All requests beginning one second after observed
readiness must be 200 for at least five seconds. Every response needs exactly one
matching gateway `trace.id` record. The session must complete in generation one
with confirmed cleanup. The operator restores exact original replicas and Flux
annotation in a finally/trap even if the harness fails; an incomplete report does
not authorize leaving the outage active.

Additional harness rights in `agent-platform` are read-only and named:

```yaml
- apiGroups: [apps]
  resources: [deployments]
  resourceNames: [agent-policy-service]
  verbs: [get]
- apiGroups: [""]
  resources: [endpoints]
  resourceNames: [agent-policy-service]
  verbs: [get]
- apiGroups: [""]
  resources: [configmaps]
  resourceNames: [conformance-aps-outage-control]
  verbs: [get]
```

Existing Pod list/log rights cover APS inventory and gateway evidence. The tenant
role needs get on the exact new AgentRelease and existing sandbox Pod list.
No scale, patch, status, exec, Secret or Flux privileges enter the harness.
Local tests cover scheduling, evidence completeness and cancellation; they do not
establish a live T17 pass.

### Bounded log collection across replicas

The harness preserves one log-window cutoff while reading replicas sequentially.
It recomputes Kubernetes' relative `since_seconds` for every request and retry, so
slow discovery or an earlier replica cannot discard another replica's early events.
API discovery/log calls have explicit timeouts. T4 retries only until it finds the
exact namespace, final sandbox Pod and snapshot token JTI, with every APS log source
readable; an unrelated issuance cannot suppress the retry. Missing evidence after
three reads still fails. Run18's historical missing issuance remains a failure;
these regressions demonstrate collection defects without proving which delay caused
that particular missing event.

### Checkpoint source lifetime during upload

The snapshot activity now keeps the source Pod UID in its heartbeat and checks
that exact Pod is still Running and not terminating after the artifact-store
write. Losing or replacing it while the spooled archive is being copied yields
`SnapshotFailed`, so the existing workflow restores the last accepted checkpoint
on a new generation. A durable candidate object alone is not accepted progress.
A retry keeps the original source UID; it cannot adopt a replacement with the
same name. Storage write errors remain retryable snapshot failures. Pod loss
after a successful activity return does not invalidate the durable artifact.

Regressions kill, replace, terminate or fail the Pod during a real filesystem
copy and exercise retry identity and restoration after successful return. The
SessionWorkflow and confirmed deletion before replacement are unchanged. This
corrects the upload failure path needed by T23(d); live qualification of all five
failure points remains pending the observed-barrier harness and signed fixture.

### T24 grant-record discovery after successful stop

Cleanup discovery now retains a session while either its vault entry or its
actual grant record exists. A successful `stop_session` deletes the vault before
the independent grant-delete activity may finish; vault absence alone therefore
cannot prove grant cleanup. Previously both Store implementations could drop that
session from reconciliation after grant deletion failed. The same public session
fields and caller authorization remain unchanged, and the existing reconciler
retries the retained handle through its ordinary delete operation.

Memory and Redis regressions reproduce stop succeeding with the grant record
still present, preserve that record across repeated discovery, and require the
row to disappear only after its deletion. A vault with an absent grant record
remains discoverable too. All APS tests and `go vet` pass. This repairs discovery;
actual T24 fault runs must still prove initial genuine grants and final Pod,
active-generation, vault and grant-record absence within ten minutes. The ordinary
GET-grant route's 404 after vault deletion is not sufficient absence evidence.

### Observed checkpoint failure points (T23)

T23 uses a separate signed `conformance-checkpoint` fixture and an ephemeral
worker running the same pinned runner image on a unique queue. Its workflow is
ordinary `SessionWorkflow`. Test-only wrappers pause real exec/activity/storage
paths; they never manufacture an activity outcome, history or checkpoint. The
fixture performs one ordinary SDK MCP read at boundary1, with fixed stage
metadata on stderr, and keeps remote response contents out of evidence.

The operator creates `agent-platform/conformance-checkpoint-control` with
`data.config` containing JSON of exactly this shape (every ID is a fresh UUIDv4):

```json
{"version":1,"run_id":"RUN-UUID","tenant":"tenant-test","agent":"conformance-checkpoint","release":"SIGNED-READY-RELEASE","queue":"conformance-checkpoint-RUN-UUID","sessions":{"control-before":"UUID1","a":"UUID2","b":"UUID3","c":"UUID4","d":"UUID5","e":"UUID6","control-after":"UUID7"}}
```

The original ConfigMap UID and entire run binding remain fixed. Both harness and
worker need only get/patch on that exact ConfigMap; updates test UID,
resourceVersion and original config, change one key, and verify the receipt.
The harness retains its normal read-only Pod/log and exact-release permissions.
It has no Pod mutation or exec rights. The temporary worker uses the normal
session-runner identity; all fault commands bind the session, generation,
boundary, observed Pod UID and unique gate ID. Only the worker deletes the exact
Pod using an API UID precondition, then confirms404 before returning to the
ordinary activity. Every production replacement still confirms DELETED before
set_generation/create_sandbox.

Run the worker with `python -m runner.conformance_checkpoint_worker
--allow-checkpoint-faults`. Label it as a session-runner in tenant-test, with
`agentplatform.io/checkpoint-run: RUN-UUID`. The harness requires exactly one
Ready test worker, no restarts, and the same pinned image/imageID as the Ready
production workers before and after the exclusive batch. Use normal worker
Temporal/APS identity mounts and the artifact PVC. Run the harness in
agent-platform with its own identity, Temporal certificate/payload key and a
read-only artifact PVC:

```sh
python -m runner.conformance --tenant tenant-test --release PRIMARY-RELEASE \
  --tests T23 --checkpoint-fixture-release SIGNED-READY-RELEASE \
  --allow-checkpoint-faults --session-timeout 300 --report /tmp/report.json
```

Each case first commits boundary0; boundary1 does the target read; boundary2
completes. Case a kills before the real exec starts. Case b observes the real read
completion before `.done`. Case c observes `.done` and the completed boundary
activity before snapshotting. Case d pauses after a real first storage chunk;
the candidate object can become durable but Pod loss makes SnapshotFailed and
must not advance progress. Case e observes the accepted boundary1 checkpoint
before boundary2 exec, so boundary1 never repeats. Cases a–d restore boundary0;
e restores boundary1. The report records repeated-read counts, not assumptions
about read idempotence.

Qualification requires actual decoded Temporal event order, hash-verified
checkpoint metadata and `.done`, source-bound stage observations, and complete
unique selected-session transport IDs across APS, gateway and MCP capture.
Initialize/control transport is correlated separately and never counted as a
tool read. Missing/orphan/duplicate records fail. Healthy controls precede and
follow all five faults, and every session must finish with stop=OK,
delete=DELETED, grant=OK and no Pod remaining. A partial or failed batch is
retained; it cannot be rerun with the same bound session IDs to hide evidence.

Operator cleanup independently removes the temporary worker, ConfigMap and RBAC
after completion or failure, and confirms no bound sessions or Pods remain.
Use new root-created bindings for a new run. Local unit and real Temporal-history
adapter tests verify the implementation; they do not constitute a live T23 pass.

### T24 lifecycle cleanup: isolated worker and read-only fault observation

`--tests T24 --lifecycle-fixture-release <signed-ready-release>
--observe-lifecycle-faults` is exclusive and requires `tenant-test`. It runs the
four exact 15.2 faults (APS unavailable during cleanup, finalizer-blocked deletion,
isolated worker loss mid-cleanup, and Temporal hard termination) plus success,
cancellation, actual activity loss and retry-exhaustion controls. The production
SessionWorkflow, retry/deletion policies and five-minute reconciler Schedule are
unchanged. This batch does **not** qualify the production eight-hour business
clock; it neither accelerates that clock nor adds a deadline input API.

The dedicated signed `conformance-lifecycle` guest writes one real checkpoint,
then completes, or emits invalid boundary wire for the retry-exhaustion case.
It makes no model/MCP/network calls and never receives human access or refresh
tokens. Its manifest retains the ordinary SDK factory validation; only this
fixture image explicitly installs its deterministic boundary driver. Its model
capability has one token per day and per session, below the real backend's
unchanged reservation bound. A release and local tests alone do not establish a
live T24 pass.

APS optionally exposes `GET /v1/conformance/sessions/<session>?agent=conformance-lifecycle`.
Both `APS_LIFECYCLE_OBSERVER_SUBJECT=system:serviceaccount:agent-platform:conformance-harness`
and `APS_LIFECYCLE_OBSERVER_AGENTS=conformance-lifecycle` must be configured together.
TokenReview requires the APS audience and this exact caller; trusted stored
ownership must be `tenant-test/conformance-lifecycle`. An atomic store snapshot supplies
actual grant-record/vault presence and remaining TTL, explicit generation-key
presence/value, stopped and stop-pending booleans. Two safe flags additionally
confirm the bound encrypted vault contains a refresh token and its access token
is current. They prove metadata presence/lifetime, not an attempted refresh.
The initial baseline requires both; root must supply genuine issuer credentials.
The observer never exposes a grant handle,
credential, arbitrary store key, or write operation. Normal lazy key expiry still
applies. Owner metadata stays bounded to the same 24-hour lifetime in both stores,
including after discovery removes a fully cleaned session. Missing/expired owner
metadata is a refusal, not evidence of cleanup. Deploy the orphan-grant discovery
fix as well: a missing vault is not proof that its grant record was deleted.

For every case the harness requires an initial committed checkpoint, an actual
activity heartbeat on the exact Ready isolated worker, a genuine grant and vault
with more than 600 seconds remaining TTL, and an active generation. Cleanup must
be observed within 600 seconds under both wall-clock and independent local
monotonic bounds. The local upper bound begins before the held-state baseline
reads and fault notification, and ends after all final receipt and Kubernetes
404 reads. Remote cleanup/termination timestamps remain separate evidence and
never subtract from that elapsed time. The original grant/vault expiry must be later
than final observation, so natural expiry cannot satisfy cleanup. Final proof is
independent Kubernetes 404 for every created Pod name, empty session Pod listing,
no generation key, no actual grant record/vault, stopped=true and pending=false.
The finalizer case also proves stop and grant deletion while the Pod is still
blocked and records the real 110-second `NOT_CONFIRMED` result. The APS outage
case records both failed cleanup calls while actual APS Pods/Endpoints are empty,
confirmed Pod deletion during that outage, and later scheduled reconciliation.
Hard termination must be the Temporal TERMINATED event, never cancellation.

The operator creates exactly `agent-platform/conformance-lifecycle-control`.
Its `data.config` is JSON with these fields:

```json
{
  "version": 1,
  "run_id": "<fresh-lowercase-UUIDv4>",
  "tenant": "tenant-test",
  "agent": "conformance-lifecycle",
  "release": "<exact-signed-ready-lifecycle-release>",
  "queue": "conformance-lifecycle-<same-run-id>",
  "worker_image": "registry.almckay.io/platform/session-runner:<commit>@sha256:<digest>",
  "sessions": {
    "success": "<fresh-UUIDv4>", "aps_down": "<fresh-UUIDv4>",
    "finalizer": "<fresh-UUIDv4>", "worker_kill": "<fresh-UUIDv4>",
    "terminate": "<fresh-UUIDv4>", "cancel": "<fresh-UUIDv4>",
    "activity_crash": "<fresh-UUIDv4>", "retry_exhaustion": "<fresh-UUIDv4>"
  }
}
```

All session IDs must be distinct. The harness and isolated worker pin the
ConfigMap UID and exact raw configuration; replacement/drift fails closed.
Both receive only named `get` access to this control object, never patch rights.
The harness also needs named get of APS Deployment/Endpoints, existing Pod list/get,
exact Agent/AgentRelease reads, and its normal tenant Temporal/observer credentials.
It has no Pod delete, exec, Secret read, scale, approval or status-write operation.
The worker reuses the ordinary tenant session-runner identity and artifact mounts;
its only new behavior is waiting at exact fixture-bound activity gates.

Launch one same-image ephemeral worker Deployment (one replica) with
`python -m runner.conformance_lifecycle_worker`, the exact run-specific queue
configuration, `LIFECYCLE_RUN_ID`, and `POD_UID` from the downward API. Label it
`agentplatform.io/lifecycle-run=<run-id>`. Preserve the ordinary sandbox/artifact
mounts and network identity; place it where the artifact PVC can be mounted.
Run only this isolated queue on the ephemeral worker. Leave the ordinary worker
and reconciler running. A controller may replace the ephemeral worker after its
exact Pod UID is killed; the replacement must use the same pinned image and run
configuration. Initial and replacement Pods must have the same ReplicaSet
controller owner UID; an unrelated same-image Pod is not recovery evidence.
Do not restart or change shared workloads during another batch.

The root operator alone appends the following exact keys using JSONPatch tests
on ConfigMap UID, resourceVersion and unchanged `data.config`. Values are JSON
strings. Preserve every prior value; a receipt may only gain `restored_at`.

- On stderr `grant_required`, deposit a genuine refresh-backed grant for the exact
  actor `agent-tenant-test-conformance-lifecycle` and announced session. Append
  `grant-<case>` with `{"session_id":"<session>","handle":"gh_<opaque>"}`. The
  handle is approved workflow metadata; no user/refresh token enters this object,
  stdout or the report. Deposit just in time: the normal reconciler may clean an
  unused grant whose workflow does not exist. Keep the deposit→control update
  immediate, and require the observed baseline rather than assuming it succeeded.
- After `baseline_ready`, normal success/retry-exhaustion cases can be released.
  For fault cases establish the exact fault first. Append
  `release-<case>-boundary1` with
  `{"session_id":"<session>","stage":"boundary1","generation":1}`. No release
  key is needed for cancellation, hard termination, or the activity-crash case.
- For APS/finalizer/worker faults append `receipt-<case>` with exactly
  `session_id`, `fault` (the case name), `target_uid` and `started_at` (Unix seconds
  just before the actual mutation). Append `restored_at` after observed restoration.
  The target is the original APS Deployment UID, sandbox Pod UID, or ephemeral
  worker Pod UID respectively. Root receipts supplement independent API/history
  observations; an error or receipt alone never proves an outage or deletion.

Fault sequence and restoration obligations:

1. `aps_down`: record original APS UID/spec/replicas and Flux annotation; pause only
   that Deployment's reconciliation, scale 2→0 with UID/RV preconditions, observe
   zero Pods and Endpoints, then release boundary 1. On
   `cleanup_recorded_restore_fault_now`, restore exact replicas/annotation and
   require two Ready replicas. The harness waits for the ordinary Schedule's next
   action and matches its actual workflow/run ID and cleanup activity results;
   it never manually triggers reconciliation.
2. `finalizer`: add only `agentplatform.io/conformance-lifecycle` to the exact
   sandbox UID, retaining all unrelated finalizers. Release boundary 1. Wait for
   the actual `NOT_CONFIRMED` deletion result and independent stop/grant cleanup,
   then remove only that finalizer with UID/RV tests. Require actual 404.
3. `worker_kill`: release boundary 1. Wait for
   `cleanup_waiting_kill_exact_worker`, which requires successful real stop and
   pending delete/grant activity heartbeats on the original worker. Delete only
   that worker Pod with its UID precondition; the replacement's ordinary activity
   retries must execute real delete/grant calls. First attempts never fabricate
   success or failure. Require original worker 404 and a same-image Ready replacement.
4. `terminate`: the harness hard-terminates its exact workflow after baseline;
   scheduled cleanup must record both session closure and confirmed orphan deletion.
5. `activity_crash`: at baseline kill the exact ephemeral worker without releasing
   boundary 1. The actual heartbeat timeout must replace generation 1 only after
   confirmed deletion/fencing, restore the accepted checkpoint and complete on
   generation 2. Restore the same-image worker as above.

The operator must restore APS replicas/Flux state and remove its own finalizer
in independent finally/trap paths even if the Job fails, is cancelled or times
out. The harness settles normal workflow cleanup on exit and retains safe partial
evidence/error classes, but cannot undo root-owned faults. Confirm every owned
session is closed and cleaned before removing temporary controls/RBAC/worker.
A missing receipt, wrong UID, changed configuration, unavailable history, lingering
Pod/key/grant, stale worker, deadline breach or incomplete fault record fails the
case. Genuine human grant authorization is an external prerequisite; an empty or
fabricated initial grant never qualifies this test.

### Explicit T13 retry ledger and failure evidence

`--budget-day-agent conformance-budget-day-retry --budget-day-release <signed-ready-release>`
selects one additional fixed identity for T13. The default remains
`conformance-budget-day`; arbitrary aliases and use with other tests are refused.
The retry fixture retains exactly 2000 tokens/day and 200000/session and the same
enforced 300-byte-token backend reservation. All APS decisions, backend captures
and observer rows must match the selected identity. Its initial ledger must be
zero. The original failed run's ledger remains untouched; this adds no reset API,
limit increase, automatic daily identity, or qualification of Qwen accounting.
APS and the dedicated backend require the exact retry identity in their fixed
configured allowlists; gateway JWT policy, signed Agent/AgentRelease and read-only
harness access must match it too.

After the real first APS budget refusal, a recorded one-second response barrier
precedes stderr `T13 awaiting gateway replacement`
with `test=T13`, exact `agent` and `session_id`, original Pod identities, strict
integer `emitted_ns` from `time.time_ns()`, and unchanged `deadline_seconds=100`.
The external operator pins the active Job/Pod UIDs, requires two original Ready
gateways and a fresh event with at least 30 seconds left, and rechecks active UIDs
and the emission deadline before each UID-bound deletion. No mutation is
permitted after the Job ends or the event deadline passes. Root controls this
operator and records its launch, polling, event and mutation receipts.

The barrier is operational coordination, not proof of HTTP completion. The final
committed request's `finished_at` must still be no later than the actual watch
`started_at`; a guest response slower than the barrier can fail this strict check.
Budget timestamps retain full UTC microseconds, so same-second ordering is not
lost. Neither this pause nor timestamp precision changes the 100-second window.

The read-only watcher also keeps `started_at`, local elapsed time, last observed
inventory and a completion flag. An inventory response arriving at or after the
100-second bound fails even if the new Pods are Ready. Successful evidence must
show completed replacement within that bound and before the fixture's actual
post-restart request. On any collection/orchestration error, the report retains
the phase (including `denial_response_barrier`), its barrier receipt, bound
session/release IDs, already observed ledgers, allowlisted
decision correlation metadata and partial restart evidence. It records only the
exception class, never arbitrary log/response/exception text. Evidence is frozen
at failure without extra network collection; settled independent session cleanup
cannot rewrite an error into a pass. Missing committed fixture or complete logs
remain missing; partial observations never qualify the test.

### Observed TLS transport and ordinary T4 capture (T27)

T27 uses a signed `conformance-transport` fixture followed by the ordinary
`conformance` T4 case. The fixture first proves the fixed gateway listener is
reachable with verified TLS, then sends a credential-free plaintext HTTP request
to that same port443. Only an explicit TLS-required response or a closed/reset
connection qualifies; timeout and unrelated HTTP failures do not. A separate
loopback server uses a newly generated ephemeral CA and IP-valid certificate.
Its trusted positive control must succeed, while the ordinary SDK identity
client must fail with unknown-issuer verification code20/21 before sending HTTP.
The fixture never requests an identity token; it deletes its temporary key files.
The installed driver forwards the normal SDK manifest/factory validators, whose
release-image checks remain mandatory.

The operator creates `agent-platform/conformance-transport-control` with
`data.config` containing exactly the following JSON shape. Every ID is a new
UUIDv4; both releases must be controller-verified, approved and Ready:

```json
{"version":1,"run_id":"RUN-UUID","tenant":"tenant-test","queue":"conformance-transport-RUN-UUID","worker_image":"registry.almckay.io/platform/session-runner:COMMIT@sha256:DIGEST","transport_release":"conformance-transport-SIGNED-RELEASE","t4_release":"conformance-SIGNED-RELEASE","sessions":{"transport":"UUID1","t4":"UUID2"}}
```

Start one temporary worker using the same immutable image as production, on the
bound queue, with `python -m runner.conformance_transport_worker`. It uses the
ordinary session-runner identity, Temporal/APS mounts and artifact PVC. Supply
`TRANSPORT_RUN_ID` and downward-API `POD_UID`; label it as a tenant-test
session-runner with `agentplatform.io/transport-run: RUN-UUID`. It accepts only
these two bound sessions without seed or delegated grant. Only its actual T4
`run_boundary` wrapper waits before boundary0 and after the final real exec;
the standard SessionWorkflow, activity result and deletion order remain intact.
The before-exec gate waits at most240s, and the after-exec gate120s, within the
ordinary activity deadline. A missed window requires a new bound run.

Run the harness with its normal read-only logs/Pods, exact two release reads,
Temporal identity and read-only artifact PVC:

```sh
python -m runner.conformance --tenant tenant-test --agent conformance \
  --release T4-RELEASE --tests T27 --transport-fixture-release TRANSPORT-RELEASE \
  --observe-host-capture --session-timeout 900 --report /tmp/report.json
```

Both workloads require only `get` on the exact control ConfigMap. Neither can
publish a capture receipt, execute host commands or open a raw socket. The
operator owns bounded host preparation/capture and separate UID/resourceVersion
conditional publication of the two sanitized receipts. Concrete host actions
must be inspected before execution; the opt-in flags are scope guards, not an
additional mandatory user-approval policy. Never create a privileged capture Pod.

On `capture_start_required`, save only its `gate` object to a private0600 JSON
file. On rig0, prepare the exact binding using an explicitly selected kubeconfig:

```sh
python -m runner.conformance_transport_host prepare --kubeconfig KUBECONFIG \
  --gate GATE.json --binding BINDING.json --allow-host-metadata
```

Preparation reads the exact Pod UID/IP, its CRI sandbox/netns, guest eth0 and host
veth peer indices. Review that binding and the concrete capture command before
running it. Capture rechecks those identities before start, during collection,
and after stop; it refuses replacement, termination, another node or interface.

```sh
sudo python -m runner.conformance_transport_host capture --kubeconfig KUBECONFIG \
  --gate GATE.json --binding BINDING.json --directory FRESH-PRIVATE-DIRECTORY \
  --owner al --allow-host-metadata --allow-host-capture
```

The only filter is `host EXACT-POD-IP and host 10.43.250.10 and tcp port 443`
on the single bound host veth, with promiscuous mode disabled. `tcpdump` drops to
the selected unprivileged local owner. Its raw pcap is0600 inside a0700 directory;
the file is capped at32MiB and4096packets. An independent `/usr/bin/timeout`
supervisor sends INT at115s and kills the process group after another5s, even if
the Python helper is killed. Ordinary signal/failure cleanup also stops that
exact process group. The start receipt's `pid` identifies this supervisor.
Hitting any limit, losing packets, missing statistics or forced cleanup fails
qualification rather than producing a partial pass.

Once `capture-start.json` exists, the operator publishes its exact JSON as the
control ConfigMap's new `data.capture-start`, preserving the original config and
UID with an atomic resourceVersion precondition. This releases the first real
T4 exec. The existing T4 evaluator must independently pass exact claims, current
JTI and its sole APS issuance. On `capture_stop_required`, create the local
`FRESH-PRIVATE-DIRECTORY/stop` marker. The worker holds that same Pod Running until
the helper stops and writes `capture-complete.json`. Publish that exact sanitized
JSON as new `data.capture-complete` using the same conditional operation. Never
replace an existing receipt; observed keys are immutable. Failed completion
still releases normal cleanup and produces a failed/error report.

The report requires both real gate times and the entire captured T4 execution,
including its exact issuance, to fall within the recorded interval. The bounded
classic-pcap parser requires complete IPv4 TCP stream coverage, TLS handshake and
encrypted application records in both directions, no unexpected endpoint,
fragment, missing segment or conflicting overlap. It searches reassembled bytes
for cleartext Authorization/Bearer/JWT patterns, including packet splits. Missing
or ambiguous evidence fails. Only sanitized counts, times and SHA256 enter the
report/control object; raw bytes and bearer values are never printed or uploaded.
Keep the raw0600 file local only until operator verification, then remove it along
with the private binding/receipts after retaining the sanitized report and digest.

Finally remove the temporary worker/control/RBAC and independently confirm both
bound workflows are closed and Pods absent. Qualification requires stable worker
UIDs/images/restarts, unchanged fixture UID/spec, and confirmed ordinary cleanup
for both sessions. Unit tests, dummy-process lifetime tests and installed-image
validation establish these local paths; they do not constitute live T27 evidence.

### T27 missing-capture and fatal-alert regression (2026-11-06)

Run31 remains ERROR: host capture did not start within the unchanged 240-second
window, and the missing completion receipt triggered an evaluator AttributeError.
An explicit null or malformed completion receipt now produces failed capture
checks while retaining the original evidence. No historical result is upgraded.

The companion transport fixture recognizes only a complete single TLS alert
record with legacy version 0x0303, length two, fatal level two and description
unexpected_message(10) or decode_error(50). Unknown responses remain failures.
The credential-free host diagnostic `15030300020232` is a fatal decode_error,
not HTTP; it cannot establish what the sandbox received. Recognition follows
[RFC 5246 sections 6.2.1 and 7.2](https://www.rfc-editor.org/rfc/rfc5246).
The deployed gateway's recorded v1.7.0 digest is
`sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`;
[its pinned source](https://github.com/agentgateway/agentgateway/tree/26f5aebe99a36a037fe54f8080a608aca5547e15)
locks rustls 0.23.46. Its
[alert writer](https://github.com/rustls/rustls/blob/v/0.23.46/rustls/src/common_state.rs)
uses fatal alerts; its [message builder](https://github.com/rustls/rustls/blob/v/0.23.46/rustls/src/msgs/message/mod.rs)
uses TLSv1_2 for the record and its [connection parser](https://github.com/rustls/rustls/blob/v/0.23.46/rustls/src/conn.rs)
emits fatal decode_error on invalid input.

These are local regressions only. Publish a fresh signed transport fixture and
updated runner from merged main, verify installed/source equality, then coordinate
a new bounded operator capture during exact T4. No image was built, released or
qualified by these tests; run31 and its absence of packet evidence remain intact.
