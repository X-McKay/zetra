---
name: zetra-author-skill
description: Use when creating or changing a runtime skill consumed by a deployed Zetra agent, including loading, packaging, and behavioral evaluation.
---

Read [runtime and developer skill distinctions](../../../docs/development.md).
Inspect how this agent actually loads instructions/resources. The current starter
does not automatically discover runtime SKILL.md files or provide a skill registry.

Define the skill's purpose, activation/nonactivation conditions, required tools,
inputs, outputs and allowed effects. Keep task-specific guidance concise. Add the
resource inside the agent package and wire its consumer explicitly using the
current SDK. A file under `.agents/skills` or `.claude/skills` guides the developer's
assistant; it does not ship a runtime capability.

For an incident-investigation skill, exercise a relevant incident, an unrelated
request, missing evidence, untrusted instructions in tool output, and a required
evidence-before-conclusion ordering. Check actual artifacts/outcomes and partial
ordering, allowing alternative valid tool sequences. Test resource loading from
the built wheel as well as source.

If the claim is improved model capability, compare the same cases with/without
the runtime skill and repeat trials. If it encodes an organizational procedure,
evaluate adherence to that procedure. Identify model-based activation/quality
evidence unavailable in fixture mode; never substitute a prose checklist for it.
