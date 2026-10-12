"""``GatewayMCP``: the only route from an agent to MCP, always through the gateway.

``- GatewayMCP: {server: status, tools: [read_status]}`` in the spec declares the per-server
tool allowlist. The server URL comes from ``zetra.toml``; tests substitute a stub toolset.
MCP tools are reads in this layer: every write goes through a receiver.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic_ai.toolsets import AbstractToolset

from .errors import CompileError

TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def gateway_entry(arguments: dict[str, Any]) -> tuple[str, list[str]]:
    """Validate a ``GatewayMCP`` spec entry into ``(server, tools)``."""
    unknown = set(arguments) - {"server", "tools"}
    if unknown:
        raise CompileError(
            "ZT-MCP-004", f"GatewayMCP takes server and tools, not {sorted(unknown)}"
        )
    server, tools = arguments.get("server"), arguments.get("tools")
    if not isinstance(server, str) or not TOOL_NAME.fullmatch(server):
        raise CompileError("ZT-MCP-001", "GatewayMCP.server must be a short lowercase name")
    if (
        not isinstance(tools, list)
        or not tools
        or any(not isinstance(t, str) or not TOOL_NAME.fullmatch(t) for t in tools)
    ):
        raise CompileError(
            "ZT-MCP-002", f"GatewayMCP.tools for {server!r} must list the allowed tools"
        )
    return server, list(tools)


def gateway_toolset(
    server: str,
    tools: list[str],
    *,
    url: str | None,
    token: str | None = None,
    stub: AbstractToolset[Any] | None = None,
) -> AbstractToolset[Any]:
    """The toolset for one gateway server, filtered to the declared tools."""
    allowed = set(tools)
    if stub is not None:
        return stub.filtered(lambda _ctx, tool_def: tool_def.name in allowed)
    if url is None:
        raise CompileError(
            "ZT-MCP-003", f"gateway server {server!r} is not configured in zetra.toml"
        )
    from pydantic_ai.mcp import MCPToolset  # the mcp extra is optional at runtime

    headers = {"Authorization": f"Bearer {token}"} if token else None
    toolset = MCPToolset(
        url, id=f"gateway-{server}", headers=headers, include_instructions=False, cache_tools=False
    )
    return toolset.filtered(lambda _ctx, tool_def: tool_def.name in allowed)
