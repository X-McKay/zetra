"""Agent definitions share one registration for capture and execution."""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Tool:
    function: Callable
    effect: str

    def __post_init__(self):
        if self.effect not in {"workspace_read", "workspace_write"}:
            raise ValueError("unsupported tool effect")
        if not inspect.iscoroutinefunction(self.function):
            raise ValueError("tools must be async functions")
        for parameter in inspect.signature(self.function).parameters.values():
            if parameter.kind == parameter.POSITIONAL_ONLY:
                raise ValueError("positional-only tools are unsupported")
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                raise ValueError("variadic tools are unsupported")


class AgentContext:
    def __init__(self, bridge, *, workspace: Path, model: str, instruction: str = ""):
        from ._runtime.capture import BridgeModel

        self.workspace = Path(workspace)
        self.instruction = instruction
        self.model = BridgeModel(bridge, model=model)
        self._bridge = bridge
        self._specs = {}
        self._wrappers = []
        self._sealed = False

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

            async def execute(args, state, key, function=spec.function):
                return await function(**dict(args))

            result[name] = execute
        return result
