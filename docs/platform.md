# Agent Runtime Platform

`platform/` implements the Agent Runtime Platform specification (revision 2):
Kata sandboxes reachable only through agentgateway, OAuth agent identity from a
projected ServiceAccount token, the Agent Policy Service as fail-closed external
authorization, and a Session Runner that drives one boundary per kubelet exec.
New `zetra-core-v1` releases use the shared SDK execution core in `src/zetra`
through the platform transport. Legacy platform and OpenShell paths stay available
for their retained histories and images; production migration requires Phase B
cluster qualification.

| Directory | Spec section | Contents |
| --- | --- | --- |
| `platform/contracts` | 3, 5.2, 5.3, 6.3 | `agent_platform` constants and naming, policy bundle and digest, pod template, tenant NetworkPolicy set and digest, `agentctl` |
| `../deploy/platform/crds` | 6.1, 6.2 | `Agent` and `AgentRelease` CRDs (vendored by Kubani) |
| `platform/controller` | 6.4, 7.3, 7.4 | Registry Controller (kopf) with a Keycloak admin adapter |
| `platform/netpol-attestor` | 5.5 | node DaemonSet recording host-side kube-router chains on sandbox pods |
| `platform/egress-guard` | 5.5 | sandbox init container; `--attack` mode for T2b |
| `platform/agent-runtime` | 11 | `agent_runtime` sandbox SDK baked into agent images |
| `platform/aps` | 9 | Agent Policy Service (Go): ext_authz, token and grants API, Redis ledger and vault, settlement |
| `platform/runner` | 10 | Session Runner, cleanup reconciler, AES-GCM payload codec |

Each directory is its own project with its own lock. `just platform-check` runs
every project's lint and tests; tests that need a cluster are marked `cluster`
and are named after the conformance test they cover. Nothing here has run on a
cluster yet; the items in spec 17.3 must be confirmed there before Phase A is
accepted. Deployment manifests live in Kubani.

## Shared authoring and explicit migration

Author one installed Python factory returning the admitted ADK `LlmAgent` with
`zetra.AgentContext` and `zetra.Tool`. Use the same packaged SDK `AgentSpec` in the
local project index and the schema-2 platform envelope's `definition` resource.
The envelope declares the exact tool/effect inventory and deployed model binding.
The platform supplies authenticated model/MCP transports; local modes use explicit
fixtures or a reviewed model configuration. There is no implicit live fallback.

A new image/release selects immutable `spec.runtimeProfile: zetra-core-v1`.
Its profile enters the compiled policy and therefore the attested policy digest.
The controller renders release identity; the runner adds the original Temporal
execution/task identity and checks it before adoption and every command. Upgrade
the CRD, contracts/controller and runner together before activating any core
release. The corresponding Kubani change only adds the optional immutable field;
this work does not change cluster stages or approve an agent.

Prepare captures the next operation without effects. Temporal records it before
one execute attempt. The guest returns a candidate receipt; the host validates
its full tape/workspace and persists and rechecks the bounded archive before
accepting the new Position. Only a later prepare reports completion. Execute
loss or exhausted post-effect checkpoint retries terminates `OUTCOME_UNKNOWN`;
a local candidate never authorizes another effect. Recovery from preparation loss
requires confirmed deletion and the last accepted checkpoint. Core restoration
holds the execution lease and uses a durable per-pod admission/completion fence;
a lost restore response can never authorize a late overwrite of newer tool work.

Core workflows reject resets/retries/continuations and noncanonical execution IDs
before activation. A Temporal patch preserves legacy refusal-history cleanup while
new histories skip cleanup before activation. Keep old immutable worker/image
versions available while their retained histories need them.

Core executions return `CoreSessionResult` with `runtime_profile`, `position`,
`reason`, and `final_text`. Typed Python clients select that result type when
opening a handle for a core release; legacy `SessionResult` retains its original
shape. Status queries expose the richer core fields. Unknown tool outcomes may
carry a refusal reason without asserting that earlier tool work did not happen.

Archive collection and restore are limited to one buffered archive per worker,
with heartbeating waiters, for the current memory baseline. Load/capacity and
cluster enforcement remain unqualified. Native recovery/replay tests and installed
agent image parity are recorded in [the evidence record](evidence/runtime-unification-2026-10-31.md).
