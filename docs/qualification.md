# Qualification

This report records fresh, unsigned local evidence for the ordinary-worker SDK on
2026-10-18. It qualifies the admitted serial profile in this environment, not a
production service deployment.

## Identity and environment

[Runtime identities](../benchmarks/results/runtime.json) bind the SDK source,
unchanged lockfile, seven rebuilt images and five agent releases. All seven
installed image packages matched source SHA-256
`0c50877de9810ad3eeb953fdc982640e62b61875b687d9496b5304f930e81fbe`.
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
| Locked local `just check` | 186 passed, 37 deselected; lint, format, lock, docs, both example tests and fixture evaluations passed |
| Full suite through Temporal/OpenShell in kind with live vLLM | 223 passed, zero failures/errors/skips, 676.53 s |
| Bridge and SDK latency benchmarks | 2 passed, zero failures/errors/skips, 175.60 s |
| Fresh provisioning benchmark | 1 passed, zero failures/errors/skips, 128.39 s |
| Installed wheel outside checkout | Scaffold, generated-agent test, fixture evaluation and embedded build assets passed |

[Test evidence](../benchmarks/results/tests.json) lists the full run. The new SDK
coverage exercises independent client/worker lifetimes, repeated submission,
concurrent session workspace separation, exact-run inspection and raw Temporal
reset/foreign-runtime rejection before sandbox access. At cursor 2, a worker is
stopped during a safe advance; a newly constructed Worker/router/FileStore resumes
and completes with exactly three model and two tool execution Activities. This is
fresh worker-object/cache recovery on one host, not a multi-host availability test.

Native create coverage adds two real-gateway fault cases. One drops a successful
create acknowledgement and replaces the worker: native replay returns the same
immutable sandbox for the identical request ID/payload, then execution completes
with exactly three model and two tool Activities. The retired attempt still yields
`REMEDIATION_REQUIRED` / `NOT_CONFIRMED`. The other holds client dispatch until after
hard termination and durable cleanup reports `NOT_CONFIRMED`, then releases the
request and observes an actual late sandbox. After exact-instance deletion, native
replay refuses with `REQUEST_REPLAY_UNAVAILABLE` and creates no replacement. These
are bounded client-transport faults, not injected gateway transaction/process
failures. Transport tests also reject a retry whose original Activity window has
expired even when its current attempt and reconstructed adapter are fresh.

Cleanup recovery adds two real-cluster cases with client-boundary gateway faults.
Normal close survives four consecutive `UNAVAILABLE` responses and confirms deletion
on Activity attempt five. A `PERMISSION_DENIED` close stops on its first attempt;
a fresh worker then reconciles that exact terminal run through four transient
failures and succeeds on attempt five. Temporal history verifies the ten-minute
cleanup window, original three model/two tool Activities with one attempt each,
and an unchanged original execution during reconciliation. The successful agent
result survives cleanup recovery. These are transport faults, not gateway outages
or a ten-minute wall-clock exhaustion test. Unit tests distinguish five explicit
permanent gateway statuses from transient errors and OpenShell deletion wait expiry.

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
samples and shared-machine load limit interpretation. The cleanup retry change adds
no remote call to a successful session; these fresh measurements do not isolate
a small latency difference from normal host/model variation.

[Bridge samples](../benchmarks/results/latency.json)

| Boundary | Fixture median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| Full session wall | 15.121 | 22.502 |
| Provision Activity | 9.806 | 9.502 |
| Active history interval | 2.384 | 10.457 |
| Cleanup Activity | 2.749 | 2.324 |

Native comparison

| Boundary | Fixture median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| Native ADK body | 0.310 | 3.509 |
| Native process | 0.851 | 3.835 |
| Native OpenShell exec | 0.999 | 4.590 |

[Public SDK samples](../benchmarks/results/sdk-latency.json)

| Boundary | Fixture median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| Full session wall | 14.505 | 18.206 |
| Provision Activity | 9.187 | 9.328 |
| Active history interval | 2.484 | 5.701 |
| Cleanup Activity | 2.561 | 3.065 |

All four SDK samples completed with confirmed deletion. Each used 13 Activities. Reconstruction body medians for prefixes 0, 1, 8, 32, 63 and 64 (n=3 each) were 0.202, 0.281, 0.268, 0.496, 0.580, 0.485 s.

The bridge active/native differences are observed path differences, not isolated
Temporal overhead. Native OpenShell execution includes a fresh process; durable
active time excludes provisioning. SDK wall time includes submit and wait, but
excludes shared Worker startup, release loading and subsequent benchmark history
retrieval. SDK and bridge images/packages and inference timings differ, so their
difference does not isolate SDK wrapper overhead.

[Interleaved provisioning samples](../benchmarks/results/provisioning.json)

| Median, seconds | Uncompiled | Precompiled |
| --- | ---: | ---: |
| Fixture (n=3 per image) provision | 9.762 | 8.601 |
| Fixture (n=3 per image) import probe | 1.517 | 0.488 |
| vLLM policy (n=2 per image) provision | 9.133 | 8.684 |
| vLLM policy (n=2 per image) import probe | 1.328 | 0.489 |

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

Two further Sol reviews checked native admission, recovery and test coverage;
no implementation blockers remained. Review corrections made the 64-generation
limit and post-retention re-execution risk explicit. Three new transport tests
failed before implementation, then passed. The old lifecycle fake initially caused
ten failures and an interrupted wait because it lacked the native transport seam;
it now supplies that seam. The first new live test failed on protobuf metadata
field access, including its teardown. Both were corrected and the exact leaked
instance was confirmed deleted. A formatting gate also caught the new test file.
Those failed/interrupted runs are not counted as final qualification passes.

Two additional Sol reviews checked the cleanup implementation, conformance tests
and upstream proposal. Cleanup reuses the existing Activity boundary and Temporal
retry policy; no public API, service, registry or new persistence was added. Review
kept generic OpenShell deletion timeouts retryable and simplified the RFC to use
permanent tombstones as its minimal design. The proposal is a local draft only.

Test-first runs exposed five missing permanent-error classifications (six other
cases passed), then the new kind recovery test failed because the old policy
stopped after three transient failures. Those expected red runs are preserved
separately and excluded from the final qualification counts.

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

Normal cleanup and explicit cleanup Workflows retry transient errors within their
existing ten-minute window, with 30-second maximum backoff. Explicit permanent
errors stop promptly; terminal `NOT_CONFIRMED` results are not retried. There is no
continuous janitor. A retired or unacknowledged provisioning attempt can create a
sandbox late; its history remains `NOT_CONFIRMED` even after observed instances
are removed. Native request IDs deduplicate creates but do not fence late admission.
Successful receipts expire after 24 hours; unresolved receipts persist; the caller
quota is 1,000. A gateway-side lifecycle fence or platform reconciliation remains
necessary for a stronger closure claim, not a custom SDK scheduler. Resource removal does not prove physical erasure.
No p95, throughput, cold-pull, hostile-code or production availability claim follows
from these measurements.
