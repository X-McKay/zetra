# agent-tools

`zetra-agents`: what every agent repository shares, as one pinned package.

An agent repository, whether it holds one agent or thirteen, is laid out the same way:
`agents/<name>/` directories that are members of one uv workspace at the repository root,
one `uv.lock` that is the image lock (`AgentRelease.spec.lockSha256`), and root files
rendered from the templates here (`Dockerfile`, `.dockerignore`, `justfile`, root
`pyproject.toml`, CI, the release shim). The repository pins one zetra commit for the
sandbox SDK in its images, this tooling and the contracts the Registry Controller runs.

| Command | Does |
| --- | --- |
| `zetra-agents init DIR --agent NAME --owner EMAIL` | render a new repository holding one agent |
| `zetra-agents check [AGENT...]` | repository layout, agent layout (spec 11.7), network-import ban (14.1), fixture gate |
| `zetra-agents render [--check]` | write the rendered root files and each `agent.yaml` `tools:` list from the code, or verify both |
| `zetra-agents run AGENT [--scenario S] [--task T] [--live]` | one whole session on this machine: the SDK's boundary driver, a scripted or local model, scripted MCP behind the Agent's grants |
| `zetra-agents image-target AGENT` | `runtime`, or `fixture` for the conformance fixtures in `layout.FIXTURES` |
| `zetra-agents preflight AGENT --tag T --kubani PATH` | offline source/candidate compatibility against a Kubani checkout |
| `zetra-agents release AGENT --image DIGEST --revision SHA --out DIR` | the Agent, unapproved AgentRelease, policy bundle and release predicate |
| `zetra-agents predicate eval|vuln|provenance` | one attestation predicate for cosign |

`zetra_agents.testing` and the pytest plugin this package registers give an agent's tests
the `agent` fixture: the agent built by the SDK from the image layout, with a scripted
MCP that answers only what `deploy/agent.yaml` grants. None of it is in an image; the
package is a development dependency of an agent repository. See `docs/agents.md`.

`.github/actions/agent-release` is the composite action a repository's rendered
`release.yml` calls on an `<agent>/v<version>` tag: build with the agent's image target,
push by digest, sign and attest keylessly through Kubani's private Sigstore (trust roots
ship with the action), verify, publish the release assets. It is a composite action and
not a reusable workflow so that the certificate subject stays the calling repository's
workflow on the tag, the shape every tenant pin matches.

The conformance fixtures in `layout.FIXTURES` are the only agents built with the
Dockerfile's `fixture` target, which replaces the installed SDK boundary driver with the
agent package's `step.py` and runs the agent's `verify_image.py`. `check` refuses those
files on any other agent.

## One file instead of two

An agent whose `agent.yaml` has a `requires` block is on the one-file layout: the author edits
`agent.yaml` and `deploy/agent.yaml` is generated from it and committed.

```yaml
model: {backend: llm-main, name: main, max_tokens: 1024, temperature: 0}   # name is an alias
requires:
  owner: al
  description: Read-only PostgreSQL administrator.
  tenant: tenant-test
  identity: {mode: agent}
  budget: {tokensPerDay: 1000000, tokensPerSession: 200000}
  tools: [{backend: postgres-admin, effect: remote_read, tools: [execute_sql], delegation: inherit}]
  runtime: {cpu: "1", memory: 2Gi, ttlSeconds: 900}
```

`zetra-agents render` writes the `tools:` list and `deploy/agent.yaml`; `check` fails when
either is out of date. On this layout `model.name` must be an alias of `model.backend`:
`main` is `llm-main` and `fast` is `llm-fast`, or the table under
`[tool.zetra-agents.aliases]` in the root `pyproject.toml`. An optional
`evaluation/suite.yaml` is checked by `check`. Every release carries the Agent's spec as
`spec.declaration`. See `examples/declarations/valid/one-file-agent`.
