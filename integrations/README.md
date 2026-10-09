# Live integration fixtures

These are actual, narrowly scoped validation runs from October 8, 2026, Eastern time. They establish the recorded behavior of a disposable local installation. They do not authorize a production release or prove that the entire Zetra architecture has been composed and qualified. The developer toolkit's Python capability checks remain cooperative checks; the platform tests below exercise distinct enforcement boundaries.

## Verified scope and evidence

| Boundary | Recorded result | Evidence and procedure | Important limit |
| --- | --- | --- | --- |
| Tetragon eBPF and Cilium network policy | **8/8 passed**: policy loaded; unrelated access allowed; unselected pod unaffected; selected sensitive read killed with exit137 and no returned bytes; correlated kernel action; network baseline reachable; approved peer allowed; forbidden direct peer denied | [Kernel/network report](evidence/kernel-network-report.json), [normalized events](evidence/tetragon-normalized-events.jsonl), [fixture runner](kernel-network-qualify.py) | Synthetic selected pods on the recorded kernel. Startup races, policy-agent outages, production kernel compatibility, and IPv6/DNS/external-provider bypass remain open. |
| OpenShell supervised Python sandbox | **7/7 passed**: allowed scratch access; outside read/write denied; `/etc` write denied; unapproved HTTP and direct TCP denied; actual knowledge-agent factory executed | [Report](openshell-live-output/report.json), [probe result](openshell-live-output/probe.json), [supervisor logs](openshell-live-output/supervisor.log), [OpenShell guide](openshell-LIVE-VALIDATION.md) | Development principal-authentication setting; user namespaces disabled. Separate boundary test, not gateway/sandbox composition or credential/revocation qualification. |
| agentgateway identity and MCP tool authorization | **11/11 passed**: missing/invalid identity denied; valid MCP initialization/read; discovery filters forbidden tool; forbidden write and wrong subject rejected; actual reference-agent chain | [Gateway report](gateway-live-output/gateway-report.json), [gateway guide](gateway-LIVE-VALIDATION.md) | Native host gateway and synthetic issuer/backend. TLS, production issuer/resource authorization, controller APIs, and tight revocation bounds are unqualified. |
| Temporal durability | Real transient-failure retry produced attempts **[1,2]**, exact reference result, and successful recorded-history replay | [Independent Temporal report](evidence/temporal-local-test.json), [gateway-chain report](gateway-live-output/temporal-gateway-report.json), [chain history](gateway-live-output/temporal-gateway-history.json) | Actual SDK test server on the host; not a production Temporal cluster. No write/approval activity, heartbeat preservation, or failover qualification. |
| OpenTelemetry delivery | **8/8 passed**: cluster collector received actual gateway spans, propagated W3C trace identity, matching release/run/operation metadata, denied-tool observation, and no raw JWT in captured synthetic logs | [Telemetry report](gateway-live-output/telemetry-report.json), [collector log](gateway-live-output/collector.log), [collector manifest](telemetry-collector.yaml) | Diagnostic header correlation is not authorization. Captured-log check is not universal DLP; a single shared Temporal-to-gateway tracing DAG was not qualified. |
| Toolkit cluster discovery | Actual read-only API discovery passed | [Discovery report](evidence/toolkit-cluster-discovery.json) | Discovery only; not admission or security enforcement. |
| Worker image packaging | Real locked ARM64 OCI build; nonroot/read-only runtime inventory; write capability rejected before connection | [Image report](evidence/worker-image.json), [worker recipe](../deploy/Dockerfile), [build guide](../deploy/README.md) | No registry image publication, signature/SBOM/vulnerability qualification or AMD64 build. Export recompression changes the manifest digest; identities are recorded separately. |
| Worker on Kubernetes | Actual knowledge workflow returned the expected result; 11-event history replayed | [Worker report](evidence/worker-temporal-kubernetes-report.json), [history](evidence/worker-temporal-kubernetes-history.json), [qualification fixture](../deploy/worker-qualification.yaml) | Separate restricted pod with loopback Temporal development server and ephemeral SQLite. No production persistence, HA, mTLS or full gateway/sandbox composition. |
| Native candidate API validation | Four native resources accepted by server dry-run; zero replicas and actual built image preserved | [Render report](evidence/render-server-report.json), [runner](render-qualify.py) | API schema acceptance only; no activation or signed admission validation. Temporary namespace removed. |

