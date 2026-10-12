"""``GatewayMCP``: the only route from an agent to MCP, always through the gateway.

``- GatewayMCP: {server: status, tools: [read_status]}`` declares the per-server tool
allowlist. The server URL comes from ``zetra.toml``; tests substitute a stub toolset.
MCP tools are reads in this layer: every write goes through a receiver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AbstractToolset

from . import context
from .errors import CompileError

TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(kw_only=True)
class GatewayMCP(AbstractCapability[Any]):
    server: str
    tools: list[str]

    def __post_init__(self) -> None:
        if not isinstance(self.server, str) or not TOOL_NAME.fullmatch(self.server):
            raise CompileError("ZT-MCP-001", "GatewayMCP.server must be a short lowercase name")
        if (
            not isinstance(self.tools, list)
            or not self.tools
            or any(not TOOL_NAME.fullmatch(str(t)) for t in self.tools)
        ):
            raise CompileError(
                "ZT-MCP-002", f"GatewayMCP.tools for {self.server!r} must list the allowed tools"
            )
        self.tools = [str(t) for t in self.tools]
        self.id = f"gateway-{self.server}"
        ctx = context.current()
        if self.server not in ctx.gateway_stubs and self.server not in ctx.project.gateway:
            raise CompileError(
                "ZT-MCP-003", f"gateway server {self.server!r} is not configured in zetra.toml"
            )

    @property
    def capabilities(self) -> list[str]:
        return [f"{self.server}.{tool}" for tool in self.tools]

    def get_toolset(self) -> AbstractToolset[Any] | None:
        ctx = context.current()
        allowed = set(self.tools)
        stub = ctx.gateway_stubs.get(self.server)
        if stub is not None:
            return stub.filtered(lambda _ctx, tool_def: tool_def.name in allowed)
        from pydantic_ai.mcp import (
            MCPToolset,  # imported lazily: the mcp extra is optional at runtime
        )

        headers = {"Authorization": f"Bearer {ctx.gateway_token}"} if ctx.gateway_token else None
        toolset = MCPToolset(
            ctx.project.gateway[self.server],
            id=self.id,
            headers=headers,
            include_instructions=False,
            cache_tools=False,
        )
        return toolset.filtered(lambda _ctx, tool_def: tool_def.name in allowed)
