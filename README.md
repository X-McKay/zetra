# Zetra

Zetra is an experimental Python SDK for durable Google ADK agents in disposable
OpenShell sandboxes. Temporal history owns accepted progress at every model/tool
boundary. Callers supply a Temporal connection, OpenShell client, immutable artifact
store and persistent worker secret. Independently running workers can replace one
another without client-local recovery state.

## Develop an agent

```bash
uvx --from 'git+https://github.com/X-McKay/zetra@<commit>#subdirectory=platform/agent-tools' \
  zetra-agents init my-agent --name my-agent --owner you@example.com
cd my-agent && just setup && just check && just run my-agent
```

`zetra-agents init` (or `zetra init` from a checkout) renders an agent repository for the
Agent Runtime Platform, locks it and starts its git history: `agents/<name>/`
members of one uv workspace, one `uv.lock` that is the image lock, and a Dockerfile, CI,
justfile and release shim rendered from `platform/agent-tools` at one pinned zetra commit.
A repository holds one agent or many with the same layout. Agents reach the outside only
through `ctx.mcp`; tools are declared once in the code, tests get the agent as its image
builds it with a scripted MCP, and `just run` drives a whole session locally from a
scenario. A release is a tag `<agent>/v<version>`, signed keylessly by the
repository's workflow, that Kubani stages and approves. See
[agent authoring](docs/agents.md) and [the platform](docs/platform.md).

```python
from agent_runtime.tools import Tool


def create_agent(ctx) -> dict:
    async def write_report(content: str) -> dict:
        """Write the report; at most 150 words of plain text."""
        (ctx.workspace / "report.md").write_text(content.rstrip() + "\n", encoding="utf-8")
        return {"path": "report.md"}

    return {"tools": [Tool(write_report, "workspace_write")]}
```

`zetra init --harness` renders the OpenShell local-harness project (a Google ADK
`LlmAgent`, fixture evaluation, `zetra build`); that path is described below and in
[agent authoring](docs/agents.md).

## Embed the SDK

```python
from zetra.artifacts import ArtifactStore, FileStore, Ref
from zetra.client import SessionClient
from zetra.worker import create_worker

# temporal and gateway are caller-configured clients; worker_key comes from
# the caller's secret manager and contains at least 32 persistent random bytes.
artifacts = FileStore("/durable/zetra/objects")
worker = create_worker(
    temporal, task_queue="agents", runtime_id="agents-v1",
    sandbox_client=gateway, artifacts=artifacts, key=worker_key,
    approved_releases={release.identity},
)
client = SessionClient(
    temporal, task_queue="agents", runtime_id="agents-v1", artifacts=artifacts,
)
# In the worker service: await worker.run()
# In a submitting process:
handle = await client.submit(release, session="report-001", message="Inspect the workspace.")
result = await client.wait(handle)
```

The returned worker is an ordinary Temporal `Worker`. Configure Temporal TLS,
authentication, namespace and payload codecs on the supplied connection. The caller
owns connections and their lifetime. All workers on a queue must share its stable
`runtime_id`, gateway identity, artifact backing and derivation key; different
configurations require separate queues. Artifact methods are blocking and must be
thread-safe, immutable and verify bytes against `Ref`. `FileStore` is a local
implementation; shared workers need access to the same durable backing.

Persist the exact `SessionHandle` for inspection, cancellation or reconciliation.
It pins namespace, Workflow ID, run ID and runtime identity. Reset, continuation,
cron and Workflow retry lineages are unsupported. Temporal history and artifacts,
with the original worker key/configuration, support worker replacement.

## Local harness

The disposable kind lab and CLI connection settings are described in
[deployment](docs/deployment.md). Run a separate `zetra worker --key-file ...`
process before submitting CLI work. `zetra run` emits its handle to stderr before
waiting and returns a compact summary on stdout. `run --output DIR` saves the
exact handle, full result and verified workspace in an SDK-owned export directory.
Status and cancel take the handle JSON file. Worker flags `--release` (repeatable)
and `--provider` (repeatable) forward release approval and native provider names;
SIGTERM/SIGINT drain through Temporal with configurable graceful shutdown. The
local harness is separate from production deployment.

See [architecture](docs/architecture.md), [agent authoring](docs/agents.md),
[security boundaries](SECURITY.md), [qualification](docs/qualification.md) and
[Playbook conformance](docs/playbook-conformance.md). Qualification describes only
the observations actually recorded for the tested environment.
