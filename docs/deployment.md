# Local deployment harness

The disposable lab uses `scripts/kind-up.sh`, targeting only
`kind-zetra-validation` with `.state/kubeconfig`. It installs Cilium, Agent Sandbox,
OpenShell mTLS, PostgreSQL and Temporal. The worker process and example artifact
backing run on the host; inference runs separately. No script changes the global
Kubernetes context. Production service deployment is outside this SDK.

Use the repository's locked Python environment and pinned infrastructure files.
Image publication and production provenance require separate qualification; a
local immutable image is not a signed supply-chain policy.

```bash
nix develop
just bootstrap
just cluster-up
uv run zetra build examples/workspace_reporter --tag zetra-reporter:local --kind \
  --output .state/reporter-release.json
```

Create explicit forwards in separate terminals:

```bash
kubectl --kubeconfig .state/kubeconfig --context kind-zetra-validation \
  -n temporal port-forward service/temporal-frontend 7233:7233
kubectl --kubeconfig .state/kubeconfig --context kind-zetra-validation \
  -n openshell port-forward service/openshell 8080:8080
```

Export connection settings in the worker terminal:

```bash
export TEMPORAL_ADDRESS=127.0.0.1:7233
export OPENSHELL_ADDRESS=127.0.0.1:8080
export OPENSHELL_TLS_DIR="$PWD/.state/tls"
```

Create the worker secret once, with restrictive file permissions. Retain it across
worker replacements; rerunning this creation command refuses to overwrite it.

```bash
uv run python - <<'PY'
import os
import secrets
from pathlib import Path

path = Path('.state/worker.key')
path.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as stream:
    stream.write(secrets.token_bytes(32))
PY
uv run zetra --artifacts .state/objects --task-queue zetra --runtime-id local \
  worker --key-file .state/worker.key --release .state/reporter-release.json
```

In another terminal with the same Temporal settings, submit work:

```bash
uv run zetra --artifacts .state/objects --task-queue zetra --runtime-id local \
  run .state/reporter-release.json --session report-001 \
  --input 'Inspect the workspace and write a summary.' --output .state/report-export
uv run zetra --artifacts .state/objects --task-queue zetra --runtime-id local \
  status .state/report-export/handle.json
```

`run` emits the exact handle to stderr and saves it before waiting. An interrupted
client can repeat the same release, session ID, input and workspace; native
submission checks the original request. Temporal and artifacts remain the
recovery authority. Output directories must be empty or existing Zetra exports,
with no overlap with input workspaces or artifact backing. A static ownership
marker only authorizes replacing local exports; it carries no progress state.
Use a caller-controlled local directory (the CLI does not defend against hostile
concurrent filesystem mutation). Each accepted submission replaces stale export
files. `workspace/` contains verified agent files; `result.json` is published last
after successful export. Failed or uncertain runs publish their result without a
workspace export and return nonzero. Stdout contains only a compact summary.
`cancel` accepts the same handle file. The submitting CLI is not a worker;
stopping it does not stop the independently running worker.

The worker supports repeatable `--release` allowlist files and `--provider` native
OpenShell provider names. Omit releases only for an intentionally unrestricted
trusted queue. SIGTERM/SIGINT request native Temporal shutdown; the default
`--graceful-shutdown-seconds 20` bounds Activity drain time before cancellation.
Allow additional process termination time for shutdown. These flags forward
existing Python SDK settings; credential administration remains deployment-owned.

For embedded use, pass a caller-configured Temporal connection to `SessionClient`
and `create_worker`; configure TLS, authentication and codecs there. The CLI
worker and client are local-lab entrypoints with no Temporal TLS configuration.
Use the Python SDK with a caller-configured authenticated/TLS client for remote
services; do not expose the CLI connection over an untrusted network. Callers also own gateway identity,
secret management, artifact durability and resource lifetime. Workers polling one
queue must share a stable `runtime_id`, gateway, artifact backing and key; assign
different configurations separate queues. `FileStore` requires a shared durable
filesystem when used across workers. Custom `ArtifactStore` implementations expose
blocking thread-safe methods and verify immutable bytes against `Ref`.

Retain history, artifacts and the original worker key/configuration for execution
recovery and cleanup. Key rotation is not versioned. Hard-terminal cleanup is an
explicit durable reconciliation operation; wait for its result. Failed normal
cleanup can also be reconciled. Both cleanup paths retry transient failures for
up to ten minutes with backoff capped at 30 seconds. Explicit gateway authorization
or invalid-operation failures stop promptly; correct the cause before reconciling.
These retries do not repeat agent effects. A successfully completed cleanup Workflow means `DELETED`. Unconfirmed cleanup
fails without automatic Workflow retry. Explicit reconciliation may start another
failed attempt, but Temporal refuses to replace a completed cleanup Workflow;
concurrent or later callers reuse its verified proof within history retention.
Status does not perform cleanup. Provisioning
uncertainty can leave `NOT_CONFIRMED` even after observed deletion, pending a future
gateway-side lifecycle fence or platform reconciliation. No janitor is implemented.
Native create request IDs use the caller's existing gateway identity; retain that
identity during recovery. OpenShell 0.1.4 retains successful admissions for 24 hours
and caps them at 1,000 per caller, including creates whose sandboxes were deleted.
Unresolved admissions do not expire. See [native admission](architecture.md#native-create-admission)
for the bounded retry contract and private SDK compatibility seam.

## Qualification

```bash
just check
uv run python scripts/build_qualification.py
BRIDGE_LIVE_VLLM=1 uv run zetra qualify
BRIDGE_LATENCY=1 uv run zetra qualify --suite latency
BRIDGE_PROVISIONING=1 BRIDGE_PROVISION_BASELINE_IMAGE=zetra-validation:source \
  uv run zetra qualify --suite provisioning
```

These opt-in commands exercise the local harness. Synthetic prompts and workspace
files are used. Benchmark JSON remains in `.state`; see
[qualification](qualification.md) for recorded scope, failures and results. No
command listing is evidence that it has passed in the current revision.

To remove this disposable installation explicitly:

```bash
kind delete cluster --name zetra-validation
```

This also removes Temporal/PostgreSQL volumes. Remove local `.state` only when its
retained artifacts, keys and certificates are no longer needed.

## Experimental rollout boundary

Use fresh execution IDs, a new runtime identity and a separate task queue for
this cleanup contract. Do not migrate retained sessions from older revisions:
older cleanup Workflows could complete with `NOT_CONFIRMED`, whereas this revision
reserves completion for `DELETED`. Historical execution migration is unsupported.
Retain the original worker/configuration for any old execution requiring cleanup.

## Retained release references

The current PRs pin commits from their feature branches. Merge Zetra #4, then #5,
then zetra-template #1 using merge commits, retaining those ancestors. Do not
squash or rebase these pinned revisions away. If repository policy requires squash
merges, first replace the pins with retained release commits and regenerate locks,
template provenance and qualification before distributing projects. Deleting a
branch after a merge commit does not remove the retained ancestors. No merge or
repository policy change is performed by SDK commands.
