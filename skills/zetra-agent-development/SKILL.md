---
name: zetra-agent-development
description: Build or change a Zetra agent package, its typed factory, manifest and executable evaluation scenarios using the repository's development contract. Use for Zetra implementation work, not generic AI advice.
---

# Zetra agent development

Read the repository's `CONTRIBUTING.md`, `justfile`, `schemas/agent-v1alpha1.schema.json` and the relevant example before changing a package. Use `docs/developer.html` for the target standard; distinguish it from the implemented v0.1 contract in `src/zetra/manifest.py`.

Construct the agent with injected typed dependencies. Keep imports/construction free of external I/O and keep credentials out of instructions, source, Temporal history and telemetry. Register logical capabilities; adding a name to the manifest does not implement an independent enforcement boundary.

Choose stable material-scenario IDs and bind them to tests that execute. Keep authorization, approval, isolation, schema and idempotency assertions deterministic. An LLM judge can assess quality but cannot stand in for those gates. Cover the denied path and retries when adding a side effect.

Use the pinned development environment and `just` recipes. Run static `check`, execute the affected `eval` suites, run the toolkit regression suite, and run `ruff`/`ty`. A source, dependency, instruction or evaluator change requires fresh evidence. Do not edit evidence JSON to make a release pass.

`eval` and `run` execute project code; use trusted source or an explicitly qualified sandbox. Local capability/STOP/budget checks are cooperative. The renderer's zero replicas and unsigned candidate status remain until independent qualification; do not present them as production security.

Update the editable chapter/source if a contract changes, rebuild HTML and run document QA. Report exactly which behavior and failure paths were tested, with residual gaps and affected artifacts.
