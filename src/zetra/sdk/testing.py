"""pytest support: the ``agent`` fixture, a scripted model, stub gateways and ledger receivers.

Register through the ``pytest11`` entry point or ``pytest_plugins = ["zetra.sdk.testing"]``.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset

from .assemble import assemble
from .bundle import Bundle, build
from .contracts import Contract, RunDeps
from .receiver import MemoryReceiver
from .runner import Decision, Pending, RunError, Session, make_deps
from .tools import Bindings, ensure_importable


@dataclass(frozen=True)
class call:
    """A scripted model turn that calls ``tool`` with ``args``."""

    tool: str
    args: dict[str, Any]

    def __init__(self, tool: str, **args: Any):
        object.__setattr__(self, "tool", tool)
        object.__setattr__(self, "args", args)


@dataclass(frozen=True)
class final:
    """A scripted model turn that returns ``output`` as the structured result."""

    output: Contract | str


Turn = call | final | tuple[call, ...]  # a tuple of calls is one turn with several tool calls


class ScriptedModel:
    """A model that follows a script, one turn per model response, as a pure function of history."""

    def __init__(self) -> None:
        self.turns: list[Turn] = []

    def script(self, *turns: Turn) -> ScriptedModel:
        self.turns = list(turns)
        return self

    def build(self) -> Any:
        if not self.turns:
            return TestModel()
        turns = self.turns

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            index = sum(isinstance(m, ModelResponse) for m in messages)
            turn = turns[min(index, len(turns) - 1)]
            if isinstance(turn, tuple):
                parts = [
                    ToolCallPart(c.tool, c.args, tool_call_id=f"script:{index}:{c.tool}:{i}")
                    for i, c in enumerate(turn)
                ]
                return ModelResponse(parts=parts)
            if isinstance(turn, call):
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            turn.tool, turn.args, tool_call_id=f"script:{index}:{turn.tool}"
                        )
                    ]
                )
            if isinstance(turn.output, str):
                return ModelResponse(parts=[TextPart(turn.output)])
            wanted = type(turn.output).__name__
            tools = info.output_tools
            chosen = next(
                (t for t in tools if t.name.endswith(wanted)), tools[0] if tools else None
            )
            if chosen is None:
                return ModelResponse(parts=[TextPart(turn.output.model_dump_json())])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        chosen.name,
                        turn.output.model_dump(mode="json"),
                        tool_call_id=f"script:{index}:final",
                    )
                ]
            )

        return FunctionModel(respond, model_name="zetra-script")


class StubGateway:
    """Stands in for one gateway MCP server. Declare each tool's canned result and parameters."""

    def __init__(self, server: str):
        self.server = server
        self.results: dict[str, Any] = {}
        self.params: dict[str, Sequence[str]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def stub(self, tool: str, result: Any, params: Sequence[str] = ()) -> StubGateway:
        self.results[tool] = result
        self.params[tool] = tuple(params)
        return self

    def toolset(self) -> FunctionToolset[Any]:
        toolset: FunctionToolset[Any] = FunctionToolset(id=f"gateway-{self.server}")
        for name, result in self.results.items():
            toolset.add_function(
                self._function(name, result, self.params[name]), takes_ctx=True, name=name
            )
        return toolset

    def _function(self, name: str, result: Any, params: Sequence[str]) -> Any:
        calls = self.calls

        def stub(ctx: RunContext[Any], *args: Any, **kwargs: Any) -> Any:
            calls.append((name, dict(kwargs)))
            return result

        parameters = [
            inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_ONLY, annotation=RunContext[Any])
        ]
        parameters += [
            inspect.Parameter(p, inspect.Parameter.KEYWORD_ONLY, annotation=str, default="")
            for p in params
        ]
        shaped: Any = stub
        shaped.__signature__ = inspect.Signature(parameters, return_annotation=Any)
        shaped.__annotations__ = {"ctx": RunContext[Any], **{p: str for p in params}, "return": Any}
        stub.__name__, stub.__doc__ = name, f"Stubbed gateway tool {name} on server {self.server}."
        return stub


@dataclass
class Approvals:
    items: list[Pending] = field(default_factory=list)

    @property
    def pending(self) -> list[Contract]:
        return [item.payload for item in self.items]


class Run:
    """One run of the agent under test; pauses at approvals until told what to do."""

    def __init__(self, session: Session):
        self._session = session
        asyncio.run(session.start())

    @property
    def approvals(self) -> Approvals:
        return Approvals(list(self._session.pending))

    @property
    def output(self) -> Any:
        return self._session.outcome.output

    @property
    def status(self) -> str:
        return self._session.outcome.status

    @property
    def error(self) -> RunError | None:
        return self._session.outcome.error

    @property
    def messages(self) -> list[ModelMessage]:
        return self._session.outcome.messages

    @property
    def proposals(self) -> list[Pending]:
        return list(self._session.outcome.proposals)

    def _decide(self, approved: bool, actor: str, reason: str = "") -> Run:
        decisions = {
            item.tool_call_id: Decision(approved, actor, reason) for item in self._session.pending
        }
        asyncio.run(self._session.decide(decisions))
        return self

    def approve_all(self, actor: str = "test:approver") -> Run:
        return self._decide(True, actor)

    def reject_all(self, actor: str = "test:approver", reason: str = "rejected in test") -> Run:
        return self._decide(False, actor, reason)


class AgentUnderTest:
    def __init__(self, bundle: Bundle):
        self.bundle = bundle
        self.model = ScriptedModel()
        self._gateways: dict[str, StubGateway] = {}
        self._receivers: dict[str, MemoryReceiver] = {}
        for decl in bundle.tools:
            if decl.is_write:
                self._receivers.setdefault(decl.resource, MemoryReceiver(decl.resource))
        for server in bundle.mcp:
            self._gateways[server] = StubGateway(server)

    def gateway(self, server: str) -> StubGateway:
        if server not in self._gateways:
            raise KeyError(f"{self.bundle.name} declares no GatewayMCP server {server!r}")
        return self._gateways[server]

    def receiver(self, resource: str) -> MemoryReceiver:
        return self._receivers.setdefault(resource, MemoryReceiver(resource))

    def run(
        self, value: Contract, *, principal: str = "test:caller", run_id: str = "test-run"
    ) -> Run:
        agent = assemble(
            self.bundle,
            Bindings(dict(self._receivers)),
            model=self.model.build(),
            gateway_stubs={server: stub.toolset() for server, stub in self._gateways.items()},
        )
        deps: RunDeps = make_deps(self.bundle, value, run_id=run_id, principal=principal)
        return Run(Session(agent, self.bundle, value, deps))


def agent_under_test(package_dir: Path) -> AgentUnderTest:
    return AgentUnderTest(build(package_dir))


def _package_for(path: Path) -> Path:
    for directory in (path, *path.parents):
        if (directory / "agent.yaml").is_file():
            return directory
    raise LookupError(f"no agent.yaml above {path}")


try:  # pytest is a development dependency; the plugin is optional at runtime
    import pytest

    def pytest_collect_file(file_path: Path, parent: Any) -> None:
        """Before a test under an agent package is imported, make the package importable."""
        if file_path.suffix == ".py":
            try:
                ensure_importable(_package_for(file_path.parent))
            except LookupError:
                pass
        return None

    @pytest.fixture
    def agent(request: pytest.FixtureRequest) -> AgentUnderTest:
        """The agent whose package contains the current test file."""
        return agent_under_test(_package_for(Path(str(request.node.path)).parent))

except ImportError:  # pragma: no cover
    pass
