"""The skill tools the runtime offers the model: `skill_use`, `skill_file` and `skill_run`, and
the part of the system prompt that lists the skills (section 8 of
`platform/registry/contract/skills.md`).

Everything here reads the app directory and nothing else. `skill_run` hands the call to
`launcher.run`; no other code in the step starts a process.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any

from agent_runtime.skills import format as fmt
from agent_runtime.skills import launcher
from agent_runtime.tools import Tool

WINDOW = 24000  # characters of a file in one `skill_file` result
MAX_SKILL_MD = 32768  # bytes
MAX_RESULT = 60000  # bytes of a result as JSON, so that it is never cut (the step's bound is 65536)

LINE_BREAKS = re.compile(r"\r\n|\r|\n")

REFUSAL_TEXT = {
    "unknown_skill": "That is not a skill this agent lists.",
    "undeclared": "That skill declares no scripts.",
    "unknown_script": "That script is not one the skill lists.",
    "path": "That path is not a regular file inside the skill.",
    "arguments": "The arguments are not a list of strings within the bounds.",
    "interpreter": "The script's interpreter was not found.",
    "digest": "The skill is not what the lock pins.",
    "workspace": "The workspace is not a directory.",
    "unsupported": "Scripts cannot run here.",
    "not_text": "That file is not UTF-8 text.",
    "too_large": "SKILL.md is larger than 32768 bytes.",
}

SCHEMAS = {
    "skill_use": {
        "type": "function",
        "function": {
            "name": "skill_use",
            "description": "Read a skill's instructions. Returns its body, its other files "
            "and the scripts it may run.",
            "parameters": {
                "type": "object",
                "properties": {"skill": {"type": "string"}},
                "required": ["skill"],
            },
        },
    },
    "skill_file": {
        "type": "function",
        "function": {
            "name": "skill_file",
            "description": "Read a text file of a skill, up to 24000 characters from offset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string"},
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                },
                "required": ["skill", "path"],
            },
        },
    },
    "skill_run": {
        "type": "function",
        "function": {
            "name": "skill_run",
            "description": "Run a script of a skill with a list of arguments. No shell is used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string"},
                    "script": {"type": "string"},
                    "arguments": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["skill", "script", "arguments"],
            },
        },
    },
}


def load(app: pathlib.Path, manifest: Any) -> list[fmt.Skill]:
    """The skills the manifest lists, read from the app directory, in the manifest's order.
    Raises SkillError (an agent error) unless every one is there and the lock pins exactly
    them (section 8.1). An agent that lists none has none, whatever the directory holds."""
    names = fmt.listed(manifest)
    if not names:
        return []
    skills_dir = pathlib.Path(app) / "skills"
    problems = fmt.lock_problems(names, skills_dir, pathlib.Path(app) / fmt.LOCK)
    if problems:
        raise fmt.SkillError("; ".join(problems))
    return [fmt.read(skills_dir / name) for name in names]


def prompt_section(skills: list[fmt.Skill]) -> str:
    """What follows the agent's instructions in the system prompt (section 8.2)."""
    lines = [f"- {s.name}: {LINE_BREAKS.sub(' ', s.description)}" for s in skills]
    head = (
        "\n## Skills\n\nBefore a task that one of these skills covers, call `skill_use` "
        "with its name and follow what it returns.\n\n"
    )
    return head + "\n".join(lines)


def refused(code: str) -> dict:
    return {"refused": code, "message": REFUSAL_TEXT[code]}


def confined(root: pathlib.Path, relative: Any) -> pathlib.Path | None:
    """`relative` as a regular file of the skill directory `root`, or None. No link at any
    step, and nothing that leaves the directory (section 9.2)."""
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        return None
    pure = pathlib.PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
        return None
    here = root
    if here.is_symlink() or here.parent.is_symlink():
        return None
    for part in pure.parts:
        here = here / part
        if here.is_symlink():
            return None
    return here if here.is_file() else None


