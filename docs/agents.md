# Agent authoring and evaluation

An agent package contains `agent.yaml`, a Python project, `src/<package>/agent.py`,
`tools.py`, tests and evaluation cases. The factory returns an ADK LlmAgent using
`ctx.model` and `ctx.tools(Tool(function, effect), ...)`. Tools must be async,
nonvariadic and callable with named arguments. Supported effects are
`workspace_read` and `workspace_write`; the latter is confined session data, not
permission for external business-system writes.

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
