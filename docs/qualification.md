# Qualification

This report records fresh, unsigned local evidence for the ordinary-worker SDK on
2026-10-18. It qualifies the admitted serial profile in this environment, not a
production service deployment.

## Identity and environment

[Runtime identities](../benchmarks/results/runtime.json) bind the SDK source,
unchanged lockfile, seven rebuilt images and five agent releases. All seven
installed image packages matched source SHA-256
`275a46c210aef656d410e528a3205acf787b22d45816d7c4c085b87680cccff8`.
Docker Hub repository names were normalized before comparing exact image digests.

The run used `kind-zetra-validation` on Linux arm64 in a local Docker VM with
8 GiB memory. Temporal and OpenShell ran in kind; ordinary workers and shared
FileStore backing ran on the host. Other local workloads shared the VM. Images
were preloaded; registry pull time is excluded. The SDK accepts caller-configured
connections and storage; the kind installation is a qualification harness.

Live inference used `https://llm.almckay.io/v1`, model
`openai/Qwen3.6-35B-A3B-NVFP4`, temperature 0, 512 output tokens, no thinking,
serial tool calls and disabled provider retries. Inference is hosted outside kind.
This is a functional/recovery check, not a model-quality evaluation.

## Verification

| Gate | Result |
| --- | --- |
| Locked local `just check` | 172 passed; 33 cluster tests deselected; lint, format, lock, docs, both example tests and fixture evaluations passed |
| Full suite through Temporal/OpenShell in kind with live vLLM | 205 passed, zero failures/errors/skips, 838.70 s |
| Bridge and SDK latency benchmarks | 2 passed, zero failures/errors/skips, 277.13 s |
| Fresh provisioning benchmark | 1 passed, zero failures/errors/skips, 131.78 s |
| Installed wheel outside checkout | Scaffold, generated-agent test, fixture evaluation and embedded build assets passed |

[Test evidence](../benchmarks/results/tests.json) lists the full run. The new SDK
coverage exercises independent client/worker lifetimes, repeated submission,
concurrent session workspace separation, exact-run inspection and raw Temporal
reset/foreign-runtime rejection before sandbox access. At cursor 2, a worker is
stopped during a safe advance; a newly constructed Worker/router/FileStore resumes
and completes with exactly three model and two tool execution Activities. This is
fresh worker-object/cache recovery on one host, not a multi-host availability test.

Existing cases additionally exercise subprocess loss/SIGKILL, sandbox replacement,
lost effect completion, heartbeat expiry, cancellation, hard termination, history
replay, policy denials and malformed receipts/workspaces. Cleanup and effect
certainty remain separate. Unit tests cover wrong-key control-plane rejection,
concurrent cache initialization, canceled initialization, cleanup queue/target
validation and asynchronous store/receipt-validation heartbeat coverage.

The full 64-boundary runtime test executes 32 model/tool pairs, reconstructs using
a fresh Runtime halfway through, and rejects boundary 65 both warm and cold without
repeating recorded effects. It is local runtime conformance, not a 64-boundary live
model performance result. Replay microbenchmarks remain a separate measurement.

## Latency

Times are seconds. Full session wall time includes a fresh sandbox, execution and
confirmed deletion. Component medians need not sum to median wall time. Small
samples and shared-machine load limit interpretation.

[Bridge samples](../benchmarks/results/latency.json)

| Boundary | Fixture median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| Full session wall | 17.194 | 25.929 |
| Provision Activity | 10.145 | 11.727 |
| Active history interval | 3.994 | 10.816 |
| Cleanup Activity | 3.003 | 3.159 |

Native comparison

| Boundary | Fixture median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| Native ADK body | 1.014 | 8.835 |
| Native process | 6.122 | 9.324 |
| Native OpenShell exec | 6.497 | 10.428 |

[Public SDK samples](../benchmarks/results/sdk-latency.json)

