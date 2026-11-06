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
