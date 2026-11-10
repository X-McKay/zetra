# Agent authoring and evaluation

## Platform agents

```bash
uvx --from 'git+https://github.com/X-McKay/zetra@<commit>#subdirectory=platform/agent-tools' \
  zetra-agents init my-agent --name my-agent --owner you@example.com
cd my-agent && just setup && just check && just run my-agent
```

`zetra-agents init` renders a repository for the Agent Runtime Platform (`platform/`,
spec sections 6 and 11), locks it and starts its git history, then prints the steps to a
deployed agent. The tooling alone installs in seconds; `zetra init` from a zetra checkout
does the same and additionally carries the local harness. The same layout serves a
repository with one agent and one with many; `X-McKay/zetra-agents` is the reference
repository. `zetra-agents rename OLD NEW` renames an agent across its directory,
package, manifest, Agent, project name, tests and instructions, and re-locks.

```text
my-agent/
├── pyproject.toml, uv.lock          one uv workspace; uv.lock is AgentRelease.spec.lockSha256
├── Dockerfile, .dockerignore        one image recipe for every agent (targets runtime, fixture)
├── justfile                         setup, check, run, render, build, policy-digest, preflight
├── .github/workflows/ci.yml         check, render check, per-agent tests and image build
├── .github/workflows/release.yml    on tags <agent>/v*: calls zetra's agent-release action
├── AGENTS.md, README.md, .gitignore seeded once, then yours
└── agents/my-agent/
    ├── pyproject.toml               this agent's dependencies (a workspace member)
    ├── agent.yaml                   the image manifest (spec 11.7), /app/agent.yaml
    ├── deploy/agent.yaml            the Agent resource (spec 6.1) the security review approves
    ├── src/my_agent/                create_agent(ctx) and instructions/base.md
    ├── tests/                       the agent as its image builds it, and the smoke session
    └── scenarios/smoke.yaml         a scripted session for `just run`
```

The files outside `agents/` that every repository shares are rendered from
`platform/agent-tools` templates and pinned to one zetra commit, which also pins the
sandbox SDK in the images, the tooling and the contracts the Registry Controller runs.
`zetra-agents render --check` (part of `just check` and CI) fails on a hand edit, on a
lock whose zetra entries are not the pinned commit, and on a member that overrides a
source; change the templates in zetra instead. `zetra-agents check` requires
`agent.yaml` and `deploy/agent.yaml` to agree (the model is granted, names match),
compiles each release policy (only `remote_read` grants), refuses network and process
imports and stray files in agent packages, and admits the fixture image target only for
the conformance fixtures it lists.

### Writing an agent

`create_agent(ctx)` receives `ctx.workspace`, `ctx.instructions` and `ctx.mcp`, and
returns `{"tools": [Tool(fn, effect), ...]}` with effects `workspace_read`,
`workspace_write` and `remote_read`; every remote read goes through `ctx.mcp` to a
backend and tool granted in `deploy/agent.yaml`. The Session Runner drives one boundary
per `agent_runtime.step`; the report is `workspace/report.md`, at most 16000 bytes.

A tool is declared once, in the code. `just render` reads the list from `create_agent`
and writes the `tools:` block of `agent.yaml`; `render --check` (in `just check` and CI)
names any difference. Only that block is replaced (comments inside it go with it;
everything outside it, line endings included, is kept), and only when a name or effect
changed, because the manifest's bytes are hashed into every release. `tools:` must be a
plain key at the left margin. The grants in
`deploy/agent.yaml` are not derived: they are the capabilities a security review
approves, so they stay written by a person. The conformance fixtures are not derived
either; their own image build holds them to their manifests.

`agent_runtime.report` is the ending most agents share. `Evidence(ctx.workspace)`
records what a remote read returned as `evidence/<label>-<sha256>.json` and returns the
data with its link; `report_tool(ctx.workspace, evidence)` is a `write_report` tool that
bounds the narrative and appends exactly the evidence links that exist, so a report can
cite only what was observed.

### Testing an agent

`zetra-agents` registers a pytest plugin, so an agent's tests need no setup of their own:

```python
async def test_pods_are_listed(agent):
    agent.mcp.reply("pods_list_in_namespace", {"items": []})
    result = await agent.call("list_pods")
    assert agent.mcp.calls == [("kubernetes", "pods_list_in_namespace", {"namespace": "demo"})]
```

