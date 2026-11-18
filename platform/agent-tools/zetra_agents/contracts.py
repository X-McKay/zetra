"""`check --contracts DIR`: each agent's tool grants against the tool contracts (dependencies.md
section 11).

A contract is a `<server>.json` of the cluster repository's `infrastructure/mcp/contracts`
(section 2.1): its `backend` names the backend and its `tools` give each tool's `effect`. A
grant names a backend, the tools it may call and their effect. A grant is checked against
every contract that names its backend; a grant that no contract names is not an error, and
`check` says it was not checked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from zetra_agents import layout


@dataclass(frozen=True)
class Contract:
    file: str
    backend: str
    effects: dict[str, str]  # tool name to the effect the contract records


def load(directory: Path) -> tuple[list[Contract], list[str]]:
    """The contracts in DIR, and a problem for each file that is not one. A contract whose
    backend is null is for a server no backend names, so it constrains no grant."""
    if not directory.is_dir():
        return [], [f"{directory}: not a directory of tool contracts"]
    contracts: list[Contract] = []
    problems: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            problems.append(f"{path.name}: not a tool contract: it is not JSON")
            continue
        if not isinstance(document, dict) or "backend" not in document or "tools" not in document:
            problems.append(f"{path.name}: not a tool contract: it needs backend and tools")
            continue
        backend, tools = document["backend"], document["tools"]
        if backend is not None and not isinstance(backend, str):
            problems.append(f"{path.name}: not a tool contract: backend is not a string or null")
            continue
        if not isinstance(tools, list) or not all(
            isinstance(t, dict)
            and isinstance(t.get("name"), str)
            and isinstance(t.get("effect"), str)
            for t in tools
        ):
            problems.append(
                f"{path.name}: not a tool contract: tools is not a list of name and effect"
            )
            continue
        if backend is not None:
            effects = {t["name"]: t["effect"] for t in tools}
            contracts.append(Contract(path.name, backend, effects))
    return contracts, problems


def grants(agent: layout.AgentDir) -> list[dict[str, Any]]:
    """The tool grants of the agent's deploy/agent.yaml; a file that cannot be read has none,
    as `check` reports that file itself."""
    try:
        cr = agent.agent_cr()
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError):
        return []
    spec = cr.get("spec") if isinstance(cr, dict) else None
    caps = spec.get("capabilities") if isinstance(spec, dict) else None
    mcp = caps.get("mcp") if isinstance(caps, dict) else None
    return [g for g in mcp or [] if isinstance(g, dict)]


def check(agents: list[layout.AgentDir], directory: Path) -> tuple[list[str], list[str]]:
    """The problems with the agents' grants against the contracts in DIR, and the notes for
    the grants that no contract names."""
    contracts, problems = load(directory)
    notes: list[str] = []
    for agent in agents:
        for grant in grants(agent):
            backend, effect = grant.get("backend"), grant.get("effect")
            tools = grant.get("tools") if isinstance(grant.get("tools"), list) else []
            named = [c for c in contracts if c.backend == backend]
            if not named:
                notes.append(f"{agent.name}: backend {backend}: no contract names it; not checked")
                continue
            for contract in named:
                for tool in tools:
                    if tool not in contract.effects:
                        problems.append(
                            f"{agent.name}: backend {backend}: granted tool {tool} is not "
                            f"listed in {contract.file}"
                        )
                    elif contract.effects[tool] != effect:
                        problems.append(
                            f"{agent.name}: backend {backend}: granted tool {tool} has effect "
                            f"{effect} but {contract.file} records {contract.effects[tool]}"
                        )
    return problems, notes
