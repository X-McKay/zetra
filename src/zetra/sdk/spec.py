"""Strict loading of a PydanticAI Agent Spec.

Upstream ``AgentSpec`` ignores unknown keys at runtime; Zetra checks the file against the
spec's own fields first so a misspelled key is an error, and refuses spec features that
would bypass the gateway or the model policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from pydantic_ai import AgentSpec

from ..manifest import ContractError, UniqueLoader
from .errors import CompileError

REFUSED_CAPABILITIES = {
    "MCP",
    "WebFetch",
    "WebSearch",
    "XSearch",
    "LocalWorkspace",
    "NativeTool",
    "ImageGeneration",
    "ToolSearch",
}
ALLOWED_CAPABILITIES = {"Skills", "GatewayMCP", "Thinking", "Caching"}
REFUSED_SETTINGS = {"extra_body", "extra_headers"}


def load_spec(path: Path) -> tuple[AgentSpec, dict[str, Any]]:
    if not path.is_file() or path.stat().st_size > 128_000:
        raise CompileError("ZT-SPEC-001", "agent.yaml must exist and be <= 128 KB", where=str(path))
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (yaml.YAMLError, ContractError) as exc:
        raise CompileError("ZT-SPEC-002", f"invalid YAML: {exc}", where=str(path)) from exc
    if not isinstance(raw, dict):
        raise CompileError("ZT-SPEC-002", "agent.yaml must be a mapping", where=str(path))
    known = {f.alias or n for n, f in AgentSpec.model_fields.items()} | set(AgentSpec.model_fields)
    unknown = set(raw) - known
    if unknown:
        raise CompileError(
            "ZT-SPEC-004",
            f"unknown spec keys {sorted(unknown)}; upstream would silently ignore them",
            where=str(path),
        )
    if "metadata" in raw:
        raise CompileError(
            "ZT-SPEC-005", "metadata is not used by Zetra; remove it", where=str(path)
        )
    model = raw.get("model")
    if not isinstance(model, str) or not model.startswith("approved:"):
        raise CompileError("ZT-SPEC-003", "model must be approved:<policy>", where=str(path))
    settings = raw.get("model_settings") or {}
    if isinstance(settings, dict) and set(settings) & REFUSED_SETTINGS:
        raise CompileError("ZT-SPEC-006", "extra_body/extra_headers are refused", where=str(path))
    for entry in raw.get("capabilities") or []:
        name = (
            entry
            if isinstance(entry, str)
            else next(iter(entry), None)
            if isinstance(entry, dict)
            else None
        )
        if name in REFUSED_CAPABILITIES:
            raise CompileError(
                "ZT-SPEC-007", f"capability {name} bypasses the gateway; refused", where=str(path)
            )
        if name not in ALLOWED_CAPABILITIES:
            raise CompileError(
                "ZT-SPEC-008",
                f"capability {name!r} is not on the reviewed allowlist",
                where=str(path),
            )
    try:
        spec = AgentSpec.model_validate(raw)
    except ValidationError as exc:
        raise CompileError(
            "ZT-SPEC-009", f"spec does not validate: {exc}", where=str(path)
        ) from exc
    return spec, raw


def capability_entry(entry: Any) -> tuple[str, dict[str, Any]]:
    """Read one spec capability entry as ``(kind, arguments)``; upstream parses them as
    ``CapabilitySpec(name, arguments)``, raw YAML as a string or a one-key mapping."""
    name, arguments = getattr(entry, "name", None), getattr(entry, "arguments", None)
    if isinstance(name, str):
        return name, dict(arguments) if isinstance(arguments, dict) else {}
    if isinstance(entry, str):
        return entry, {}
    if isinstance(entry, dict) and len(entry) == 1:
        kind, value = next(iter(entry.items()))
        return kind, dict(value) if isinstance(value, dict) else {}
    raise CompileError("ZT-SPEC-010", f"unreadable capability entry {entry!r}")


ZETRA_CAPABILITIES = {"Skills", "GatewayMCP"}  # built by Zetra as toolsets, not by upstream


def upstream_spec(spec: AgentSpec) -> AgentSpec:
    """The spec with Zetra's capability entries removed, for ``Agent.from_spec``."""
    kept = [c for c in spec.capabilities if capability_entry(c)[0] not in ZETRA_CAPABILITIES]
    return spec.model_copy(update={"capabilities": kept})
