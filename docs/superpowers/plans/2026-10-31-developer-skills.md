# Zetra Developer Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Deliver a shared, versioned Claude Code/Codex developer skill pack in generated agent repositories.

**Architecture:** Own canonical files in zetra-template/template, generate Claude mirrors
from Codex-compatible SKILL.md content, and bundle with the existing SDK importer.
Use a dependency-free local checker/synchronizer and existing pytest/CI entry points.

**Tech Stack:** Python 3.12, standard library, pytest, Markdown, JSON, existing uv and Hatch packaging.

**Spec:** ../specs/2026-10-31-developer-skills-design.md

## Global Constraints

- Current SDK Git pin and lockfile stay unchanged.
- Check mode is read-only; sync refuses conflicts before writing; unrelated skills survive.
- No global installation, extra runtime dependencies, implicit external actions, or invented CLI commands.
- Fixture evidence is not model-quality, isolation, or production-approval evidence.
- Application wheels exclude developer skills; canonical-template and SDK bundle stay identical.

## Review Focus

- Local edits in Claude mirrors must survive a failed synchronization (Task 1).
- Symlinked roots and traversal entries must not read/write outside the project (Task 1).
- Renamed generated packages must keep valid pack hashes and references (Task 3).
- A supported rendering path must not omit hidden skill directories (Task 3).
- A green fixture result must not become a production-quality claim (Task 2 behavioral scenario).

## Task 1: Managed pack synchronization

**Files (relative to zetra-template):**
- Create: template/scripts/developer_skills.py
- Create: template/tests/development/test_developer_skills.py
- Create: template/developer-skills.json
- Modify: template/Justfile
- Modify: template/.github/workflows/validate.yml

**Interfaces:**
- Consumes: JSON {schema_version: 1, pack_version: string, files: {relative_skill_file: sha256}}.
- Produces: `python scripts/developer_skills.py --check` (read-only, nonzero on drift) and
  `--sync` (explicit canonical-to-Claude synchronization, conflict detection).
- Canonical root `.agents/skills`; mirror root `.claude/skills`; only inventoried zetra-* skills.

- [x] Write subprocess tests for absent implementation and missing/matching mirrors,
  canonical updates, conflicting edits, unrelated skills, unsafe inventory and symlink roots.
- [x] Run the focused tests and confirm failure from the missing script.
- [x] Implement `synchronize(root: Path, *, check: bool) -> None`; validate the inventory,
  compare every file and preflight every conflict before publishing changes.
- [x] Run focused tests; add `--check` to local validation and rendered-repository CI.

## Task 2: Skill content and developer workflow

**Files (relative to zetra-template):**
- Create: template/.agents/skills/zetra-*/SKILL.md (seven named skills in the spec)
- Generate: template/.claude/skills/zetra-*/SKILL.md
- Create: template/docs/development.md
- Create: template/CLAUDE.md
- Create: template/tests/development/skill-scenarios.json
- Modify: template/AGENTS.md and template/README.md

**Interfaces:**
- Consumes: installed SDK help, indexed factory/manifest, existing Justfile validation.
- Produces: task-specific guidance, shared command reference, versioned scenario inputs
  and behavioral acceptance criteria; no new runtime API.

- [x] Record baseline observations from the unmodified template.
- [x] Author each concise skill and validate metadata and actionable references.
- [x] Exercise representative guided scenarios with an independent assistant; record
  limitations and address concrete findings without claiming measured uplift.
- [x] Generate the manifest and mirrors, then run read-only drift validation.

## Task 3: Render, package, and bundle

**Files:**
- Modify: zetra-template/template/tests/packaging/check_wheel.py
- Modify: zetra/tests/sdk/test_scaffold.py
- Generate: zetra/src/zetra/templates/agent/** and templates/provenance.json
- Modify: zetra/docs/playbook-conformance.md
- Create: zetra/docs/developer-skills.md (delivery and validation record)

**Interfaces:**
- Consumes: committed canonical tree and unchanged renderer via scripts/sync_template.py.
- Produces: SDK `zetra init` output containing checked skills and recorded canonical provenance.

- [x] Add a negative wheel fixture proving developer-assistant assets are rejected.
- [x] Add a scaffold integration check that runs the generated pack checker and detects
  mirror drift after generation; verify it fails before importing the new tree.
- [x] Render a renamed project; run validation and wheel checks against the pinned SDK.
- [x] Commit canonical changes on the local feature branch, import with the existing
  sync script, and verify `--check` plus SDK scaffold tests and the full available suite.
- [x] Perform a fresh independent review, address material findings, and record actual
  test counts, environment limitations and unrun behavioral/production qualifications.

## Execution record

Approval: user said “This plan sounds great, let's proceed” after the skill-pack proposal.
Implementation method: inline work with a cost-effective behavioral baseline and a fresh
final reviewer. Isolated branches: codex/developer-skills in the template and SDK repositories.
No publication, merging, deployment, cluster access or paid model evaluation is included.

Completion: implemented and locally verified; see [the validation record](../../developer-skills.md) for exact counts and limitations. Actual Claude Code/Codex repeated behavioral benchmarks and hosted CI remain unrun, as scoped in the design.