The initial 60-second-expired JWT was accepted by the real gateway. That observation is preserved in [the initial expiry record](gateway-initial-expiry-observation.json); the final expired-token test used one hour and was rejected. Include measured leeway when designing authority freshness and revocation. A failed initial OpenShell HTTP expectation is also retained [here](openshell-initial-probe-observation.json): the sandbox denied the connection with an explicit permission error before an HTTP response, so the final assertion tests that actual boundary rather than treating timeout or DNS failure as a deny proof.

## Prerequisites and installed pins

Read [LOCAL-CLUSTER.md](LOCAL-CLUSTER.md) before operating the VM. The tested host is macOS ARM64 with Podman 6.1.0, kind 0.33.0, `kubectl`, Helm, OpenSSL, and the project's locked Python environment. The separate rootful `zetra-validation` VM has 4 CPUs, 6144 MiB RAM, and a 25 GiB disk. Kubernetes 1.35.8 runs containerd 2.3.4 on ARM64 Linux `7.1.10-200.fc44.aarch64` with BTF and Landlock/BPF prerequisites. Exact node image identity and cluster-creation observations are in [the creation record](local-cluster-attempt.json).

| Component | Installed version or immutable identity | Required fixture settings |
| --- | --- | --- |
| Cilium Helm chart | `cilium/cilium` **1.20.2**, official `https://helm.cilium.io` repository | `ipam.mode=kubernetes`, `kubeProxyReplacement=false`, `operator.replicas=1`, `bpf.hostLegacyRouting=true`; Cilium actually replaced the initial kindnet installation. |
| Tetragon Helm chart | `cilium/tetragon` **1.7.1**, same official repository | `tetragon.enablePolicyFilter=true`; fixture-scoped `TracingPolicyNamespaced`, not a blanket file policy. |
| OpenShell chart/server/CLI | **0.1.2**; official OCI chart artifact `helm-chart-0.1.2.tgz` | TLS/mTLS on, live Agent Sandbox preflight on, separate workload/supervisor boundary runtime, `server.enableUserNamespaces=false`, `server.appArmorProfile=Unconfined`, anonymous telemetry off. **Local fixture only:** `server.auth.allowUnauthenticatedUsers=true`; principal authentication and AppArmor/user namespace protections are not qualified. |
| Agent Sandbox API | **1.0.6**, `v1beta1` API | Installed for OpenShell's Kubernetes compute driver. OpenShell's Kubernetes path is experimental; production adoption needs an explicit maturity decision. |
| agentgateway | **v1.6.0**, official Darwin ARM64 release | Runner verifies SHA256 `6cebe8bd57262edce23a65a376d2f5a27b97cdfef609740642edbe75e1e9c685`; actual gateway is host loopback. |
| Temporal Python SDK | **1.34.0**, resolved by `uv.lock` | Actual SDK test server, cached under `/tmp/zetra-temporal-bin`; no external service credentials. |
| OTel Collector | **0.162.0**, official ARM64 image digest pinned in [manifest](telemetry-collector.yaml) | OTLP/gRPC receiver and debug exporter in `zetra-gateway-test`; host gateway exports through loopback forwarding. |
| Supervised Python workload | **3.12.13-slim**, resolved digest pinned by [OpenShell runner](openshell-reproduce.py) | Strict `landlock.compatibility: hard_requirement`, explicit filesystem roots, and no allowed network routes/providers. |

OpenShell's CLI, workload, supervisor, and effective policy hashes are captured in its report. No private signing keys, client certificates, kubeconfigs, or downloaded binaries belong in source control. The runners isolate these under ignored `.tools`; only selected public fixture code is uploaded.

## Reproduce on the dedicated disposable target

Run from the repository root. Bootstrap the locked environment using the root contributor instructions (`mise install`, then `just bootstrap`, or `uv sync --locked --all-extras`). This creates `.venv` with the Temporal extra. Create the separate VM and dedicated kind cluster using [LOCAL-CLUSTER.md](LOCAL-CLUSTER.md), then install the lab controllers with:

```sh
bash integrations/bootstrap-local-platform.sh
```

