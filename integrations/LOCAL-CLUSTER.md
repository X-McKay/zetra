# Disposable local validation cluster

Checked October 8, 2026. The local cluster **kind-zetra-validation** was created successfully using kind v0.33.0, Podman 6.1.0 and a dedicated rootful AppleHV VM. The API uses the ignored dedicated kubeconfig `.tools/zetra-kubeconfig`; the shared Kubernetes context was not changed.

## Runtime isolation and actual observations

The existing Podman `kz-eval` VM had a full disk and unavailable container APIs. With user authorization, it was stopped; its images, services and data were preserved. Podman's AppleHV setup permits one active VM, so a separate `zetra-validation` VM uses 4 CPUs, 6144 MiB RAM and a 25 GiB disk. Host checks found 403 GiB free disk and 24 GiB RAM.

The new VM mounts only the workspace. This avoids inheriting an existing global container configuration referencing an unavailable `runsc` runtime. Its tested runtime uses crun, rootful Podman, cgroups v2/systemd, seccomp and SELinux. The Linux 7.1.10 kernel exposes BTF and enables Landlock and BPF in its LSM list. These prerequisites do not themselves prove an installed security control.

```sh
podman machine init --cpus 4 --memory 6144 --disk-size 25 \
  --rootful --update-connection=false \
  --volume /Users/al/git/zetra:/Users/al/git/zetra zetra-validation
podman machine start --update-connection=false zetra-validation
```

The execution environment here requires the launching terminal session to remain alive; session 50315 currently holds the VM processes. Do not close it during validation. A normal persistent user terminal can serve the same purpose after this session ends.

## Explicit targeting

For the current VM, use its own rootful API, without changing the default connection:

```sh
export PATH="/Users/al/git/zetra/.tools:/opt/homebrew/bin:$PATH"
export KIND_EXPERIMENTAL_PROVIDER=podman
export CONTAINER_HOST="ssh://root@127.0.0.1:57654/run/podman/podman.sock"
export CONTAINER_SSHKEY="/Users/al/.local/share/containers/podman/machine/machine"
export KUBECONFIG="/Users/al/git/zetra/.tools/zetra-kubeconfig"
podman info
kubectl --kubeconfig /Users/al/git/zetra/.tools/zetra-kubeconfig \
  --context kind-zetra-validation get nodes
```

The SSH port changes when the VM is recreated; inspect `podman system connection list --format json`. The original global default connection remains `kz-eval`, while that VM is stopped.

```sh
kind create cluster --name zetra-validation \
  --config integrations/kind-config.yaml \
  --kubeconfig .tools/zetra-kubeconfig --wait 120s
```

The tested node image is digest-pinned to Kubernetes v1.35.8 with containerd 2.3.4. The initial cluster loaded the configuration before `disableDefaultCNI` was added, so it initially had kindnet. That kindnet deployment was subsequently removed and Cilium v1.20.2 was installed. Actual allowed-peer and denied-peer probes passed under Cilium; see the [kernel/network report](evidence/kernel-network-report.json). The committed configuration disables the default CNI for future creation; those nodes remain NotReady until a CNI is installed. Default kindnet does not establish NetworkPolicy enforcement.

Exact creation commands, checks, attempts and cleanup steps are recorded in [local-cluster-attempt.json](local-cluster-attempt.json). That record describes cluster creation before the subsequent platform installations. The [integration index](README.md) links the later kernel/network, OpenShell, gateway, Temporal, and telemetry evidence and reproduction procedures; do not infer their outcomes from the creation record.

## Deliberate cleanup

Cleanup has not been run. Delete only this disposable cluster using the explicit own Podman connection and kubeconfig:

```sh
kind delete cluster --name zetra-validation \
  --kubeconfig /Users/al/git/zetra/.tools/zetra-kubeconfig
podman machine stop zetra-validation
podman machine rm --force zetra-validation
```

Podman machine removal can change the selected default connection; check it and restore `kz-eval` if necessary. Do not delete or repair `kz-eval` as part of this cleanup. Starting it again is an explicit separate operational action.

Sources: [kind installation and provider selection](https://kind.sigs.k8s.io/docs/user/quick-start/), [kind v0.33.0 image digests](https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0).
