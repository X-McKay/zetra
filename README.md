# Zetra

Zetra is an experimental Python SDK for durable Google ADK agents in disposable
OpenShell sandboxes. Temporal history owns accepted progress at every model/tool
boundary. Callers supply a Temporal connection, OpenShell client, immutable artifact
store and persistent worker secret. Independently running workers can replace one
another without client-local recovery state.

## Develop an agent

```bash
nix develop
just bootstrap
uv run zetra init /tmp/my_agent --name my_agent
uv run zetra check /tmp/my_agent
uv run zetra test /tmp/my_agent
uv run zetra eval /tmp/my_agent --mode fixture
```

Fixture tests execute trusted developer code locally. They do not establish model
quality or sandbox enforcement. Agent packages define factories and tools:

```python
from google.adk.agents import LlmAgent
from zetra import AgentContext, Tool
from .tools.document_read import read_document


def create_agent(ctx: AgentContext) -> LlmAgent:
    return LlmAgent(
        name="reader", instruction=ctx.instruction,
        model=ctx.model, tools=ctx.tools(Tool(read_document, "workspace_read")),
    )
```

The same registration produces capture wrappers and effect executors. The admitted
profile is static, serial and nonstreaming, with at most 64 model/tool boundaries.
Each effect has one Temporal Activity attempt. Lost completion remains unknown.

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
waiting and returns handle/result JSON on stdout; status and cancel take a handle
JSON file. The local harness is separate from production deployment.

See [architecture](docs/architecture.md), [agent authoring](docs/agents.md),
[security boundaries](SECURITY.md), [qualification](docs/qualification.md) and
[Playbook conformance](docs/playbook-conformance.md). Qualification describes only
the observations actually recorded for the tested environment.
