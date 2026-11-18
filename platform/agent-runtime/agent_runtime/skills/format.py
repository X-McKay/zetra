"""The files a skill is made of here: the directory digest, the front matter of `SKILL.md`,
the sidecar `zetra.yaml` and the lock `skills.lock`. The rules are sections 2 to 5 of
`platform/registry/contract/skills.md`.

This is the one implementation. The tooling imports it to write and check the lock, the image
build runs it, and the runtime reads the same files with it. Nothing here starts a process
or opens a connection.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
from dataclasses import dataclass
from typing import Any

import yaml

SKILL_FILE = "SKILL.md"
SIDECAR = "zetra.yaml"
LOCK = "skills.lock"
SCRIPTS = "scripts"
SKIPPED_DIRECTORY = "__pycache__"

NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\Z")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_SKILLS = 16  # skills one agent may list
VERSION = re.compile(r"^[^\s\x00-\x1f]{1,64}\Z")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}\Z")
SCRIPT_PATH = re.compile(r"^scripts/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*\Z")
MAX_SCRIPT_PATH = 128
INTERPRETER = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,31}\Z")
BACKEND = re.compile(r"^[a-z0-9-]{1,63}\Z")
TOOL = re.compile(r"^[A-Za-z0-9_.-]{1,128}\Z")
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 240


class SkillError(ValueError):
    """A skill directory, sidecar or lock breaks a rule of the contract. One line of text."""


@dataclass(frozen=True)
class Script:
    path: str  # relative to the skill directory, under scripts/
    interpreter: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ToolNeed:
    backend: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class Sidecar:
    tools: tuple[ToolNeed, ...] = ()
    scripts: tuple[Script, ...] = ()
    evaluation: str | None = None

    def script(self, path: str) -> Script | None:
        return next((s for s in self.scripts if s.path == path), None)


@dataclass(frozen=True)
class Skill:
    """One skill directory as read: what `SKILL.md` says, the sidecar when there is one
    (None means the skill is undeclared), and the digest of the directory."""

    name: str
    description: str
    version: str | None
    sidecar: Sidecar | None
    digest: str


def is_name(name: Any) -> bool:
    return isinstance(name, str) and len(name) <= MAX_NAME and NAME.match(name) is not None


def files(directory: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    """Every file of a skill directory as (relative POSIX path, path), in path order.

    A symbolic link, or anything that is neither a regular file nor a directory, is refused:
    a digest must name content, and a link names something outside it. Directories named
    `__pycache__` are not part of the skill.
    """
    root = pathlib.Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise SkillError(f"{root.name}: the skill directory is missing or is a symbolic link")
    found: list[tuple[str, pathlib.Path]] = []
    pending = [root]
    while pending:
        here = pending.pop()
        with os.scandir(here) as entries:
            for entry in entries:
                path = pathlib.Path(entry.path)
                relative = path.relative_to(root).as_posix()
                if entry.is_symlink():
                    raise SkillError(f"{root.name}: {relative} is a symbolic link")
                if entry.is_dir(follow_symlinks=False):
                    if entry.name != SKIPPED_DIRECTORY:
                        pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    if "\n" in relative or "\r" in relative:
                        raise SkillError(f"{root.name}: a file name holds a line break")
                    found.append((relative, path))
                else:
                    raise SkillError(f"{root.name}: {relative} is not a regular file")
    return sorted(found, key=lambda item: item[0].encode("utf-8"))


def digest(directory: pathlib.Path) -> str:
    """`sha256:<hex>` of a skill directory (section 3).

    One line per file, in byte order of the path: the sha256 of the content, `x` or `-` for
    whether its owner may execute it, and the path. The digest is the sha256 of those lines.
    """
    lines = hashlib.sha256()
    for relative, path in files(directory):
        mode = "x" if path.stat().st_mode & stat.S_IXUSR else "-"
        content = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.update(f"{content} {mode} {relative}\n".encode())
    return "sha256:" + lines.hexdigest()


def front_matter(directory: pathlib.Path) -> tuple[dict[str, Any], str]:
    """The front matter of `SKILL.md` as a mapping whose scalars are all strings, and the
    body after it. Reading scalars as strings keeps `version: 1.10` as written."""
    root = pathlib.Path(directory)
    path = root / SKILL_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"{root.name}: {SKILL_FILE} cannot be read as UTF-8 text") from exc
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"{root.name}: {SKILL_FILE} must start with a front matter block")
    end = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end is None:
        raise SkillError(f"{root.name}: the front matter of {SKILL_FILE} is not closed")
    try:
        data = yaml.load("".join(lines[1:end]), Loader=yaml.BaseLoader)
    except yaml.YAMLError as exc:
        raise SkillError(f"{root.name}: the front matter of {SKILL_FILE} is not YAML") from exc
    if not isinstance(data, dict):
        raise SkillError(f"{root.name}: the front matter of {SKILL_FILE} must be a mapping")
    return data, "".join(lines[end + 1 :]).lstrip("\n")


def _mapping(value: Any, where: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in value if k not in allowed)
    if unknown:
        raise SkillError(
            f"{where} has the unknown key {unknown[0]!r}; allowed: {', '.join(allowed)}"
        )
    return value


def _inside(root: pathlib.Path, relative: Any, where: str) -> str:
    """A relative POSIX path that names a regular file of the skill directory."""
    if not isinstance(relative, str) or not relative:
        raise SkillError(f"{where} must be a path inside the skill directory")
    pure = pathlib.PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
        raise SkillError(f"{where} must be a plain relative path, and {relative!r} is not")
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise SkillError(f"{where} names {relative}, which is not a file of the skill")
    return relative


def parse_sidecar(root: pathlib.Path, data: Any) -> Sidecar:
    where = f"{root.name}: {SIDECAR}"
    data = _mapping(data, where, ("schema_version", "requires", "scripts", "evaluation"))
    if data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
        raise SkillError(f"{where} schema_version must be 1")

    requires = _mapping(data.get("requires") or {}, f"{where} requires", ("tools",))
    needs = requires.get("tools") or []
    if not isinstance(needs, list):
        raise SkillError(f"{where} requires.tools must be a list")
    tools = []
    for index, need in enumerate(needs):
        at = f"{where} requires.tools[{index}]"
        need = _mapping(need, at, ("backend", "tools"))
        backend, names = need.get("backend"), need.get("tools")
        if not isinstance(backend, str) or not BACKEND.match(backend):
            raise SkillError(f"{at} backend must be a tool backend name")
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(n, str) and TOOL.match(n) for n in names)
        ):
            raise SkillError(f"{at} tools must be a list of at least one tool name")
        tools.append(ToolNeed(backend, tuple(sorted(set(names)))))
    if len({t.backend for t in tools}) != len(tools):
        raise SkillError(f"{where} requires.tools names a backend more than once")

    entries = data.get("scripts") or []
    if not isinstance(entries, list):
        raise SkillError(f"{where} scripts must be a list")
    scripts = []
    for index, entry in enumerate(entries):
        at = f"{where} scripts[{index}]"
        entry = _mapping(entry, at, ("path", "interpreter", "timeout_seconds"))
        path = entry.get("path")
        if not isinstance(path, str) or not SCRIPT_PATH.match(path) or len(path) > MAX_SCRIPT_PATH:
            raise SkillError(
                f"{at} path must be under {SCRIPTS}/, in letters, digits, dot, hyphen and "
                f"underscore, and at most {MAX_SCRIPT_PATH} characters"
            )
        path = _inside(root, path, f"{at} path")
        interpreter = entry.get("interpreter")
        if not isinstance(interpreter, str) or not INTERPRETER.match(interpreter):
            raise SkillError(f"{at} interpreter must be a command name, such as python3")
        timeout = entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 1 <= timeout <= MAX_TIMEOUT_SECONDS
        ):
            raise SkillError(f"{at} timeout_seconds must be from 1 to {MAX_TIMEOUT_SECONDS}")
        scripts.append(Script(path, interpreter, timeout))
    if len({s.path for s in scripts}) != len(scripts):
        raise SkillError(f"{where} scripts lists a path more than once")

    evaluation = data.get("evaluation")
    if evaluation is not None:
        evaluation = _inside(root, evaluation, f"{where} evaluation")
    return Sidecar(
        tools=tuple(sorted(tools, key=lambda t: t.backend)),
        scripts=tuple(sorted(scripts, key=lambda s: s.path)),
        evaluation=evaluation,
    )


def sidecar(directory: pathlib.Path) -> Sidecar | None:
    """The sidecar of a skill directory, or None when it has none (the skill is undeclared)."""
    root = pathlib.Path(directory)
    path = root / SIDECAR
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SkillError(f"{root.name}: {SIDECAR} is not YAML text") from exc
    return parse_sidecar(root, data)


def read(directory: pathlib.Path) -> Skill:
    """One skill directory. The directory name is the skill name, as the specification asks."""
    root = pathlib.Path(directory)
    if not is_name(root.name):
        raise SkillError(f"{root.name}: not a skill name (lowercase letters, digits, hyphens)")
    value = digest(root)
    meta, _ = front_matter(root)
    if meta.get("name") != root.name:
        raise SkillError(f"{root.name}: {SKILL_FILE} name is {meta.get('name')!r}")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(f"{root.name}: {SKILL_FILE} has no description")
    if len(description.strip()) > MAX_DESCRIPTION:
        raise SkillError(f"{root.name}: the description is over {MAX_DESCRIPTION} characters")
    metadata = meta.get("metadata")
    version = metadata.get("version") if isinstance(metadata, dict) else None
    if version is not None and (not isinstance(version, str) or not VERSION.match(version)):
        raise SkillError(f"{root.name}: metadata.version must be one word of at most 64")
    return Skill(root.name, description.strip(), version, sidecar(root), value)


def lock_entry(skill: Skill) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": skill.name, "digest": skill.digest}
    if skill.version is not None:
        entry["version"] = skill.version
    return entry


def lock_text(skills: list[Skill]) -> str:
    """The text of `skills.lock` for these skills (section 4)."""
    entries = [lock_entry(s) for s in sorted(skills, key=lambda s: s.name)]
    return json.dumps({"schema_version": 1, "skills": entries}, indent=2, sort_keys=True) + "\n"


def read_lock(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """The entries of a lock file by skill name."""
    path = pathlib.Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillError(f"{path.name} cannot be read as JSON") from exc
    if not isinstance(data, dict) or set(data) != {"schema_version", "skills"}:
        raise SkillError(f"{path.name} must hold schema_version and skills")
    if data["schema_version"] != 1 or not isinstance(data["skills"], list):
        raise SkillError(f"{path.name} schema_version must be 1 and skills a list")
    entries: dict[str, dict[str, Any]] = {}
    for entry in data["skills"]:
        if (
            not isinstance(entry, dict)
            or not set(entry) <= {"name", "digest", "version"}
            or not is_name(entry.get("name"))
            or not isinstance(entry.get("digest"), str)
            or not DIGEST.match(entry["digest"])
            or ("version" in entry and not isinstance(entry["version"], str))
            or entry["name"] in entries
        ):
            raise SkillError(f"{path.name} holds an entry that is not a pinned skill")
        entries[entry["name"]] = entry
    return entries


def lock_problems(names: list[str], skills_dir: pathlib.Path, lock: pathlib.Path) -> list[str]:
    """Why the lock does not pin exactly these skills as they are on disk. [] when it does.

    `names` is the agent's `skills:` list. With no skills listed there must be no lock.
    """
    lock = pathlib.Path(lock)
    if not names:
        return [f"{lock.name} exists and the agent lists no skills"] if lock.exists() else []
    if not lock.is_file():
        return [f"{lock.name} is missing"]
    try:
        entries = read_lock(lock)
        skills = [read(pathlib.Path(skills_dir) / name) for name in names]
    except SkillError as exc:
        return [str(exc)]
    problems = [
        f"{lock.name} pins {name}, which the agent does not list"
        for name in sorted(set(entries) - set(names))
    ]
    for skill in skills:
        if entries.get(skill.name) != lock_entry(skill):
            pinned = "is not pinned" if skill.name not in entries else "changed since it was pinned"
            problems.append(f"skill {skill.name} {pinned} in {lock.name}")
    return problems


def release_entry(skill: Skill) -> dict[str, Any]:
    """One entry of `AgentRelease.spec.skills` (section 11). Keys with nothing to say are
    left out."""
    entry = lock_entry(skill)
    entry["description"] = skill.description
    entry["declared"] = skill.sidecar is not None
    if skill.sidecar is not None:
        if skill.sidecar.scripts:
            entry["scripts"] = [s.path for s in skill.sidecar.scripts]
            entry["interpreters"] = sorted({s.interpreter for s in skill.sidecar.scripts})
        if skill.sidecar.tools:
            entry["tools"] = [
                {"backend": t.backend, "tools": list(t.tools)} for t in skill.sidecar.tools
            ]
    return entry
