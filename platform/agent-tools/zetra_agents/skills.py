"""What `check` holds for an agent that lists skills, and the lock `render` writes
(sections 4 and 6 of `platform/registry/contract/skills.md`).

Skill files are read only through `agent_runtime.skills.format`, the one reader; this module
decides what is a problem and how it is worded.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import skills_ref
from agent_runtime.skills import format as fmt

from zetra_agents import declaration

if TYPE_CHECKING:
    from zetra_agents.layout import AgentDir

EFFECT = "sandbox_execute"
MAX_SKILL_FILE = 32768  # bytes of SKILL.md (section 8.3)
SKILLS_DIR = "skills"
COMMAND = "zetra-agents render"


def skills_dir(agent: AgentDir) -> Path:
    return agent.repo / SKILLS_DIR


def lock_path(agent: AgentDir) -> Path:
    return agent.root / fmt.LOCK


def listed(agent: AgentDir) -> list[str]:
    """The skills `agent.yaml` lists; [] for no key, an unreadable manifest or a bad list
    (`check` reports those)."""
    try:
        return fmt.listed(agent.manifest())
    except Exception:  # noqa: BLE001 - a manifest or list that cannot be read lists none
        return []


def read_all(agent: AgentDir, names: list[str]) -> list[fmt.Skill]:
    """Each listed skill as `format.read` reads it. Raises `fmt.SkillError`."""
    return [fmt.read(skills_dir(agent) / name) for name in names]


def lock_text(agent: AgentDir) -> str | None:
    """What `skills.lock` should say, or None when the agent lists no skills."""
    names = listed(agent)
    return fmt.lock_text(read_all(agent, names)) if names else None


def write_lock(agent: AgentDir) -> bool:
    """Write the lock the listed skills give, or remove it when none are listed. True when
    a file changed. A skill that cannot be read leaves the lock alone; `check` says why."""
    path = lock_path(agent)
    try:
        text = lock_text(agent)
    except fmt.SkillError:
        return False
    if text is None:
        if path.is_file():
            path.unlink()
            return True
        return False
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def lock_differences(agent: AgentDir) -> list[str]:
    """Why the lock is not what `write_lock` would write; [] when it is."""
    names = listed(agent)
    found = fmt.lock_problems(names, skills_dir(agent), lock_path(agent))
    return [f"{p}; run `{COMMAND}`" for p in found]


def release_entries(agent: AgentDir) -> list[dict[str, Any]]:
    """The `spec.skills` of a release: one entry per listed skill, in name order."""
    skills = sorted(read_all(agent, listed(agent)), key=lambda s: s.name)
    return [fmt.release_entry(s) for s in skills]


def _requested_tools(manifest: dict[str, Any]) -> dict[str, set[str]]:
    block = manifest.get("requires")
    entries = block.get("tools") if isinstance(block, dict) else None
    found: dict[str, set[str]] = {}
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("backend"), str):
            tools = entry.get("tools")
            found.setdefault(entry["backend"], set()).update(
                t for t in (tools if isinstance(tools, list) else []) if isinstance(t, str)
            )
    return found


def _requested_effects(agent: AgentDir, manifest: dict[str, Any]) -> set[str]:
    """The effects the agent asks for: `requires.effects` on the one-file layout, and
    `spec.capabilities.effects` of the hand-written Agent on the other."""
    if declaration.is_one_file(manifest):
        block = manifest.get("requires")
        value = block.get("effects") if isinstance(block, dict) else None
    else:
        try:
            spec = (agent.agent_cr() or {}).get("spec") or {}
            value = (spec.get("capabilities") or {}).get("effects")
        except Exception:  # noqa: BLE001 - an unreadable Agent is reported where it is read
            value = None
    return {e for e in value if isinstance(e, str)} if isinstance(value, list) else set()


def requested_effects(agent: AgentDir) -> list[str]:
    """The effects the agent requests, sorted: what the pod's `AGENT_EFFECTS` would say."""
    try:
        return sorted(_requested_effects(agent, agent.manifest()))
    except Exception:  # noqa: BLE001 - an unreadable manifest requests nothing; `check` reports it
        return []


