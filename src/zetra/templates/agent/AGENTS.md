# Agent development

Keep agent definitions indexed in pyproject.toml. Use a standard ADK LlmAgent with ctx.model, ctx.tools, and ctx.instruction. Classify every tool effect explicitly. Keep package instructions and manifests in the wheel. Run just validate after changes. Fixture evaluation runs trusted host code and cannot qualify sandbox isolation.

Use the shared workflow in `docs/development.md` for supported commands and evidence
limits. The repository includes task-specific Zetra developer skills under
`.agents/skills` and `.claude/skills`; load the relevant skill for agent authoring,
tools, runtime skills, evaluation, debugging, optimization or release review.
They guide development and do not grant runtime permissions or enforce CI policy.
