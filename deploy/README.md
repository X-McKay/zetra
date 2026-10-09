# OCI worker image and Kubernetes qualification

The reference image contains the actual Zetra toolkit and `examples/knowledge` at `/app/agent`. Its entrypoint is `python -m zetra.temporal_worker --root /app/agent`. It supports trusted read-only knowledge execution and explicitly configured `mcp.read_status`; it rejects write capabilities before connecting to Temporal. This is an executable baseline for platform development.

## Build the image

From the repository root, enter the Nix or mise environment and run `just bootstrap`. An existing isolated Podman connection is required; the tested connection is `zetra-validation-root`. Review [local cluster setup](../integrations/LOCAL-CLUSTER.md) first.

```sh
ZETRA_PODMAN_BIN=/opt/homebrew/bin/podman just worker-image
```

The build script selects that connection explicitly, uses the versioned tag `localhost/zetra-worker:0.1-lab`, and does not change defaults or push an image. `.dockerignore` permits only declared build inputs; local credentials, kubeconfigs, tools and attachments cannot enter this context. Official Python 3.12.13 and uv 0.12.7 image indexes are pinned by digest in [Dockerfile](Dockerfile). Missing pinned bases may be pulled; Python dependencies are fetched from the registries recorded in `uv.lock`.

The builder runs `uv sync --locked --no-dev --extra temporal --no-editable`. It installs a wheel rather than an editable source package, then copies its virtual environment into a separate runtime stage. uv and build caches remain in the builder. pip is removed from the final Python base, and Ruff, ty and prek are absent. Temporal's transitive `types-protobuf` package remains because the SDK declares it. The final runtime uses UID/GID 10001, disables bytecode writes, and makes `/app` and the installed environment read-only. No tokens, endpoint credentials or private keys are baked in. This follows [Astral's documented container integration](https://docs.astral.sh/uv/guides/integration/docker/).

The tested platform is Linux ARM64. The base indexes support other platforms, but an amd64 worker build has not been qualified. Build-backend dependencies remain bounded by `pyproject.toml`; the current process does not claim bit-for-bit reproducibility, SBOM generation, signature verification or vulnerability qualification.

## Inspect and load the exact artifact

```sh
podman --connection zetra-validation-root run --rm \
  --read-only --network none --cap-drop ALL \
  --security-opt no-new-privileges \
  localhost/zetra-worker:0.1-lab --help

ZETRA_PODMAN_BIN=/opt/homebrew/bin/podman \
ZETRA_KIND_BIN=/Users/al/git/zetra/.tools/kind just worker-load
cat .tools/worker-kind-reference
```

The load helper exports an OCI archive under ignored `.tools/`, targets only `kind-zetra-validation`, retains the actual OCI index through containerd's `--index-name`, and records its verified CRI reference. It loads an image without starting a pod. Connection URI and SSH identity are discovered from the explicitly named Podman connection, avoiding a stale port or global default.

Distinguish the image identities. Podman's local manifest can change when an export compresses layers. The exported OCI index selects an ARM64 manifest; that manifest references the config image ID. The qualified Kubernetes reference is the actual index digest, rather than the Podman storage digest or config ID. [worker-image.json](../integrations/evidence/worker-image.json) records all three forms. The recorded build uses:

```text
OCI index:    sha256:b8dc0f5920797713eb87c1f2450fc18d2d5db5ba7cbd85d4e72f33d2f4ca4462
ARM64 manifest: sha256:f2245a1a696d0ebec49cc3cb0fb145b6b5bd9953000bc0a3e48d0f305f2fd240
Config image: sha256:4052966b888a888880def2837c3a69ce1e8798eab44484108d4a75ac4ecc1787
```

A changed build can produce a different digest. Replace the worker reference in the qualification fixture with the new `.tools/worker-kind-reference` before testing; do not relabel a different target using an old digest. A registry release is a separate governed step: publish the approved artifact, verify its registry digest and attestations, and render against that real registry reference.

## Reproduce the Kubernetes workflow test

[worker-qualification.yaml](worker-qualification.yaml) is a separate lab fixture. It runs one worker alongside the official digest-pinned Temporal CLI 1.9.1 development server, creates the `zetra-worker-lab` Temporal namespace, and uses pod-local `127.0.0.1:7233`. There is no Kubernetes Service, UI or public ingress. The pod disables service-account token mounting, uses non-root identities, RuntimeDefault seccomp, read-only roots and dropped capabilities. A deny-all pod NetworkPolicy is configured. Only the development server's SQLite state uses a bounded writable `emptyDir` at `/tmp`.

```sh
kubectl --kubeconfig .tools/zetra-kubeconfig \
  --context kind-zetra-validation apply -f deploy/worker-qualification.yaml
kubectl --kubeconfig .tools/zetra-kubeconfig \
  --context kind-zetra-validation -n zetra-worker-qualification \
  wait pod/zetra-worker-temporal --for=condition=Ready --timeout=60s
uv run --locked --all-extras python integrations/worker_temporal_kubernetes_test.py \
  --kubeconfig .tools/zetra-kubeconfig --context kind-zetra-validation \
  --podman /opt/homebrew/bin/podman
```

The client binds its own port-forward to loopback, sends a controlled `{"key":"zetra"}` request through the actual Temporal worker, checks the expected answer, fetches its recorded history, and replays it with the installed SDK. Before cleanup it verifies live container metadata and hashes the OCI index/manifest chain against the final build config. The client removes its port-forward even on failure. Results are in [worker-temporal-kubernetes-report.json](../integrations/evidence/worker-temporal-kubernetes-report.json), with a [public recorded history](../integrations/evidence/worker-temporal-kubernetes-history.json).

The tested Go development server requires `USER=zetra` when running under a numeric UID without a passwd entry; the fixture provides it. The worker can restart during concurrent server startup. Container restart counts are recorded rather than treated as production readiness. The actual controlled workflow and replay passed; cleanup removed the qualification namespace after evidence collection.

```sh
kubectl --kubeconfig .tools/zetra-kubeconfig \
  --context kind-zetra-validation delete namespace zetra-worker-qualification
```

The development server is ephemeral and single-pod. This check does not establish production HA, disaster recovery, mTLS, live model quality, full history compatibility or negative-path NetworkPolicy enforcement. [Temporal documents this image as its development server](https://github.com/temporalio/cli#run-via-docker). The native deployment renderer continues to withhold activation until mandatory platform integrations and release gates qualify; this lab pod does not change that rule.

## Runtime configuration for an approved deployment

Pass explicit Temporal address, namespace and task queue. For a non-loopback server the worker requires all three TLS files. Mount credentials read-only with permissions readable by UID/GID 10001, and set arguments such as:

```yaml
args:
  - --address
  - temporal.example.internal:7233
  - --namespace
  - approved-agent-namespace
  - --task-queue
  - approved-readonly-release
  - --tls-ca
  - /run/temporal/tls/ca.pem
  - --tls-cert
  - /run/temporal/tls/client.pem
  - --tls-key
  - /run/temporal/tls/client.key
```

An MCP agent additionally needs `--gateway-endpoint` and an explicitly mounted `--gateway-token-file`; bearer tokens are read from the file on each request, supporting rotation. The current image embeds the knowledge agent, so another approved agent package requires a separately built and evaluated image. Write-capable packages remain denied by this worker.

If using the cooperative stop-file control, project the externally managed stop state into `/app/agent/.zetra` using a read-only volume. It is checked at runtime capability boundaries. Projection delay and in-flight effects require the separate gateway revocation, fencing and termination controls described in the governance playbook. A read-only filesystem does not itself implement containment, nor does this worker image install OpenShell, Tetragon or gateway policies; platform qualification and admission bind those controls to the released artifact.