def _lines(need: fmt.ToolNeed) -> list[str]:
    return [
        "    - backend: " + need.backend,
        "      tools: [" + ", ".join(need.tools) + "]",
    ]


def _tool_problems(name: str, skill: fmt.Skill, have: dict[str, set[str]]) -> list[str]:
    missing = [
        need
        for need in (skill.sidecar.tools if skill.sidecar else ())
        if not set(need.tools) <= have.get(need.backend, set())
    ]
    if not missing:
        return []
    lines = ["  requires:", "    tools:"]
    for need in missing:
        lines += _lines(need)
    head = f"{name}: skill {skill.name} needs tools the agent does not request; add to agent.yaml:"
    return ["\n".join([head, *lines])]


def problems(agent: AgentDir, manifest: dict[str, Any]) -> list[str]:
    """The six rules of section 6, for an agent that lists skills; the agent with none has
    nothing to answer for here but a lock it should not have, and an effect it should not
    request."""
    name = agent.name
    try:
        names = fmt.listed(manifest)
    except fmt.SkillError as exc:
        return [f"{name}: {exc}"]
    requested = EFFECT in _requested_effects(agent, manifest)
    if names and not declaration.is_one_file(manifest):
        return [f"{name}: agent.yaml lists skills and has no requires block (one-file layout)"]
    found: list[str] = []
    if names:
        found += [
            f"{name}: src/ holds {entry.name}, where the image keeps skills; rename it"
            for entry in sorted(agent.src.iterdir() if agent.src.is_dir() else [])
            if entry.name in (SKILLS_DIR, fmt.LOCK)
        ]
    skills: list[fmt.Skill] = []
    for skill_name in names:
        directory = skills_dir(agent) / skill_name
        if not directory.is_dir():
            found.append(f"{name}: skill {skill_name} is not a directory under {SKILLS_DIR}/")
            continue
        refused = [f"{name}: skill {skill_name}: {m}" for m in skills_ref.validate(directory)]
        try:
            skill = fmt.read(directory)
            size = (directory / fmt.SKILL_FILE).stat().st_size
        except (fmt.SkillError, OSError) as exc:
            refused.append(f"{name}: skill {skill_name}: {exc}")
        else:
            if size > MAX_SKILL_FILE:
                refused.append(
                    f"{name}: skill {skill_name}: {fmt.SKILL_FILE} is {size} bytes, over "
                    f"{MAX_SKILL_FILE}"
                )
            skills.append(skill)
        found += refused
    if found:
        return found
    found += [f"{name}: {p}" for p in lock_differences(agent)]
    have = _requested_tools(manifest)
    for skill in skills:
        found += _tool_problems(name, skill, have)
    scripted = [s.name for s in skills if s.sidecar and s.sidecar.scripts]
    if scripted and not requested:
        found.append(
            f"{name}: skill {', '.join(scripted)} declares scripts, which run only with the "
            f"effect {EFFECT}; add to agent.yaml:\n  requires:\n    effects: [{EFFECT}]"
        )
    elif requested and not scripted:
        found.append(
            f"{name}: the agent requests {EFFECT} and no listed skill declares scripts; "
            "remove it from requires.effects"
        )
    return found


def notes(agent: AgentDir, manifest: dict[str, Any]) -> list[str]:
    """What `check` prints without failing: skills with no sidecar, and `allowed-tools`
    fronts, which are ignored."""
    found = []
    try:
        names = fmt.listed(manifest)
    except fmt.SkillError:
        return []
    for skill_name in names:
        directory = skills_dir(agent) / skill_name
        try:
            skill = fmt.read(directory)
            meta, _ = fmt.front_matter(directory)
        except fmt.SkillError:
            continue
        if skill.sidecar is None:
            found.append(
                f"{agent.name}: skill {skill_name} has no {fmt.SIDECAR}: its tool needs are "
                "unknown and its scripts do not run"
            )
        if "allowed-tools" in meta:
            found.append(
                f"{agent.name}: skill {skill_name} has allowed-tools in {fmt.SKILL_FILE}; "
                "it is ignored"
            )
    return found
