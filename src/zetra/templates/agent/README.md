# agent-package

A small Zetra application using a standard Google ADK `LlmAgent`. The document_reader factory registers one asynchronous tool through the SDK context. The tool only accepts the stable identifier `guide`; instructions and the agent manifest ship inside the Python wheel.

Use Python 3.12, uv, and optionally just:

```sh
uv sync --locked
just validate
```

The default `fixture` model deterministically invokes the tool and returns `Completed.`. Fixture evaluation executes trusted code on the host. It proves neither model quality nor sandbox isolation. Replace the model only with a provider supported by your pinned SDK and configure its infrastructure explicitly.

`zetra check .` validates static metadata. `zetra quality .` executes the mapped tool tests and V2 fixture assertions under `evals/document_reader` before the wheel is built. The active contract checks final text, the approved tool call and a three-boundary budget. Tests also cover package resources. Validation checks that the built wheel’s YAML and Markdown resources exactly match the source bytes.

`zetra dev` and `zetra eval . --mode local` support explicit local model inference without Temporal. Supply a reviewed endpoint/model through `--model-config`; the fixture gate does not measure live summary accuracy. The SDK admits reviewed `remote_read` tools, but an MCP transport, identity, resource scope and deployment policy still need explicit integration. See [the development workflow](docs/development.md) for commands and evidence limits.

Durable qualification requires an explicitly configured self-hosted runner labelled `zetra-kind`, the dedicated zetra-validation kind cluster, Docker, Temporal, OpenShell credentials, and a running compatible worker. Set the repository variable ZETRA_QUALIFICATION_ENABLED to true only after configuring this infrastructure. The manually dispatched qualify workflow builds and loads a source image, binds a release to that source, and evaluates it in the sandbox. Missing infrastructure fails the run. Review SDK documentation before provisioning; this project does not provision clusters.

The release workflow produces a Python wheel and SHA-256 checksum artifact. It does not publish an image, promote a release, or issue an attestation. SDK runtime image publication remains an explicit operator step.

Claude Code and Codex receive the same versioned developer skill pack with this
repository. See [the development workflow](docs/development.md) for authoring tools
and runtime skills, evaluation, debugging, optimization, release review and pack
updates. `just validate` checks that both assistant copies match the inventory.
Developer skills are excluded from the application wheel.

[The quality contract reference](docs/quality-upgrade/README.md) explains the active
requirements and the upgrade history. Optional Linux local Temporal runs are
available through `zetra dev . --temporal`; see the development workflow for
private state and trusted host limits.