`agent` is the agent built by the SDK's own loader from the layout its image has, so a
manifest that disagrees with the code fails here as it does in the sandbox. `agent.mcp`
stands where the gateway would: it answers only tools `deploy/agent.yaml` grants, and
raises `PolicyDenied` for the rest as the gateway's 403 does. A test fails when the agent
made an ungranted call (the message names the grant line to add), a call with no scripted
reply, or an MCP call from a tool not declared `remote_read`. A test that means to
exercise a refusal sets `agent.mcp.strict = False`; that turns all three checks off for
that test, so it asserts on `agent.mcp.denied`, `unscripted` and `misdeclared` itself.
The fixture reads the grants as `deploy/agent.yaml` writes them and does not compile the
Agent, so a grant the platform refuses (a duplicate backend, an effect other than
`remote_read`) is caught by `zetra-agents check` and `run`, not by a test. The kit lives
in the tooling, a development dependency, so no test double is ever in a signed image.

### Running an agent locally

```bash
just run my-agent                         # scenarios/smoke.yaml
just run my-agent --scenario crashloop    # agents/my-agent/scenarios/crashloop.yaml
just run my-agent --live --task "Why is the checkout pod restarting?"
```

`zetra-agents run` drives one whole session through `agent_runtime.step`, one process
per boundary as the Session Runner does, and prints each boundary, the report path and
the transcript path. The model and MCP are stand-ins described by a scenario:

```yaml
task: Why is the checkout pod restarting?     # text is sent as {request: text}, a mapping as given
mcp:                                          # what each granted MCP tool returns
  - tool: pods_list_in_namespace
    backend: kubernetes                       # optional
    result: {items: []}                       # text, or a document sent as JSON
  - tool: events_list
    error: upstream unavailable               # the call fails instead
model:                                        # scripted turns; ignored with --live
  - call: list_pods
    arguments: {namespace: shop}
  - call: write_report
    arguments: {content: Nothing is restarting.}
  - say: The report is written.
```

With `--live` the model is any OpenAI-compatible endpoint: `ZETRA_AGENTS_MODEL_URL` is
its `/v1` base URL, `ZETRA_AGENTS_MODEL_NAME` the name it serves the model under (the
manifest's name is the platform route's) and `ZETRA_AGENTS_MODEL_API_KEY` its key, if
any. The run exits 0 only when the session completed with a readable `report.md` and
made no MCP call that was ungranted or missing from the scenario; each of those is
printed with the line to add. Runs are kept under `~/.local/state/zetra/agent-runs/`
(`$XDG_STATE_HOME/zetra/agent-runs/` when that is set).
The scaffolded tests run the smoke scenario, so it stays true as the agent changes.

A local run keeps the parts of a platform session that need no platform. Before
anything runs, `deploy/agent.yaml` is held to the schema in the Agent CRD: its types,
ranges, patterns, enums and required fields, so an Agent the cluster would refuse to
store (65 boundaries, a budget of zero, a grant with no `effect`) is refused here with
the field at fault. It is a reading of that schema, not the API server's validator: a
`format` (the owner's email) is not checked, the file is read as YAML 1.1 with the last
of a repeated key winning, and a null or a `3.0` where the schema says integer is
refused here though the cluster would take it. The cluster has the last word.
`zetra-agents check` does not read the schema; today only a local run and the cluster
do.

The MCP grants and the session's token budget then come from the policy bundle the
contracts compile the Agent to, the same compilation a release goes through. The budget
(the smallest `tokensPerSession` among the model grants) is handed to the step as the
pod template hands it, but nothing local enforces it as the platform does: there the
gateway reserves against it before every model call and refuses a call whose reservation
would pass it. Here only the SDK's advisory check runs, after a model turn that called a tool; a
scripted model reports no usage, so a scripted run is never stopped by the budget, and a
`--live` run can end over it. The boundary limit is the Agent's
`spec.runtime.maxBoundaries`, as it is on the platform: the Session Runner reads it from
the release's policy bundle and stops a session at the smaller of that and the limit its
submitter passes (64 unless `just agent run --max-boundaries` says otherwise). The two
differ only in how a request above the Agent's limit is met: `--max-boundaries` over it
is refused here, and there the session is submitted and ends at the Agent's limit. An
agent `zetra-agents check` refuses, or whose Agent
does not compile, is not run. Each boundary is held to the exec deadline and the task
to 16 KiB. After every boundary `workspace/` and `state/` are measured as the tar the
platform snapshots, by GNU tar's arithmetic: a file with several names is stored once
and linked, and a socket and any `state/token.json` are left out, as tar leaves them.
Passing its 64 MiB limit is reported as a problem. Only the size is modelled: a file or
directory the agent leaves unreadable fails the platform's snapshot and is not reported
here.

A step counts only when it exited 0, 2, 3 or 4 and printed what the Session Runner
accepts for that boundary:
at most 64 KiB, exactly the result's six fields, this boundary's number, and `done`
agreeing with the exit code. A step that ends without that, for example because a tool
exited the process, ends the run as `unknown`. A tool shares the step's standard
output, so one that prints the driver's own result and exits is taken at its word, on
the platform exactly as here; the check is fidelity to the runner, not a defence
against the agent's code. The agent's code is laid out read-only, as the image's
`/app` is, and a step starts from an empty directory with a short environment that
leaves out the caller's other variables. An interrupted or terminated run ends its step
and leaves a run directory that can be removed; a process a tool started and left
behind is not tracked and keeps running.

None of that contains the agent's code. A local step is an ordinary process running as
you: it can read and write whatever you can, including your home directory, the run's
own transcript and bookkeeping, and with `--live` the endpoint's key. The read-only
layout and the short environment catch accidents, such as an agent that writes beside
its code or leans on a variable the sandbox will not have; they stop nothing that sets
out to get past them. Run only code you trust, and take a local pass as saying nothing
about code you do not.

A local run shows what the agent does with a task. It is not a deployment check:
identity, the gateway's policy and budget ledger, the sandbox (no Landlock, no network
policy), the signed image and the real MCP servers are exercised only by a session on
the platform (`just agent run` in Kubani). Among the things that can pass locally and
fail there: an import the agent's own package does not declare, because a local step
sees the whole workspace environment (the CI image step loads the agent from its built
image and catches it); a task the Session Runner refuses for carrying something
credential-like; a session that outlives the Agent's `spec.runtime.ttlSeconds`, which a
local run does not model; and anything that depends on what a scripted MCP reply or
model turn left out. The list is not complete, which is why the first session on the
platform is still a step of its own.