The [bootstrap script](bootstrap-local-platform.sh) accepts no alternate target and supports only the tested macOS ARM64 host. It requires the repository's `.tools/zetra-kubeconfig`, context `kind-zetra-validation`, a loopback HTTPS API, and exactly the expected `zetra-validation-control-plane` node before any cluster write. Helm, `kubectl`, `curl`, `shasum`, `tar`, and `.venv/bin/python` must already be available. It installs only the pinned chart/controller versions above plus the official native OpenShell CLI into ignored `.tools/openshell`, verifies SHA256 for all downloaded artifacts and the extracted CLI, uses ignored workspace Helm state without adding global repositories, and waits for Agent Sandbox's CRD to become established before OpenShell's enabled live preflight. It can remove the original local kindnet daemonset before Cilium installation. Existing exact releases skip Helm writes; unexpected versions, release state, or required settings cause refusal rather than silent upgrades.

The script was actually run against the existing validated tuple: all four installations were skipped and all readiness checks passed. The [bootstrap report](evidence/platform-bootstrap.json) records its script/artifact hashes, release actions, and explicit lab settings. Its fresh-install path has not been exercised on a second empty cluster. It does not create the VM/cluster, create production identities, or prove boundary enforcement; run the probes below after bootstrap. Its unauthenticated-user, unconfined AppArmor, and disabled-user-namespace settings are explicit disposable-lab accommodations, not production installation guidance. OpenShell's official [chart documentation](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/deploy/helm/openshell/README.md), [pinned values](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/deploy/helm/openshell/values.yaml), and Agent Sandbox's [installation documentation](https://agent-sandbox.sigs.k8s.io/docs/getting_started/install_prerequisites/) provide the upstream context. The fixture runners use the dedicated kubeconfig and already installed controllers.

Always name both the dedicated kubeconfig and context. Do not change the shared default context or use the unrelated `kz-eval` Podman connection:

```sh
kubectl --kubeconfig "$PWD/.tools/zetra-kubeconfig" \
  --context kind-zetra-validation get nodes

.venv/bin/python integrations/kernel-network-qualify.py \
  --kubeconfig "$PWD/.tools/zetra-kubeconfig" \
  --output integrations/evidence/kernel-network-report.json

.venv/bin/python integrations/openshell-reproduce.py

bash integrations/gateway-reproduce.sh

PYTHONPATH=src .venv/bin/python integrations/temporal_local_test.py \
  --root examples/knowledge \
  --history tests/fixtures/temporal-readonly-history.json \
  --output integrations/evidence/temporal-local-test.json
```

The kernel runner applies only its synthetic namespace/pods and policies, verifies independent selection and denial outcomes, and records fixture hashes. The OpenShell runner creates a fresh sandbox, copies the actual current knowledge-agent factory/runtime, requires acknowledged effective policy hash, runs all seven probes, and captures supervisor deny logs. The gateway runner verifies the proxy binary, deploys the collector, executes real reference agent → Temporal → gateway → deterministic MCP receiver, then checks exported spans. This chain has no paid model calls or business-system writes. Read the two live-validation guides for exact topology, artifact paths, and remaining acceptance controls.

Rerunning replaces the named reports. Preserve evidence separately when comparing changes. These JSON reports are observations, not signed production attestations, and must not be substituted for the release-evidence contracts in the playbooks.

## Cleanup and current state

Cluster/VM cleanup has **not** been run; the disposable installation remains available for inspection. The original `kz-eval` VM is stopped with its data preserved, and its default Podman connection remains unchanged. Keep launcher session 50315 alive while validating.

The gateway runner stops its own proxy/backend and loopback forward but leaves the collector namespace. The OpenShell runner stopped its forward and removed its fresh reproduction sandbox; the initial manually inspected `zetra-python` and `zetra-strict` sandboxes may remain. The kernel fixtures and installed controllers remain. Do not infer cleanup from a passed report: consult the latest OpenShell `cleanup.log` when available.

To remove only the collector fixture:

```sh
kubectl --kubeconfig "$PWD/.tools/zetra-kubeconfig" \
  --context kind-zetra-validation delete namespace zetra-gateway-test
```

Full disposable cluster/VM cleanup is documented in [LOCAL-CLUSTER.md](LOCAL-CLUSTER.md). Use its explicit Podman connection and dedicated kubeconfig; removing or repairing `kz-eval`, restarting it, or changing any shared cluster is outside fixture cleanup.

The technical contract review corrected an unused OpenShell `supervisor.sideloadMethod` Helm value. The chart/driver define separate workload and supervisor runtimes; storing an extra Helm value did not establish that behavior. The corrected bootstrap passed readiness again with all four releases skipped. The [original readiness report](evidence/platform-bootstrap-original.json) is retained; [the current report](evidence/platform-bootstrap.json) records the corrected script hash. See [the pinned integration contract review](../docs/research/integration-contract-review.md).
