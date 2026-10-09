# Draft Codex and Claude skills

Zetra includes three repository workflow skills under `skills/`. They are drafts: they guide an assistant through the repository's contracts and evidence; they do not grant deployment authority or provide a runtime security boundary.

| Skill | Use | Expected result |
| --- | --- | --- |
| `zetra-agent-development` | Create or change a package, typed factory and evaluation suite | Reviewed implementation, fresh executable evidence and affected quality checks |
| `zetra-deployment-qualification` | Qualify an explicitly authorized environment | Positive and negative boundary tests, a compatibility tuple and open controls |
| `zetra-governance-review` | Assess risk, release evidence, publication or revocation | A decision tied to evidence, ownership and unresolved promotion gates |

Read `CONTRIBUTING.md` and the skill before enabling it. Each skill has a portable `SKILL.md` with a narrow name and description. `agents/openai.yaml` adds Codex display metadata. The skills deliberately preserve the difference between candidate evidence, runtime discovery, tested enforcement and authorized production promotion.

## Optional repository installation

Codex discovers project skills in `.agents/skills/`, including symlinked folders. See the [official Codex skill documentation](https://learn.chatgpt.com/docs/build-skills). Claude Code discovers project skills in `.claude/skills/<skill-name>/SKILL.md` and supports symlinked directories. See [Claude Code skills](https://code.claude.com/docs/en/skills).

From the repository root, optionally link the reviewed drafts:

```bash
mkdir -p .agents/skills .claude/skills
for name in zetra-agent-development zetra-deployment-qualification zetra-governance-review; do
  ln -s "../../skills/$name" ".agents/skills/$name"
  ln -s "../../skills/$name" ".claude/skills/$name"
done
```

These commands intentionally fail when a destination already exists; inspect an existing skill rather than overwriting it. No global skills or local hooks were installed while preparing this repository. Load or restart the assistant according to its current discovery behavior, then invoke `$zetra-agent-development` in Codex or `/zetra-agent-development` in Claude Code. An invocation helps select the workflow; it does not bypass normal authorization or repository checks.

## Validation and maintenance

All three drafts passed the skill creator's frontmatter/name validation. An independent agent exercised governance review against the actual action example and correctly withheld production promotion while recognizing the demonstrated SQLite transaction mechanics. It also reviewed deployment instructions for authority preservation and the distinction between discovery and enforcement. The observed results and limits are in [skill-forward-test.md](skill-forward-test.md).

This was an instruction-level forward test, not a runtime test of both products' discovery and invocation. Recheck each supported product after installing a skill. For future changes, use representative tasks: a legitimate implementation change, stale evidence, an attempted permission expansion, a failed negative-path test and a request to publish without authorization. Measure whether the assistant completes authorized work and reports missing controls accurately. Review skill instructions with the same care as CI and release policy.
