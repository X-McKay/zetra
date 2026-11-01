# Platform guest runtime

Existing immutable releases use `agent_runtime.step`. The opt-in
`agent_runtime.core_step` adapter uses Zetra's admitted Python/ADK factory,
Definition, Runtime, Dispatcher and operation tape. It does not extract tools into
the legacy transcript loop. Keep existing images and worker builds available for
retained legacy histories.

A core image installs its agent as a Python package and includes `/app/agent.yaml`:

```yaml
schema_version: 2
name: investigator
runtime_profile: zetra-core-v1
definition: investigator:agent.yaml
model:
  backend: llm-main
  name: your-model-name
  max_tokens: 1024
  temperature: 0
tools:
  - name: get_pod
    effect: remote_read
```

The referenced package resource is the same indexed SDK AgentSpec used locally.
The deployed factory must register exactly the declared tool/effect inventory.
The model binding always uses the gateway; an SDK fixture manifest never enables
fixture fallback in production. Registered remote-read tools use `ctx.mcp` with
the platform's authenticated MCP binding.

The runner supplies an exact `AGENT_CORE_BINDING` containing the immutable release
identity, Temporal namespace/Workflow/run, task digest, session and generation.
The guest compares each command with that independent pod configuration before
importing agent code. stdin/stdout use a decimal byte-length line followed by a
JSON object, bounded to 1 MiB. Library and agent stdout/stderr are suppressed through
interpreter exit so logs cannot corrupt control replies or disclose credentials.

`prepare` reconstructs only the supplied accepted Position and returns a captured
operation or completion. `execute` reconstructs and checks the complete pinned
operation, then persists an attempt marker before dispatch. A process-wide file
lease remains held through interpreter exit. Duplicate attempts are unknown and
never select a local candidate as accepted. Receipts are candidates until the
host validates and durably snapshots them; completion is discovered by the next
prepare without counting another effect. Objects live in `state/core/objects`.

Model 403/429 responses produce typed policy/budget refusals without candidates.
Transport loss, malformed provider responses and arbitrary tool exceptions remain
unknown. A refusal inside a tool also remains unknown, with a separate reason,
because earlier tool work may already have happened. No application or connection
retries are enabled for model effects; the existing single 401 refresh is retained.
The client fixes gateway Host, verifies the CA and refuses redirects.

The runtime trusts cooperative registered agent code. Landlock remains defense in
depth; the VM, platform identity and network controls are the production boundary.
Local TLS/subprocess tests do not establish live cluster isolation. The OpenAI pin
is aligned with the shared SDK's required converter dependency; the complete legacy
runtime suite remains part of validation.
