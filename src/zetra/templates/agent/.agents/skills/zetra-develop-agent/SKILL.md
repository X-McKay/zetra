---
name: zetra-develop-agent
description: Use when creating or changing an agent's Python factory, prompt, or component wiring in a Zetra agent repository.
---

Read [the repository workflow](../../../docs/development.md) for current commands
and runtime limits. Locate the selected agent through `pyproject.toml`, then read
its manifest, factory, instructions and nearest tests.

Define the requested observable behavior and the smallest relevant acceptance
cases. Extend the starter's ordinary Python and current SDK contract. Reuse
`ctx.model`, `ctx.tools` and `ctx.instruction`; keep instructions and manifests in
the wheel. Inspect existing component patterns before introducing abstractions.

For example, adding a second approved document requires the tool's identifier
mapping, prompt guidance and positive/rejection tests to agree. A new fixture input
alone does not prove that the model selected the new identifier.

Run the relevant local tests and static checks, then the repository validation
command. Report changed behavior, commands/results and remaining evidence gaps.
Use the task-specific tool, runtime-skill or evaluation guidance when that part of
the task needs it. Do not start an unrelated platform migration or deploy an agent
as a side effect of authoring it.
