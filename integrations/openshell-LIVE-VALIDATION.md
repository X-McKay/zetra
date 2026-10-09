# OpenShell live sandbox validation

Validated October 8, 2026, Eastern time, in the authorized disposable `kind-zetra-validation` cluster. This qualifies the recorded local sandbox tuple for the tested operations; it is not a production principal-authentication or platform-wide security attestation.

## Actual topology and versions

OpenShell Helm chart/server/CLI `0.1.2` used the Kubernetes compute driver and the installed Agent Sandbox `1.0.6` API. The cluster runs Kubernetes `1.35.8` on the Linux ARM64 runtime VM with Cilium `1.20.2`. The client used a loopback-only port-forward, gateway mTLS, and isolated repository-local XDG state. Client certificates were extracted directly into ignored `.tools/config/openshell/gateways/zetra-local/mtls` with restricted permissions; no private material was printed or placed in evidence.

The pinned driver injects the workload-side boundary bootstrap and creates a separate supervisor pod and boundary Service. An earlier lab Helm invocation stored an unused `supervisor.sideloadMethod=init-container` value; that key is absent from the pinned chart and did not establish runtime topology. The reproduction bootstrap no longer submits or treats it as a required control. `server.enableUserNamespaces` is the chart key and remains false in this tuple. The installed Helm values also set `server.appArmorProfile=Unconfined`; AppArmor protection is not qualified. The development gateway permits unauthenticated users after its mTLS transport (`server.auth.allowUnauthenticatedUsers=true`); that setting is explicitly outside production acceptance. Anonymous product telemetry is disabled. No provider credentials were attached.

The supervised workload image was pinned to the actually resolved ARM64 Python image:

```
docker.io/library/python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
```

The actual supervisor image was:

```
ghcr.io/nvidia/openshell/supervisor@sha256:d7b5264bb6bc56f4796e6fa3617b8e4a8d785be0b7293542efd8cc250b0fb67a
```

The official CLI SHA256 is `789093ba9278271f2617642cadfabe58d3c08f9f8a5a11c29dcbd4ab60d6611f`. The supported policy uses `landlock.compatibility: hard_requirement`, not the upstream default `best_effort`. Effective version1 policy hash was `4f1991b4e4a21fb8a047e7af2da5577841b1994598c8fdcdc635e0eb9776b965`. The supervisor acknowledged that hash as loaded and reported its isolation boundary attached and enforcement confirmed.

## Seven actual probes passed

| Probe | Observed result |
| --- | --- |
| Approved `/tmp` scratch write/read | Succeeded, and fixture removed its file |
| Read world-readable `/var/lib/dpkg/status` outside permitted roots | `EACCES`; original mode `0644` |
| Write into world-writable `/var/tmp` outside permitted roots | `EACCES`; directory mode `01777` |
| Write `/etc/zetra-forbidden-write` | `EACCES`; ordinary DAC also contributes here |
| Unapproved `urllib` request to `http://example.com` | Explicit permission-denied error before a successful connection |
| Direct TCP connection to `1.1.1.1:443` | Explicit `EACCES` |
| Actual Zetra knowledge reference factory/runtime | Executed inside the supervised sandbox and returned the exact approved answer |

The process ran as UID10001 with `/sandbox` as its working directory. The filesystem probes distinguish policy enforcement from ordinary permissions by using a world-readable file and a world-writable directory outside the allowlist. Supervisor logs independently recorded `policy_dns_ineligible` and `transparent_tcp_policy_denied` for the unapproved endpoints.

The initial HTTP assertion expected HTTP403, but the supported boundary denied the connection earlier with `URLError(PermissionError13)`. The initial result is preserved in `openshell-initial-probe-observation.json`; the assertion was corrected to accept explicit permission denial, while rejecting timeout/DNS failures as evidence of policy enforcement. No permission was broadened to make the tests pass.

## Reproduction and evidence

The chart, Agent Sandbox API, and dedicated kubeconfig must already be installed as described above. With the project Python environment and pinned CLI present:

```bash
.venv/bin/python integrations/openshell-reproduce.py
```

The script copies only the current public runtime/reference files into ignored `.tools/openshell-fixture`, records source hashes, opens its own loopback forwarding session, creates a fresh strict-policy sandbox through the supported CLI, uploads those files, executes the probe, captures effective policy and supervisor outcomes, and requests sandbox deletion. Forwarding is always stopped. A cleanup timeout is recorded explicitly for inspection. `--keep-sandbox` preserves the new sandbox when intentionally needed.

The complete reproduction was run successfully, produced `status: passed` with seven checks, and removed its fresh sandbox. Evidence is in:

- `openshell-live-output/report.json`
- `openshell-live-output/probe.json`
- `openshell-live-output/effective-policy.json`
- `openshell-live-output/supervisor.log`
- `openshell-live-output/source-hashes.json`

The initial manually exercised `zetra-python` and `zetra-strict` sandbox records and logs are also retained for inspection. The manually exercised sandboxes may remain in the disposable cluster until cleanup; all contain only generated test code and data.

## Qualification limits

Production principal authentication, AppArmor protection, user namespace isolation, credential-provider exchange, allowed external L7 routes, policy revocation/hot reload, gateway/sandbox transport composition, resource/tenant authorization, first-instruction races, and privileged-node compromise are not qualified by this fixture. The network tests establish denied egress for the selected Python process and destinations; they do not establish a universal network proof. The offline reference agent tests package execution and runtime behavior without model calls or model-quality evaluation.

Pinned upstream implementation references: [policy compatibility enum](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/crates/openshell-core/src/policy.rs), [upstream policy example](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/examples/sandbox-policy-quickstart/policy.yaml), [CLI mTLS path resolution](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/crates/openshell-cli/src/tls.rs), [Helm values](https://github.com/NVIDIA/OpenShell/blob/v0.1.2/deploy/helm/openshell/values.yaml).
