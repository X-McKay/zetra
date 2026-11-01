"""Agent definitions share one registration for capture and execution."""

import asyncio
import inspect
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Tool:
    function: Callable
    effect: str

    def __post_init__(self):
        if self.effect not in {"workspace_read", "workspace_write", "remote_read"}:
            raise ValueError("unsupported tool effect")
        if not inspect.iscoroutinefunction(self.function):
            raise ValueError("tools must be async functions")
        for parameter in inspect.signature(self.function).parameters.values():
            if parameter.kind == parameter.POSITIONAL_ONLY:
                raise ValueError("positional-only tools are unsupported")
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                raise ValueError("variadic tools are unsupported")


class AgentContext:
    def __init__(
        self, bridge, *, workspace: Path, model: str, instruction: str = "", mcp_binding=None
    ):
        from ._runtime.capture import BridgeModel

        self.workspace = Path(workspace)
        self.instruction = instruction
        self.model = BridgeModel(bridge, model=model)
        self._bridge = bridge
        self._specs = {}
        self._wrappers = []
        self._sealed = False
        self._mcp_binding = mcp_binding
        self._mcp_scope = ContextVar("zetra_mcp_scope", default=None)

    async def mcp(self, backend: str, tool: str, arguments: dict):
        from .bindings import mcp_request

        scope = self._mcp_scope.get()
        if scope is None or not scope["active"] or scope["task"] is not asyncio.current_task():
            raise ValueError("MCP requires an active registered remote_read tool")
        if self._mcp_binding is None:
            raise ValueError("MCP transport is not explicitly bound")
        mcp_request(backend, tool, arguments)
        return await self._mcp_binding(backend, tool, arguments)

    def tools(self, *specs: Tool):
        from ._runtime.capture import BridgeTool

        if self._sealed:
            raise ValueError("registration is sealed")
        result = []
        for spec in specs:
            if not isinstance(spec, Tool):
                raise TypeError("expected Tool specification")
            name = spec.function.__name__
            if name in self._specs or name == self.model.model:
                raise ValueError("duplicate executor name")
            wrapper = BridgeTool(spec.function, self._bridge)
            self._specs[name] = spec
            self._wrappers.append(wrapper)
            result.append(wrapper)
        return result

    def seal(self, agent):
        from ._runtime.adk_compat import validate_profile

        if any(
            getattr(agent, name, None) is not None
            for name in (
                "before_agent_callback",
                "after_agent_callback",
                "before_model_callback",
                "after_model_callback",
                "before_tool_callback",
                "after_tool_callback",
            )
        ):
            raise ValueError("callbacks are unsupported")
        validate_profile(agent)
        if agent.model is not self.model or len(agent.tools) != len(self._wrappers):
            raise ValueError("agent must use this context model and tools")
        if any(a is not b for a, b in zip(agent.tools, self._wrappers, strict=True)):
            raise ValueError("agent tools differ from registered tools")
        self._sealed = True
        return agent

    def executors(self):
        if not self._sealed:
            raise ValueError("registration is not sealed")
        result = {}
        for name, spec in self._specs.items():

            async def execute(args, state, key, function=spec.function, effect=spec.effect):
                scope = {"active": effect == "remote_read", "task": asyncio.current_task()}
                token = self._mcp_scope.set(scope)
                try:
                    return await function(**dict(args))
                finally:
                    scope["active"] = False
                    self._mcp_scope.reset(token)

            result[name] = execute
        return result
