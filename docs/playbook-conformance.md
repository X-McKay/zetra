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
{"schema":"t17-v1","marker":"0123456789abcdef","deployment_uid":"ACTUAL-APS-UID","original_replicas":2}
```

The ConfigMap UID is pinned. The operator uses Deployment UID/resourceVersion
preconditions to change replicas and appends `down`, then `up`, once each:

```json
{"started_ns":1793980000000000000,"acknowledged_ns":1793980000100000000,"resource_version":"ACTUAL-PATCH-RESPONSE-RV","generation":2}
```

Times are Unix nanoseconds immediately before the scale request and after its
response. Generation/resourceVersion come from that response. Previously written
receipt fields cannot change. The response resourceVersion is retained as
operator evidence; controller status updates can advance it before observation.
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
