"""The one-file layout: an agent whose `agent.yaml` has a `requires` block.

The author edits `agent.yaml` only. `deploy/agent.yaml`, the Agent the cluster is given, is
generated from it (delivery.md section 1.1) and committed, so a reviewer sees what the
cluster will be given. `zetra-agents render` writes it and `check` fails when it is missing
or differs. `model.name` on this layout is an alias the gateway pins to a model (1.2).
An agent with no `requires` block is on the two-file layout and nothing here touches it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from zetra_agents.layout import AgentDir

COMMAND = "zetra-agents render"
API_VERSION = "agentplatform.io/v1alpha1"
HEADER = f"# Written by `{COMMAND}` from agent.yaml; not edited by hand.\n"
KEYS = ("owner", "description", "tenant", "identity", "budget", "tools", "effects", "runtime")
ALIASES = {"main": "llm-main", "fast": "llm-fast"}


def is_one_file(manifest: Any) -> bool:
    """Whether the manifest has a `requires` block; one that is present and null counts, as
    it is the author's to complete."""
    return isinstance(manifest, dict) and "requires" in manifest


def one_file(agent: AgentDir) -> bool:
    """Whether the agent's manifest has a `requires` block; a manifest that cannot be read
    has none, and `check` reports the file itself."""
    try:
        return is_one_file(agent.manifest())
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError):
        return False


def aliases(repo: Path) -> dict[str, str]:
    """The alias table: the repository's `[tool.zetra-agents.aliases]` when it has one,
    which replaces the built-in table, else the built-in."""
    try:
        data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return dict(ALIASES)
    table = data.get("tool", {}).get("zetra-agents", {}).get("aliases")
    if isinstance(table, dict) and all(isinstance(v, str) for v in table.values()):
        return dict(table)
    return dict(ALIASES)


def generate(manifest: dict[str, Any]) -> dict[str, Any]:
    """The Agent that `requires` and `model` describe (the mapping table of 1.1). A value
    the table needs and the author left out is left out; the Agent schema reports it."""
    block = manifest["requires"]
    model = manifest.get("model")
    entries: list[dict[str, Any]] = []
    if model is not None:
        entry: dict[str, Any] = {"backend": model["backend"], "models": [model["name"]]}
        if "budget" in block:
            entry["budget"] = block["budget"]
        entries.append(entry)
    spec = {k: block[k] for k in ("owner", "description", "identity") if k in block}
    spec["capabilities"] = {"models": entries, "mcp": block.get("tools") or []}
    if block.get("effects"):  # absent when nothing is requested (skills.md section 7.1)
        spec["capabilities"]["effects"] = block["effects"]
    if "runtime" in block:
        spec["runtime"] = block["runtime"]
    return {
        "apiVersion": API_VERSION,
        "kind": "Agent",
        "metadata": {"name": manifest["name"], "namespace": block["tenant"]},
        "spec": spec,
    }


def render_text(manifest: dict[str, Any]) -> str:
    return HEADER + yaml.safe_dump(generate(manifest), sort_keys=False)


def block_problems(name: str, manifest: dict[str, Any]) -> list[str]:
    """What stops the Agent being generated: the block's shape and its keys."""
    block = manifest["requires"]
    if not isinstance(block, dict):
        return [f"{name}: agent.yaml requires must be a mapping"]
    found = [
        f"{name}: agent.yaml requires.{key} is not a key of the block; allowed: {', '.join(KEYS)}"
        for key in block
        if key not in KEYS
    ]
    tenant = block.get("tenant")
    if not isinstance(tenant, str) or not tenant:
        found.append(f"{name}: agent.yaml requires.tenant is required (the tenant namespace)")
    tools = block.get("tools")
    if tools is not None and not isinstance(tools, list):
        found.append(f"{name}: agent.yaml requires.tools must be a list")
    effects = block.get("effects")
    if effects is not None and not isinstance(effects, list):
        found.append(f"{name}: agent.yaml requires.effects must be a list")
    if not isinstance(manifest.get("name"), str):
        found.append(f"{name}: agent.yaml name must be a string")
    return found


def line_of_model_name(text: str) -> int | None:
    """The line, counted from 1, of `model.name` in the manifest text."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode):
        return None
    for key, value in root.value:
        if key.value == "model" and isinstance(value, yaml.MappingNode):
            for k, v in value.value:
                if k.value == "name":
                    return v.start_mark.line + 1
    return None


def alias_problems(agent: AgentDir, manifest: dict[str, Any]) -> list[str]:
    """`model.name` must be an alias of `model.backend`; a null model has none to check."""
    model = manifest.get("model")
    if model is None:
        return []
    table = aliases(agent.repo)
    name, backend = model["name"], model["backend"]
    if table.get(name) == backend:
        return []
    allowed = sorted(a for a, b in table.items() if b == backend)
    path = agent.manifest_path.relative_to(agent.repo).as_posix()
    text = agent.manifest_path.read_text(encoding="utf-8")
    where = f"{path}:{line_of_model_name(text) or 1}"
    if allowed:
        may = f"it may be {' or '.join(repr(a) for a in allowed)}"
    else:
        known = ", ".join(f"{a} ({b})" for a, b in sorted(table.items())) or "none"
        may = f"no alias is defined for the backend {backend!r}; aliases are: {known}"
    if name in table:
        found = f"{name!r} is an alias of the backend {table[name]!r}, not {backend!r}"
    else:
        found = f"{name!r} is not an alias"
    return [f"{agent.name}: {where}: model.name {found}; on the one-file layout {may}"]


def generated_problems(agent: AgentDir, manifest: dict[str, Any]) -> list[str]:
    """The generated `deploy/agent.yaml` is missing or is not what would be generated."""
    path = agent.agent_cr_path
    if not path.is_file():
        return [f"{agent.name}: deploy/agent.yaml is missing; run `{COMMAND}` to generate it"]
    try:
        have = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        have = None
    if have != render_text(manifest):
        return [
            (
                f"{agent.name}: deploy/agent.yaml is not what agent.yaml requires generates; "
                f"run `{COMMAND}` (it is not edited by hand)"
            )
        ]
    return []


def generated_file_problems(agent: AgentDir) -> list[str]:
    """`generated_problems` for a one-file agent whose manifest and block can be read, and
    nothing for one that cannot be: `check` reports those."""
    try:
        manifest = agent.manifest()
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError):
        return []
    if not is_one_file(manifest) or block_problems(agent.name, manifest):
        return []
    return generated_problems(agent, manifest)


def problems(agent: AgentDir, manifest: dict[str, Any]) -> list[str]:
    """Everything `check` asks of a one-file agent beyond the two-file rules."""
    found = block_problems(agent.name, manifest)
    if found:
        return found
    found = alias_problems(agent, manifest)
    return found + generated_problems(agent, manifest)


def write(agent: AgentDir) -> bool:
    """Write the generated `deploy/agent.yaml`; True when the file changed."""
    manifest = agent.manifest()
    if block_problems(agent.name, manifest):
        return False
    text = render_text(manifest)
    path = agent.agent_cr_path
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True