| Boundary | Fixture median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| Full session wall | 15.929 | 19.051 |
| Provision Activity | 10.029 | 8.867 |
| Active history interval | 3.590 | 6.641 |
| Cleanup Activity | 2.829 | 3.405 |

All four SDK samples completed with confirmed deletion. Each used 13 Activities. Reconstruction body medians for prefixes 0, 1, 8, 32, 63 and 64 (n=3 each) were 0.384, 0.295, 0.598, 0.461, 0.642, 0.606 s.

The bridge active/native differences are observed path differences, not isolated
Temporal overhead. Native OpenShell execution includes a fresh process; durable
active time excludes provisioning. SDK wall time includes submit and wait, but
excludes shared Worker startup, release loading and subsequent benchmark history
retrieval. SDK and bridge images/packages and inference timings differ, so their
difference does not isolate SDK wrapper overhead.

[Interleaved provisioning samples](../benchmarks/results/provisioning.json)

| Median, seconds | Uncompiled | Precompiled |
| --- | ---: | ---: |
| Fixture (n=3 per image) provision | 10.618 | 7.935 |
| Fixture (n=3 per image) import probe | 1.474 | 0.500 |
| vLLM policy (n=2 per image) provision | 9.831 | 9.191 |
| vLLM policy (n=2 per image) import probe | 1.776 | 0.792 |

Each provisioning trial uses a distinct fresh sandbox, empty writable workspace
and confirmed deletion. Both images are preloaded. The vLLM-policy provisioning
trials do not call the model. Import probes run separately after provisioning;
they are not an additional component to add to provision time. There is no reused
agent state or workspace pool.

## Reviews, failures and limits

Sol reviews found and prompted fixes for failed normal-cleanup reconciliation,
retained canceled initializer tasks, and cleanup-result task-queue binding. Cache
initialization now uses a cancellable lock; accepted progress remains in Temporal.
Review findings have focused regression tests. A final scope audit removed an
unused transport cleanup helper that looked up the latest Workflow by ID. Cleanup
reconciliation now has one production path, through the exact-run cleanup Workflow;
the images, full suite and benchmarks were refreshed after that removal.

The first local full run had four failures and 154 passes: three receipt tests
mocked a transport seam no longer used after validation moved under the heartbeat,
and one expected-label assertion omitted the authority fingerprint. The receipt
tests now call the real validator with forged chains and missing/corrupt snapshots;
the expectations retain the same integrity requirements. The corrected local gate
and real suite were rerun. Those initial failures are not counted as final passes.

Final cluster inspection found zero Sandbox resources, session pods, session PVCs
and session PVs. The 7 shared OpenShell/Temporal pods were Ready; shared
service volumes remain. The dedicated test cluster was retained.

The supported profile remains static, serial and nonstreaming, with 64 boundaries
and a 30-minute session limit. Agent code and tools share a sandbox trust domain
and cooperate with the protocol; OpenShell enforces the outer policy. No external
business writes, MCP actions, parallel agents or automatic ambiguous-effect retry
are admitted. AgentGateway is a future integration boundary, not an implemented
transport or additional durability engine.

Callers own Temporal/OpenShell identity, TLS/authentication/codecs, durable shared
artifact backing, secret availability and retention. Workers on a queue must use
the same runtime/gateway/store/key configuration. Key rotation is not versioned;
reset/retry/continue-as-new and cron lineages are rejected. Worker rollouts require
replay-compatible code or qualified native Temporal deployment versioning. Temporal's Workflow-ID
deduplication lasts only within retained history.

Normal cleanup and explicit cleanup Workflows use Temporal retries. There is no
continuous janitor. A retired or unacknowledged provisioning attempt can create a
sandbox late; its history remains `NOT_CONFIRMED` even after observed instances
are removed. Independent reconciliation/fencing is a remaining platform requirement,
not a custom SDK scheduler. Resource removal does not prove physical erasure.
No p95, throughput, cold-pull, hostile-code or production availability claim follows
from these measurements.
