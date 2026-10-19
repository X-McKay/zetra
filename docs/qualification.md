# Qualification

This report records fresh, unsigned local evidence for the ordinary-worker SDK on
2026-10-19. It qualifies the admitted serial profile in this environment, not a
production service deployment.

## Review fixes and additional regression coverage

This pass fixes cleanup proof replacement, CLI handle round trips, portable wheel
pins and reserved project names. Cleanup success is server-enforced with Temporal
failed-only Workflow ID reuse. A real-service regression exercises failed cleanup,
eight concurrent reconcilers, client replacement without a polling worker, and
history replay. OpenShell recovery tests also reuse proof after release approval
is withdrawn; cleanup never requires permission to execute the release again.

The optional release allowlist is caller configuration, not a policy service.
Standard PEP 508 metadata preserves the exact SDK dependency outside a checkout.
SDK and canonical project creation share one renderer with checked provenance.
Ordinary agent images use a builder stage and contain no Git/uv executables or
qualification canary directories. Qualification fixtures retain their dedicated
root-image probes. CI permissions, checkout credentials and toolchains are explicit.

Independent Sol reviews found no actionable issues in the final recovery and
packaging changes; the rollout documentation explicitly requires fresh executions
and a separate runtime/queue for the experimental cleanup contract. No migration
of historical cleanup semantics is claimed.

## Identity and environment

[Runtime identities](../benchmarks/results/runtime.json) bind the SDK execution
package, dependency environments, template provenance, seven rebuilt images and
five agent releases. All seven installed packages matched execution-package
SHA-256 `fccedb48566cf52f4e2b954b88c667e7047949f685ccf2671c72fccc4c39b25c`. This hash covers `src/zetra` except generated template
assets, build-metadata copies and Python caches; it includes the agent Dockerfile.
The five agent images install SDK Git revision
`61a579c3de43844c7bb0ef13fe32fab000316c07` from their own authoritative uv.lock.
Installed Git provenance is checked against each release. They do not receive a
checkout overlay. The newer CLI bundle pins that execution revision explicitly;
template provenance and each image's installed dependency versions are recorded
separately. Base-image/OS/build-system inputs are not bit reproducible.
Docker Hub repository names were normalized before comparing exact image digests.

The run used `kind-zetra-validation` on Linux arm64 in a local Docker VM with
8 GiB memory. Temporal and OpenShell ran in kind; ordinary workers and shared
FileStore backing ran on the host. The earlier idle `adk-bridge-probe` cluster was temporarily paused for the final
100-case follow-up and benchmarks, then restored. Other local workloads shared
the VM. This differs from the prior benchmark environment. Images
were preloaded; registry pull time is excluded. The SDK accepts caller-configured
connections and storage; the kind installation is a qualification harness.

Live inference used `https://llm.almckay.io/v1`, model
`openai/Qwen3.6-35B-A3B-NVFP4`, temperature 0, 512 output tokens, no thinking,
serial tool calls and disabled provider retries. Inference is hosted outside kind.
This is a functional/recovery check, not a model-quality evaluation.

## Verification

| Gate | Result |
| --- | --- |
| Locked local `just check` | 241 passed, 38 deselected; lint, format, lock, docs, both example tests and fixture evaluations passed |
| Full suite through Temporal/OpenShell in kind with live vLLM | 279 distinct cases passed across runs: 179 passes in the second full attempt, then 100 passed in 282.95 s on rerun. Three earlier failed attempts are retained below; no single clean full run is claimed |
| Bridge and SDK latency benchmarks | 2 passed, zero failures/errors/skips, 176.43 s |
| Fresh provisioning benchmark | 1 passed, zero failures/errors/skips, 126.70 s |
| Canonical template | 13 repository tests and four generated-agent tests; isolated dependency-resolving wheel installation and package resource checks passed |
| Installed wheel outside checkout | Scaffold, generated-agent tests, fixture evaluation, hidden CI/gitignore files and agent build recipe passed |

[Test evidence](../benchmarks/results/tests.json) lists distinct passing cases and
each run attempt, including failures. This qualification has no single clean full
run; the failed normal-path case and all remaining cases passed in a follow-up run.

The document reader and workspace reporter use nested manifests, factories, tools
and packaged instructions. Discovery is data-only and requires explicit selection
for multi-agent projects. New tests cover unsafe/symlinked resource paths, packaged
instruction delivery, source/evidence separation, lock pin failures, renderer name
normalization, selected-agent/dataset evidence identity and canonical bundle
integrity. The generated document reader completes three durable boundaries
(model, tool, model) through the public SDK, including sandbox evaluation and
confirmed deletion. The reporter retains its five-boundary recovery coverage.

Canonical template validation uses an independently rendered project and its
actual Git-installed SDK, with no checkout PYTHONPATH override. It runs unit tests,
fixture evaluation, wheel archive checks and resource loading outside the source
checkout. The canonical repository and generated project have separate CI.
Fixture checks establish deterministic behavior, not model answer quality.
The new SDK coverage exercises independent client/worker lifetimes, repeated submission,
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
samples and shared-machine load limit interpretation. The review fixes add no bridge round trip to a successful session.
These measurements do not isolate a small latency difference from normal host,
image dependency and model variation.

[Bridge samples](../benchmarks/results/latency.json)

| Boundary | Fixture median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| Full session wall | 15.357 | 19.530 |
| Provision Activity | 8.839 | 9.242 |
| Active history interval | 2.920 | 6.799 |
| Cleanup Activity | 3.200 | 2.972 |

