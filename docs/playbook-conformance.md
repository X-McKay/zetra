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

A future AgentGateway should attach through the existing tool dispatcher rather
than adding an execution engine. It must enforce receiver-scoped capabilities and
cannot resolve ambiguous remote commits alone or replace Temporal's accepted
progress authority. Production deployment, signed provenance and continuous
lifecycle operations require independent design and qualification.

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
