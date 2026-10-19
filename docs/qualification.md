# Qualification

The SDK source in this branch passed local and real-cluster qualification on
2026-10-17/18 (America/New_York/UTC). These are fresh Zetra measurements with
synthetic inputs, recorded as unsigned local evidence. They qualify the supported
serial profile on this environment; they are not production certification.

## Identity and environment

[Runtime identities](../benchmarks/results/runtime.json) bind the SDK source,
lockfile, seven built images and five agent releases. Every image was inspected
in a network-disabled container and matched the SDK source SHA-256
`68b0b5bb2c13c8188cd54e7fb30a11b3ac98b12520cdecec37c4ead1a07af9cd`.
All agent release digests matched their built image identities.

The run used `kind-zetra-validation` on Linux arm64 in a local Docker VM with
8 GiB memory. Temporal and OpenShell ran in kind; the worker and artifact store
ran on the host. Other local workloads shared the VM. Images were preloaded;
these measurements exclude registry pull time. See [deployment](deployment.md)
for pinned versions and reproduction commands.

Live inference used `https://llm.almckay.io/v1`, model
`openai/Qwen3.6-35B-A3B-NVFP4`, temperature 0, 512 output tokens, no thinking,
serial tool calls and disabled provider retries. That endpoint runs separately
from kind. This is a functional provider/recovery check, not a model-quality study.

## Verification

| Gate | Result |
| --- | --- |
| `nix develop --command just check` | 139 tests passed, 29 cluster tests deselected; lint, format, lock and docs checks passed; both example tests and fixture evaluations passed |
| Full suite through OpenShell/Temporal in kind, live vLLM enabled | **168 passed, zero failures/errors/skips**, 616.05 s |
| Bridge and public SDK latency benchmarks | 2 passed, 229.67 s |
| Interleaved fresh-sandbox provisioning benchmark | 1 passed, 137.37 s; 10 distinct sandboxes |
| Installed wheel outside checkout | Scaffold, generated-agent test, fixture evaluation and embedded build assets passed |
| Contributor configuration | ShellCheck, actionlint and Nix formatting passed |

[Test evidence](../benchmarks/results/tests.json) lists all 168 test cases. The
suite exercised accepted-prefix reconstruction, changed-request rejection,
worker restart/SIGKILL, replacement sandbox recovery, lost effect completion,
heartbeat timeout, cancellation, termination reconciliation, receipt/workspace
validation, actual filesystem/network policy denials, agent-specific allowlists
and concurrent fresh workspaces. Unknown effect outcomes remain unknown; the
bridge does not redispatch them automatically.

Public SDK cases covered both example packages and a newly scaffolded third
package, repeated submission, live inference, cancellation/termination, sandbox
evaluation and reading accepted output after confirmed sandbox deletion. Sol
reviews covered correctness, lifecycle ownership, developer workflow, packaging
and benchmark interpretation; their actionable findings received fixes and
regression coverage before this final run.

## Latency

All times below are seconds. A full session includes a newly provisioned sandbox,
execution and confirmed deletion. Component medians need not sum to the median
wall time. Small sample counts and shared-machine load limit interpretation.

[Bridge samples](../benchmarks/results/latency.json):

| Boundary | Scripted median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| Full session wall | 17.366 | 22.602 |
| Provision Activity | 9.796 | 10.588 |
| Active history interval | 3.965 | 8.706 |
| Cleanup Activity | 3.010 | 2.990 |
| Native ADK body | 0.667 | 5.168 |
| Native process | 1.415 | 5.812 |
| Native OpenShell exec | 1.760 | 6.824 |

The active interval exceeded the native OpenShell exec by 2.205 s for the scripted
medians and 1.882 s for the live samples. These are observed path differences,
**not isolated Temporal overhead**: the native path includes a fresh process,
the durable active interval excludes provisioning, and inference/load vary.
Each durable bridge sample used 13 Activities and 12 OpenShell exec exchanges.

The 18 fresh-process reconstruction probes used prefixes 0, 1, 8, 32, 63 and 64
(three samples each). Body-time medians were 0.395, 0.353, 0.297, 0.433, 0.601 and
0.790 s respectively. These are reconstruction probes, not a full 64-boundary
end-to-end performance result.