def digest_of(arguments: tuple[str, ...]) -> str:
    text = json.dumps(list(arguments), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SkillTools:
    """The built-in tools for one session. `script_run` holds the record of the last
    `skill_run` call, for the step to put on the boundary's output."""

    def __init__(
        self, app: pathlib.Path, workspace: pathlib.Path, skills: list[fmt.Skill], effects: str
    ) -> None:
        self.app, self.workspace = pathlib.Path(app), pathlib.Path(workspace)
        self.skills = {s.name: s for s in skills}
        self.names = [s.name for s in skills]
        granted = set(re.split(r"[,\s]+", effects))
        self.run_offered = "sandbox_execute" in granted and any(
            s.sidecar is not None and s.sidecar.scripts for s in skills
        )
        self.script_run: dict | None = None

    def tools(self) -> dict[str, Tool]:
        found = {
            "skill_use": Tool(self.skill_use, "workspace_read"),
            "skill_file": Tool(self.skill_file, "workspace_read"),
        }
        if self.run_offered:
            found["skill_run"] = Tool(self.skill_run, "sandbox_execute")
        return found

    def directory(self, skill: Any) -> pathlib.Path | None:
        if isinstance(skill, str) and skill in self.skills:
            return self.app / "skills" / skill
        return None

    async def skill_use(self, skill=None) -> dict:
        """Read a skill's instructions: its body, its other files and the scripts it may run."""
        root = self.directory(skill)
        if root is None:
            return refused("unknown_skill")
        path = root / fmt.SKILL_FILE
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SKILL_MD:
            return refused("too_large" if path.is_file() else "path")
        try:
            _, body = fmt.front_matter(root)
            found = [p for p, _ in fmt.files(root) if p not in (fmt.SKILL_FILE, fmt.SIDECAR)]
        except fmt.SkillError:
            return refused("not_text")
        known = self.skills[skill]
        declared = known.sidecar.scripts if known.sidecar is not None else ()
        return {
            "skill": skill,
            "version": known.version,
            "body": body,
            "files": found,
            "scripts": [s.path for s in declared] if self.run_offered else [],
        }

    async def skill_file(self, skill=None, path=None, offset=0) -> dict:
        """Read a text file of a skill, up to 24000 characters from offset."""
        root = self.directory(skill)
        if root is None:
            return refused("unknown_skill")
        if offset is None:
            offset = 0
        if type(offset) is not int or offset < 0:
            return refused("arguments")
        target = confined(root, path)
        if target is None:
            return refused("path")
        try:
            text = target.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return refused("not_text")
        end = min(offset + WINDOW, len(text))
        while True:  # shrink the window until the result fits whole, so no text is cut
            result = {
                "skill": skill,
                "path": path,
                "content": text[offset:end],
                "next_offset": end if end < len(text) else None,
            }
            if len(json.dumps(result)) <= MAX_RESULT or end <= offset + 1:
                return result
            end = offset + max(1, (end - offset) * 3 // 4)

    async def skill_run(self, skill=None, script=None, arguments=None) -> dict:
        """Run a script of a skill with a list of arguments. No shell is used."""
        run = launcher.run(self.app, self.workspace, self.names, skill, script, arguments)
        self.script_run = {
            "skill": run.skill,
            "script": run.script,
            "digest": run.digest,
            "argument_count": len(run.arguments),
            "arguments_sha256": digest_of(run.arguments),
            "exit_status": run.exit_status,
            "limits_hit": list(run.limits_hit),
            "refused": run.refused,
            "duration_ms": run.duration_ms,
        }
        if run.refused is not None:
            return refused(run.refused)
        return {
            "exit_status": run.exit_status,
            "limits_hit": list(run.limits_hit),
            "stdout": run.stdout,
            "stderr": run.stderr,
            "duration_ms": run.duration_ms,
        }
