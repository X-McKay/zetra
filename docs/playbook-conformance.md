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
| Lifecycle | Normal Workflow cleanup and explicit durable terminal reconciliation; read-only status | Late provisioning uncertainty remains `NOT_CONFIRMED`; stronger closure needs gateway fencing or platform reconciliation |
| Worker rollouts | Native Temporal Worker options | Replay-compatible changes and native deployment versioning require rollout qualification |
| Key lifecycle | Per-execution capability derivation from persistent worker secret | Rotation is not versioned; retain original key and runtime configuration |
| Evaluation | Fixture and sandbox modes; release-bound evidence | Evidence is unsigned and observations remain scoped |
| Developer workflow | Scaffold, CLI, image recipe, public client and worker factory | Local kind harness is separate from production deployment |
| External actions and MCP | Not admitted | Future AgentGateway integration requires scoped identity, authorization, operation idempotency and reconciliation |
| Multi-agent execution | Not admitted | Requires a separately admitted execution design |

A future AgentGateway should attach through the existing tool dispatcher rather
than adding an execution engine. It must enforce receiver-scoped capabilities and
cannot resolve ambiguous remote commits alone or replace Temporal's accepted
progress authority. Production deployment, signed provenance and continuous
lifecycle operations require independent design and qualification.