[Public SDK samples](../benchmarks/results/sdk-latency.json):

| Boundary | Fixture median (n=3) | Live sample (n=1) |
| --- | ---: | ---: |
| `SessionClient.run` wall | 20.435 | 32.487 |
| Provision Activity | 10.809 | 10.451 |
| Active history interval | 5.336 | 18.200 |
| Cleanup Activity | 2.858 | 3.502 |

All four SDK samples used 13 Activities and ended with `DELETED`. SDK wall time
excludes release loading, client construction and subsequent history retrieval.
Active time spans first advance scheduling through final advance completion.
SDK-versus-bridge differences do not isolate wrapper cost: images, packaging,
execution order and live inference differ. The SDK timing test inspects histories;
replay/recovery correctness is established by the separate conformance suite.

## Provisioning comparison

[Interleaved samples](../benchmarks/results/provisioning.json) compare the same
SDK source/dependencies with and without precompiled trusted Python bytecode.
Both images were preloaded. Every trial created a distinct sandbox with empty
workspace/object directories, used the same policy within its provider group,
and confirmed deletion before continuing. There is no reuse of session state.

| Median, seconds | Uncompiled | Precompiled | Observed change |
| --- | ---: | ---: | ---: |
| Scripted provisioning (n=3 per image) | 10.784 | 8.834 | −18.1% |
| vLLM-policy provisioning (n=2 per image) | 9.491 | 9.016 | −5.0% |
| Scripted fresh import probe | 1.579 | 0.565 | −64.2% |
| vLLM-policy fresh import probe | 1.704 | 0.614 | −64.0% |

The vLLM-policy provisioning trials do not perform model inference. The import
probe runs separately after provisioning and is not an extra component to add to
provisioning time. These small samples support retaining precompilation, but do
not establish a stable percentage improvement under other loads. Docker-reported
image size increased from 144,427,495 to 180,202,670 bytes (about 35.8 MB/24.8%);
registry transfer and cold-pull costs were not measured.

After all tests and benchmarks, cluster inspection found zero Sandbox resources,
zero session pods and zero session PVCs/PVs. Only the shared OpenShell and
PostgreSQL data volumes remained; all shared service pods were Running/Ready.

## Development findings and final rerun

Earlier failures were not counted as final passes:

- A wrong Temporal exception import and querying completed workflows without a
  worker were corrected. Closed results now come directly from Temporal.
- Reviews found gaps in ownership/gateway binding, callback admission, terminal
  reconciliation, fixture subprocess isolation, package dependency constraints,
  unambiguous source hashing, unsuccessful CLI exit status and preserving the
  canonical build recipe. Fixes have regression coverage.
- Running builds alongside tests exposed two process-test startup failures with
  2/3-second cold-import budgets. The harness now allows 10 seconds and always
  cancels/reaps children; production timeout semantics did not change.
- A superseded full run stopped at 119 passed/1 failed when source-fingerprint
  changes correctly rejected a generated agent's mismatched release. All images
  were rebuilt and their source verified before the final 168-test run.

The final local gate, cluster suite and benchmarks ran sequentially, without
concurrent image builds. The dependency emitted one upstream warning in the
local and full suites; it did not skip tests or change results.

## Scope and remaining gates

The supported profile is static, serial and nonstreaming, with at most 64
model/tool boundaries and a 30-minute session limit. Agent code, tools and runtime
share a sandbox trust domain and must cooperate with the execution protocol.
OpenShell enforces the outer filesystem/network boundary. Two reviewed policy
profiles are provided; profiling never automatically grants access.

This runner owns sessions from one host with private persisted state and a stable
Temporal/OpenShell backend. Production still needs distributed ownership/fencing,
a continuous reconciler, replicated artifact storage, platform identity and
release authorization, remote authentication and operational qualification.
Endpoint/certificate pinning cannot detect an administrator replacing a backend
behind the same identity. Confirmed deletion means observed sandbox/resource
removal, not physical storage erasure; accepted output and private recovery state
remain outside the sandbox. No p95, throughput, cold-pull, hostile in-sandbox code
or production availability claim follows from these results.