Native comparison

| Boundary | Fixture median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| Native ADK body | 0.233 | 3.717 |
| Native process | 0.624 | 4.118 |
| Native OpenShell exec | 0.777 | 4.693 |

[Public SDK samples](../benchmarks/results/sdk-latency.json)

| Boundary | Fixture median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| Full session wall | 14.995 | 20.008 |
| Provision Activity | 8.875 | 9.530 |
| Active history interval | 2.682 | 6.526 |
| Cleanup Activity | 3.281 | 3.842 |

All four SDK samples completed with confirmed deletion. Each used 13 Activities. Reconstruction body medians for prefixes 0, 1, 8, 32, 63 and 64 (n=3 each) were 0.275, 0.218, 0.298, 0.345, 0.444, 0.474 s.

Public SDK wall time — fixture: 14.995 s versus 16.580 s previously; vllm: 20.008 s versus 21.320 s previously. These small, separate samples do not establish a causal performance change or a statistically significant regression. Component medians are not additive.

The bridge active/native differences are observed path differences, not isolated
Temporal overhead. Native OpenShell execution includes a fresh process; durable
active time excludes provisioning. SDK wall time includes submit and wait, but
excludes shared Worker startup, release loading and subsequent benchmark history
retrieval. SDK and bridge images/packages and inference timings differ, so their
difference does not isolate SDK wrapper overhead.

[Interleaved provisioning samples](../benchmarks/results/provisioning.json)

| Median, seconds | Uncompiled | Precompiled |
| --- | ---: | ---: |
| Fixture (n=3 per image) provision | 9.787 | 7.846 |
| Fixture (n=3 per image) import probe | 1.208 | 0.548 |
| vLLM policy (n=2 per image) provision | 9.851 | 7.629 |
| vLLM policy (n=2 per image) import probe | 1.211 | 0.430 |

Each provisioning trial uses a distinct fresh sandbox, empty writable workspace
and confirmed deletion. Both images are preloaded. The vLLM-policy provisioning
trials do not call the model. Import probes run separately after provisioning;
they are not an additional component to add to provision time. There is no reused
agent state or workspace pool.

## Reviews, failures and limits

The first fresh kind run reached 184 passes before cluster-wide readiness/liveness
timeouts restarted OpenShell and several controllers. The runner's pod-bound
port-forward exited; the cold replacement case failed with connection refused.
The run was interrupted after preserving its logs. The cold-replacement case
passed on a fresh connection. The second full attempt reached 179 passes before
a provisioning heartbeat timeout caused attempt 2, leaving late-admission
uncertainty. The cancellation test expected the no-retry normal path and failed
when cleanup correctly returned `NOT_CONFIRMED`. An independent Sol review
confirmed this behavior was already required by `settled_provisioning()` and ran
27 worker tests. A follow-up passed cancellation but failed termination after
another provisioning retry. The earlier task-owned idle prototype cluster was then temporarily paused
to reduce background VM load, after verifying zero sandboxes and no active test
processes. Cancellation and the 99 remaining cases were rerun unchanged on fresh
executions. All three failed attempts remain recorded; no timeout, retry or
isolation assertion was relaxed. The heartbeat timeout coincided with OpenShell/Postgres readiness failures; its underlying host
or network cause was not isolated.

This review pass first reproduced the handle-format, cleanup-proof reuse, reserved
name and wheel-metadata failures. Test-first regressions failed before the fixes.
The first local gate stopped at three import-order lint errors; those were corrected.
A subagent's restricted broad run was interrupted after 15 passes; it is not
qualification evidence. The independent Sol recovery review ran 64 focused tests,
and the packaging reviewer ran 29 SDK and 13 canonical tests with no actionable
findings. Final image installation, full tests and benchmarks are recorded above.

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

Three Sol reviews assessed the project/lock architecture, implementation and
canonical template. Findings prompted consistent renderer naming, agent identity
in eval reports, actual built-wheel resource checks, root template CI and checked
template provenance. A final bootstrap review added a small provenance gate: execution-only SDK
commits cannot silently scaffold an incompatible provisional template. The final
CLI bundle and canonical renderer create the qualified project offline. Seed-commit
scaffold CI failures are not final-release results. The experimental release format is explicitly version 2;
there is no migration of prior release/history formats.

Project test-first runs failed before discovery/instruction support existed. An
initial scaffold run exposed copied Ruff caches as non-text files; cache exclusion
fixed it and the SDK tests passed. An early broad restricted-environment run
reported five subprocess timeouts with 65 passes before interruption; it is not
counted as qualification. The subsequent locked and cluster runs are reported
above. No timeout budget or durability assertion was relaxed.

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
errors stop promptly; unconfirmed cleanup Activities do not automatically retry.
Explicit cleanup fails non-retryably unless deletion is confirmed, and successful
cleanup Workflow IDs cannot be replaced within history retention. There is no
continuous janitor. A retired or unacknowledged provisioning attempt can create a
sandbox late; its history remains `NOT_CONFIRMED` even after observed instances
are removed. Native request IDs deduplicate creates but do not fence late admission.
Successful receipts expire after 24 hours; unresolved receipts persist; the caller
quota is 1,000. A gateway-side lifecycle fence or platform reconciliation remains
necessary for a stronger closure claim, not a custom SDK scheduler. Resource removal does not prove physical erasure.
No p95, throughput, cold-pull, hostile-code or production availability claim follows
from these measurements.
