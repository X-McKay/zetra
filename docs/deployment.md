# Local deployment

`scripts/kind-up.sh` targets only `kind-zetra-validation` with `.state/kubeconfig`.
It installs pinned Cilium, Agent Sandbox, OpenShell mTLS, PostgreSQL and Temporal.
The application worker and immutable artifact directory run on the host. The
vLLM endpoint runs separately. No script changes the global Kubernetes context.

The tested starting tuple is Python 3.12.14, ADK 2.12.0, Gen AI 2.31.0,
Temporal Python SDK 1.34.0/server 1.32.0, OpenShell 0.1.4, LiteLLM 1.95.0,
kind node Kubernetes 1.35.0, Cilium 1.20.3 and Agent Sandbox 1.0.7.
The uv lockfile supplies the exact Python graph. Image builds are immutable after
creation; upstream base image tags are not a complete signed supply-chain policy.

For interactive sessions, create explicit loopback forwards:

```bash
kubectl --kubeconfig .state/kubeconfig --context kind-zetra-validation \
  -n temporal port-forward service/temporal-frontend 7233:7233
kubectl --kubeconfig .state/kubeconfig --context kind-zetra-validation \
  -n openshell port-forward service/openshell 8080:8080
```

In another terminal export `TEMPORAL_ADDRESS=127.0.0.1:7233`,
`OPENSHELL_ADDRESS=127.0.0.1:8080`, and `OPENSHELL_TLS_DIR=$PWD/.state/tls`.
Keep both forward addresses and the OpenShell CA/client identity stable. Keep
session state private and retain it for recovery. The standard runner is a
single-host local platform integration. Production remote TLS/identity, durable
artifact storage, release signatures, leases and a continuous reconciler need
separate qualification.

Session tests require confirmed sandbox deletion. Shared cluster services remain
available. To remove this disposable installation explicitly:

```bash
kind delete cluster --name zetra-validation
```

This also removes the cluster's Temporal/PostgreSQL volumes. Delete local `.state`
only when its retained session/artifact data and certificates are no longer needed.

## Reproduce complete qualification

```bash
uv run python scripts/build_qualification.py
BRIDGE_LIVE_VLLM=1 uv run zetra qualify
BRIDGE_LATENCY=1 uv run zetra qualify --suite latency
BRIDGE_PROVISIONING=1 BRIDGE_PROVISION_BASELINE_IMAGE=zetra-validation:source \
  uv run zetra qualify --suite provisioning
```

The image script builds two example packages, a freshly scaffolded package, a live-model
variant and a blocked tool fixture for deterministic cancellation tests. It also builds the same runtime
with and without trusted bytecode compilation for interleaved provisioning samples.
Only synthetic prompts and workspace files are used. Benchmark JSON remains in
`.state`; the checked-in qualification report records the scoped results.
