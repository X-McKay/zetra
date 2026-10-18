# Zetra

Zetra is an experimental Python SDK for durable Google ADK agents in disposable
OpenShell sandboxes. Temporal owns accepted progress at every model/tool boundary.
Each session gets a fresh process and workspace; successful results require
confirmed sandbox deletion. Agent repositories define agents and tools, not workers,
Activities or recovery adapters.

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
quality or sandbox enforcement. The SDK exports `AgentContext` and `Tool`:

```python
from google.adk.agents import LlmAgent
from zetra import AgentContext, Tool
from .tools import read_document


def create_agent(ctx: AgentContext) -> LlmAgent:
    return LlmAgent(
        name="reader", instruction="Read the approved guide and answer from it.",
        model=ctx.model, tools=ctx.tools(Tool(read_document, "workspace_read")),
    )
```

The same registration produces capture wrappers and effect executors. The SDK
rejects unsupported ADK features, callbacks and mismatched registries. The supported
profile is static, serial and nonstreaming, with at most 64 model/tool boundaries.
A lost effect completion remains unknown; it does not trigger automatic redispatch.

## Run in kind

Prerequisites: Docker with a Linux kernel supporting mandatory Landlock and
seccomp, kind, kubectl, Helm, uv and Python 3.12. Allow sufficient Docker VM memory
for Temporal, PostgreSQL, OpenShell and Cilium (8 GiB for this small lab).

```bash
just cluster-up
uv run zetra build examples/workspace_reporter --tag zetra-reporter:local --kind \
  --output .state/reporter-release.json
```

`--kind` loads and binds an immutable image in the dedicated `zetra-validation`
cluster. Builds without `--kind` return a local image candidate when no registry
digest exists; external publication is explicit. The shared recipe installs the
agent package under SDK dependency constraints and compiles trusted bytecode.

Set `TEMPORAL_ADDRESS` and `OPENSHELL_ADDRESS` to explicit loopback forwards, and
`OPENSHELL_TLS_DIR` to `.state/tls`. The test runner creates temporary forwards for
qualification; interactive runs use caller-managed stable forwards for resumption.

```bash
uv run zetra --state .state/sessions run .state/reporter-release.json \
  --session report-001 --input 'Inspect the workspace and write a summary.'
uv run zetra --state .state/sessions status report-001
```

Reuse the same session ID, state directory, release, input, Temporal endpoint
and OpenShell gateway identity to resume after a worker failure. Source, model or policy changes require a new
release/session. The state directory contains private authentication material and
data-only checkpoints; it must persist independently of the disposable sandbox.

## Tests and evidence

```bash
just check
uv run python scripts/build_qualification.py
BRIDGE_LIVE_VLLM=1 just qualify
BRIDGE_LATENCY=1 just latency
```

The vLLM profile uses `Qwen3.6-35B-A3B-NVFP4` at `https://llm.almckay.io/v1` through
ADK's official LiteLlm connector. Its OpenShell policy permits only Python making
POST requests to that chat-completions route. Inference is hosted separately from
kind. Both HTTP retry layers are disabled.

See [architecture](docs/architecture.md), [agent authoring](docs/agents.md),
[deployment](docs/deployment.md), [qualification](docs/qualification.md) and
[security boundaries](SECURITY.md). [Playbook conformance](docs/playbook-conformance.md)
separates implemented behavior from production gates.
