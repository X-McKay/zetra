"""Project-wide settings from ``zetra.toml``; environment variables override at deployment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CompileError

MODEL_ENV = "ZETRA_MODEL"


@dataclass(frozen=True)
class Project:
    root: Path
    model_policy: str = "approved:default"
    approvers: str = "group:approvers"
    approval_ttl: str = "PT24H"
    reversible: frozenset[str] = frozenset()
    receivers: dict[str, str] = field(default_factory=dict)
    gateway: dict[str, str] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)

    def resolve_model(self, policy: str) -> str | None:
        """Map an ``approved:<name>`` policy to a provider model. None means unresolved."""
        override = os.environ.get(MODEL_ENV)
        if override:
            return override
        if not policy.startswith("approved:"):
            raise CompileError("ZT-SPEC-003", f"model must be approved:<policy>, got {policy!r}")
        return self.models.get(policy.removeprefix("approved:"))


def find_project(start: Path) -> Project:
    for directory in (start, *start.parents):
        candidate = directory / "zetra.toml"
        if candidate.is_file():
            return load_project(candidate)
    return Project(root=start)


def load_project(path: Path) -> Project:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CompileError("ZT-PROJ-001", f"cannot read {path}: {exc}") from exc
    allowed = {"project", "reversible", "receivers", "gateway", "models", "trigger", "agents"}
    unknown = set(raw) - allowed
    if unknown:
        raise CompileError("ZT-PROJ-002", f"unknown sections {sorted(unknown)}", where=str(path))
    project = raw.get("project", {})
    reversible = raw.get("reversible", {}).get("writes", [])
    if not isinstance(reversible, list) or any(not isinstance(x, str) for x in reversible):
        raise CompileError("ZT-PROJ-003", "[reversible].writes must be a list of capabilities")
    gateway = raw.get("gateway", {}).get("servers", {})
    return Project(
        root=path.parent,
        model_policy=str(project.get("model_policy", "approved:default")),
        approvers=str(project.get("approvers", "group:approvers")),
        approval_ttl=str(project.get("approval_ttl", "PT24H")),
        reversible=frozenset(reversible),
        receivers={str(k): str(v) for k, v in raw.get("receivers", {}).items()},
        gateway={str(k): str(v) for k, v in gateway.items()},
        models={str(k): str(v) for k, v in raw.get("models", {}).items()},
    )