A release is a tag `<agent>/v<version>` pushed from merged `main`, one at a time. The
rendered `release.yml` calls `.github/actions/agent-release`, which builds the image with
the agent's target, pushes it by digest, signs and attests it keylessly through Kubani's
private Sigstore as the repository's own workflow identity, verifies, and publishes the
release assets. Kubani registers the repository for a tenant once (`just agent
register`), then stages and approves each release in separate reviewed changes.

## Local harness projects (OpenShell)

Start with `zetra init --harness ./my-agent --name my-agent`, then `cd my-agent` and
`uv sync --locked`. The SDK bundles a generated copy of the canonical
[X-McKay/zetra-template](https://github.com/X-McKay/zetra-template) tree.
The template pins a qualified SDK Git commit, not the newest branch head. Updating
that pin is an explicit dependency upgrade followed by lockfile regeneration and
qualification. Initialization performs no network lookup or dependency rewriting.
Scaffolding requires a qualified bundle. Execution-only SDK revisions reject
`init` before writing files; use the final SDK CLI or canonical template renderer
to create repositories. This keeps image execution pins independent of template
release packaging. The guard checks the bundled metadata against its pinned
project dependency; it is not a runtime compatibility or signature check. A newer
CLI intentionally creates the last qualified template instead of rebinding it to
its own Git revision. Standard wheel dependency metadata retains the exact SDK
Git reference, so installing a built agent outside its checkout uses that pin too.
A wheel does not carry the complete project lock: plain pip installation resolves
transitive dependencies. Use the captured `uv.lock` for the qualified image environment.

Project names cannot be Python keywords, standard-library modules, or reserved
locked dependency distribution/import names. Rendering validates names before
writing, uses UTF-8, and removes a newly created partial destination on failure.

```text
agent-project/
├── README.md, AGENTS.md, pyproject.toml, uv.lock, Justfile, .gitignore
├── .github/workflows/{validate,qualify,release}.yml
├── src/my_agent/
│   ├── __init__.py
│   └── agents/document_reader/
│       ├── __init__.py, agent.yaml, factory.py
│       ├── instructions/base.md
│       └── tools/{__init__,document_read}.py
├── tests/unit/document_reader/
├── tests/packaging/
└── evals/document_reader/cases.jsonl
```

The project index is data, never imported Python:

```toml
[tool.zetra.agents]
document_reader = "src/my_agent/agents/document_reader/agent.yaml"
```

One agent is inferred; multiple agents require `--agent NAME` for `check`, `build`,
`release` and `eval`. `test` validates every indexed manifest and runs the whole
project test suite. Names must match their manifest. Manifest and instruction paths
must remain inside the project and cannot contain symlinks.

Registered tools receive model-supplied arguments only. ADK-injected parameters
(`ToolContext`, including typed aliases and defaulted parameters, and
`input_stream`) are rejected during tool registration, before any model or tool
execution. Close over the supplied `AgentContext` for workspace access instead.

Reviewed read-only integrations may register tools with `effect="remote_read"`.
They use the existing single-attempt tool Activity; accepted responses replay from
Temporal-selected artifacts. This does not admit remote writes or authorize any
endpoint. The deployment must provide the required native OpenShell provider and
enforce the endpoint, credential and remote-server permissions.

```yaml
schema_version: 1
name: document_reader
owner: local-development
entrypoint: my_agent.agents.document_reader.factory:create_agent
instructions: my_agent.agents.document_reader:instructions/base.md
model: fixture
profile: serial-v1
policy: workspace
```

The factory returns an ADK LlmAgent using `ctx.model`, `ctx.instruction` and
`ctx.tools(Tool(function, effect), ...)`. The instruction resource is loaded from
the installed package inside the sandbox and passed as static text; it is not a
callback, skill loader or permission grant. Hatchling includes the packaged YAML
and Markdown. CI compares those resources byte-for-byte inside the built wheel.
Tools must be async, nonvariadic and callable with named arguments. Supported
effects are `workspace_read` and `workspace_write`; the latter is confined session
data, not permission for external business-system writes.

`agent.yaml` owns identity, factory/resource references and reviewed runtime
profile. Python owns typed behavior. Add `models.py`, `dependencies.py`, `toolset.py`,
shared tools or application settings only when the application needs them.
Development skills belong under `.agents/skills`; runtime skills are ordinary
packaged resources only if explicitly consumed by the agent. There is no automatic
skill execution or dynamic profile expansion. Custom Temporal workflows, platform
adapters, observability and deployment files are optional application additions;
the starter copies none of the SDK's runtime machinery.

### Locked images and evidence

`zetra build . --tag my-agent:local --kind` snapshots only `pyproject.toml`,
`uv.lock`, optional root README/LICENSE and nonhidden `src/**` package files
(excluding caches and egg metadata). The same file selection determines the source
hash. Tests, evals, `.agents`, `.github`, artifacts, local state and secrets outside
`src` are not copied into images. Do not put credentials in package source.
Other install inputs or local dependency overlays are unsupported. The Docker
recipe installs that snapshot with `uv sync --locked --no-dev --no-editable`.
There is no injected SDK checkout or second constraints environment. The lock must
pin Zetra to a full HTTPS Git commit. Missing or stale locks fail the build.

The release schema is version 2. This experimental SDK does not migrate earlier
release/history formats; use a new runtime identity and queue for this format.

A release binds image digest, selected manifest, source hash, lockfile hash and
SDK Git revision. Docker labels are consistency checks, not signatures or proof
against a malicious image builder. Dependency locking does not make mutable base
images, OS repositories or Python build-system dependencies bit reproducible.
Eval reports additionally bind the selected agent and dataset hash; generated
evidence cannot change the source identity it describes. These reports remain
local and unsigned.

`validate` runs static checks, unit/fixture cases and wheel-resource tests.
`qualify` is explicitly invoked on a configured dedicated kind runner and executes
sandbox cases. `release` builds a wheel plus checksums; it does not attest,
promote, publish or deploy infrastructure. Add those platform steps only with a
real consumer and explicit authorization policy.

Factories perform no I/O and return the same registration on reconstruction.
Arbitrary callbacks, dynamic instructions, subagents, streaming, parallel tool
calls and unsupported RunConfig/App settings fail admission. Python checks rely
on cooperative code; see SECURITY.md for the independent sandbox boundary.

The manifest selects the fixture model/workspace policy or the pinned vLLM model
and vllm-chat policy. These are qualified policy profiles, not an open-ended
policy compiler. Their filesystem and network rules are compiled by the SDK and
bound into each release. Custom permissions require explicit implementation and
negative-path qualification. `zetra profile` only produces normalized review
candidates; denied observations never become permissions.

`zetra check` validates data without importing the factory. Runtime admission
occurs inside the sandbox. `zetra test` runs trusted package tests. Fixture eval
runs deterministic model/tool cases and identifies that limited scope. Sandbox
eval executes through Temporal/OpenShell and requires a release matching the
current source. A passing fixture cannot stand in for a live model evaluation.

Each JSONL eval case has `id`, `input`, `contains` and `files`. IDs are unique;
files are workspace-relative paths. This minimal evaluator checks final text and
expected output existence. It does not infer semantic quality, grounding or
prompt-injection resistance. Add explicit cases and assertions for those needs.

`examples/workspace_reporter` lists files and writes a summary.
`examples/document_reader` exposes one approved document ID and rejects other
IDs. Both use the same SDK runtime with distinct factories and registries.

After a successful session and confirmed deletion, `await client.read_files(handle)`
returns workspace-relative paths and bytes from the validated accepted snapshot.
It reads from the caller-supplied artifact store after checking the exact Temporal
execution result. Retain the handle, history and artifacts until outputs and any
required recovery or cleanup are complete. Agent packages do not implement the
worker, transport adapter or lifecycle scheduler.
