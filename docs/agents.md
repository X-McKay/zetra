# Agent authoring and evaluation

Start with `zetra init ./my-agent --name my-agent`, then `cd my-agent` and
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
