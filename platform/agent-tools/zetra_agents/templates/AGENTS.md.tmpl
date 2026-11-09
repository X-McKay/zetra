# Working in this repository

- One agent per `agents/<name>/`, a member of the root uv workspace; run `just check`
  after changes. `just setup` installs everything.
- Keep `agent.yaml`, `create_agent` and `deploy/agent.yaml` in agreement: every tool in the
  code is in the manifest with the same effect, every MCP tool it calls is granted.
- Agent code reaches the outside only through `ctx.mcp`. Never import network or process
  modules in an agent package; `zetra-agents check` rejects them.
- `Dockerfile`, `.dockerignore`, `justfile`, the root `pyproject.toml` and the workflows
  are rendered from zetra (`zetra-agents render`); CI fails if they are edited by hand.
  Change the templates in zetra `platform/agent-tools` instead.
- Nothing here is applied to a cluster. Kubani owns the applied Agent and AgentRelease, and
  approval is a reviewed Kubani change; never produce an approved release here.
- Release by pushing a tag `<agent>/v<version>` from merged main, one at a time.
- Never commit signing keys, registry credentials, `dist/`, or session output.
