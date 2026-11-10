# Working in this repository

- One agent per `agents/<name>/`, a member of the root uv workspace; run `just check`
  after changes. `just setup` installs everything.
- Declare a tool once, in `create_agent`, as `Tool(fn, effect)`. `just render` writes the
  `tools:` list of `agent.yaml` from the code and `just check` fails when they differ;
  do not edit that list by hand.
- Every MCP tool the code calls through `ctx.mcp` must be granted in `deploy/agent.yaml`.
  That file is hand-written on purpose: it is what the security review approves. A test
  or a local run that makes an ungranted call fails and names the line to add.
- Tests use the `agent` fixture: the agent as its image builds it, with `agent.mcp`
  standing in for the gateway. `agent.mcp.reply(tool, result)` scripts a granted tool,
  `await agent.call(tool, ...)` runs one, `agent.mcp.calls` is what was called.
- `just run <agent>` runs one whole session on this machine from
  `agents/<agent>/scenarios/smoke.yaml`: scripted model turns and MCP replies through the
  real boundary driver. It prints each boundary and where the report and transcript are.
  `--scenario NAME`, `--task TEXT` and `--live` (a real model) vary it. It shows what the
  agent does; it is not a deployment check.
- End with a report: `agent_runtime.report.report_tool(ctx.workspace, evidence)` writes
  `report.md`, and `Evidence(ctx.workspace).record(label, data)` keeps what a remote read
  returned so the report links exactly what was observed.
- Agent code reaches the outside only through `ctx.mcp`. Never import network or process
  modules in an agent package; `zetra-agents check` rejects them.
- `Dockerfile`, `.dockerignore`, `justfile`, the root `pyproject.toml` and the workflows
  are rendered from zetra (`zetra-agents render`); CI fails if they are edited by hand.
  Change the templates in zetra `platform/agent-tools` instead.
- Nothing here is applied to a cluster. Kubani owns the applied Agent and AgentRelease, and
  approval is a reviewed Kubani change; never produce an approved release here.
- Release by pushing a tag `<agent>/v<version>` from merged main, one at a time.
- Never commit signing keys, registry credentials, `dist/`, or session output.
