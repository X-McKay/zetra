# agent-package

A small Zetra application using a standard Google ADK `LlmAgent`. The document_reader factory registers one asynchronous tool through the SDK context. The tool only accepts the stable identifier `guide`; instructions and the agent manifest ship inside the Python wheel.

Use Python 3.12, uv, and optionally just:

```sh
uv sync --locked
just validate
```

The default `fixture` model deterministically invokes the tool and returns `Completed.`. Fixture evaluation executes trusted code on the host. It proves neither model quality nor sandbox isolation. Replace the model only with a provider supported by your pinned SDK and configure its infrastructure explicitly.

`zetra check .` validates static metadata. `zetra eval . --mode fixture` checks the versioned dataset under evals/document_reader. Tests cover approved document access and package resources. `uv build --wheel` builds the wheel. Validation then checks that its YAML and Markdown resources exactly match the source bytes.

Durable qualification requires an explicitly configured self-hosted runner labelled `zetra-kind`, the dedicated zetra-validation kind cluster, Docker, Temporal, OpenShell credentials, and a running compatible worker. Set the repository variable ZETRA_QUALIFICATION_ENABLED to true only after configuring this infrastructure. The manually dispatched qualify workflow builds and loads a source image, binds a release to that source, and evaluates it in the sandbox. Missing infrastructure fails the run. Review SDK documentation before provisioning; this project does not provision clusters.

The release workflow produces a Python wheel and SHA-256 checksum artifact. It does not publish an image, promote a release, or issue an attestation. SDK runtime image publication remains an explicit operator step.
