"""Agent Skills from a spec entry: SKILL.md catalogue in instructions, body on demand.

Only ``name``, ``description`` and the Markdown body are used. ``references/``, ``scripts/``
and ``assets/`` are never read or executed. Skills are read once at build time and their
digests go into the lock; the runtime serves the bundled text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic_ai.toolsets import FunctionToolset

from .errors import CompileError

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_FRONTMATTER = {"name", "description", "license", "compatibility", "metadata"}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    digest: str


def read_skill(directory: Path) -> Skill:
    path = directory / "SKILL.md"
    if directory.is_symlink() or path.is_symlink():
        raise CompileError("ZT-SKILL-001", "symlinked skills are refused", where=str(path))
    if not path.is_file():
        raise CompileError("ZT-SKILL-002", "SKILL.md not found", where=str(directory))
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise CompileError("ZT-SKILL-003", "SKILL.md needs YAML frontmatter", where=str(path))
    end = text.index("\n---\n", 4)
    try:
        front = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        raise CompileError("ZT-SKILL-003", f"invalid frontmatter: {exc}", where=str(path)) from exc
    if not isinstance(front, dict):
        raise CompileError("ZT-SKILL-003", "frontmatter must be a mapping", where=str(path))
    unknown = set(front) - ALLOWED_FRONTMATTER
    if unknown:
        raise CompileError(
            "ZT-SKILL-004",
            f"unsupported frontmatter {sorted(unknown)} (allowed-tools is refused)",
            where=str(path),
        )
    name = front.get("name", directory.name)
    if (
        not isinstance(name, str)
        or not NAME.fullmatch(name)
        or len(name) > 64
        or name != directory.name
    ):
        raise CompileError(
            "ZT-SKILL-005",
            "name must be lowercase-hyphenated and match the directory",
            where=str(path),
        )
    description = front.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise CompileError(
            "ZT-SKILL-006", "description is required (<= 1024 characters)", where=str(path)
        )
    body = text[end + 5 :].strip()
    if not body:
        raise CompileError("ZT-SKILL-007", "skill body is empty", where=str(path))
    return Skill(name, description.strip(), body, hashlib.sha256(text.encode()).hexdigest())


def skills_toolset(skills: dict[str, Skill]) -> FunctionToolset[Any]:
    """``load_skill`` plus the catalogue of skills as instructions."""

    def load_skill(name: str) -> str:
        """Load the full instructions for one of your skills by name."""
        skill = skills.get(name)
        if skill is None:
            return f"No skill named {name!r}. Available: {', '.join(skills)}"
        return f"# Skill: {skill.name}\n\n{skill.body}"

    catalogue = "\n".join(f"- {s.name}: {s.description}" for s in skills.values())
    toolset: FunctionToolset[Any] = FunctionToolset(
        id="skills",
        instructions="You have these skills. Call load_skill before doing work one of them covers.\n"
        + catalogue,
    )
    toolset.add_function(load_skill, takes_ctx=False)
    return toolset


@dataclass(frozen=True)
class SkillSet:
    """The skills one agent may load, read once at build time."""

    skills: dict[str, Skill]

    def toolset(self) -> FunctionToolset[Any]:
        return skills_toolset(self.skills)

    def lock_entries(self) -> dict[str, dict[str, str]]:
        return {name: {"SKILL.md": f"sha256:{s.digest}"} for name, s in self.skills.items()}


def load_skills(package_dir: Path, project_root: Path, arguments: dict[str, Any]) -> SkillSet:
    """Validate a ``- Skills: {directories: ..., include: [...]}`` entry and read its skills."""
    unknown = set(arguments) - {"directories", "include"}
    if unknown:
        raise CompileError(
            "ZT-SKILL-008", f"Skills takes directories and include, not {sorted(unknown)}"
        )
    directories, include = arguments.get("directories", "skills"), arguments.get("include")
    if not isinstance(include, list) or not include or any(not isinstance(n, str) for n in include):
        raise CompileError(
            "ZT-SKILL-009", "Skills.include must list the skills this agent may load"
        )
    roots = [directories] if isinstance(directories, str) else list(directories)
    resolved: list[Path] = []
    for root in roots:
        path = (package_dir / str(root)).resolve()
        if not path.is_relative_to(project_root.resolve()):
            raise CompileError("ZT-SKILL-010", f"skills directory {root} is outside the project")
        resolved.append(path)
    skills: dict[str, Skill] = {}
    for name in include:
        found = [d / name for d in resolved if (d / name).is_dir()]
        if len(found) != 1:
            raise CompileError("ZT-SKILL-011", f"skill {name!r} not found exactly once in {roots}")
        skills[name] = read_skill(found[0])
    return SkillSet(skills)
