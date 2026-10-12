"""Agent Skills as a spec capability: SKILL.md catalogue in instructions, body on demand.

Only ``name``, ``description`` and the Markdown body are used. ``references/``, ``scripts/``
and ``assets/`` are never read or executed. Skills are read once at build time and their
digests go into the lock; the runtime serves the bundled text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import FunctionToolset

from . import context
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


@dataclass(kw_only=True)
class Skills(AbstractCapability[Any]):
    """``- Skills: {directories: skills, include: [a, b]}`` in a spec."""

    directories: str | list[str]
    include: list[str]
    exclude: list[str] | None = None
    skills: dict[str, Skill] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.id = "skills"
        if self.exclude:
            raise CompileError("ZT-SKILL-008", "use include; exclude is not supported")
        if not isinstance(self.include, list) or not self.include:
            raise CompileError(
                "ZT-SKILL-009", "Skills.include must list the skills this agent may load"
            )
        ctx = context.current()
        roots = [self.directories] if isinstance(self.directories, str) else list(self.directories)
        resolved: list[Path] = []
        for root in roots:
            path = (ctx.package_dir / root).resolve()
            if not path.is_relative_to(ctx.project.root.resolve()):
                raise CompileError(
                    "ZT-SKILL-010", f"skills directory {root} is outside the project"
                )
            resolved.append(path)
        for name in self.include:
            found = [d / name for d in resolved if (d / name).is_dir()]
            if len(found) != 1:
                raise CompileError(
                    "ZT-SKILL-011", f"skill {name!r} not found exactly once in {roots}"
                )
            self.skills[name] = read_skill(found[0])

    def get_instructions(self) -> str:
        catalogue = "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())
        return (
            "You have these skills. Call load_skill before doing work one of them covers.\n"
            + catalogue
        )

    def get_toolset(self) -> FunctionToolset[Any]:
        skills = self.skills

        def load_skill(name: str) -> str:
            """Load the full instructions for one of your skills by name."""
            skill = skills.get(name)
            if skill is None:
                return f"No skill named {name!r}. Available: {', '.join(skills)}"
            return f"# Skill: {skill.name}\n\n{skill.body}"

        toolset: FunctionToolset[Any] = FunctionToolset(id="skills")
        toolset.add_function(load_skill, takes_ctx=False)
        return toolset

    def lock_entries(self) -> dict[str, dict[str, str]]:
        return {name: {"SKILL.md": f"sha256:{s.digest}"} for name, s in self.skills.items()}
