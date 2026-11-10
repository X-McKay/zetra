"""Load an agent the way its image does, with a scripted MCP in place of the gateway.

Development tooling. Nothing here is in an agent image: the SDK an image carries is
`agent_runtime`, and this module only drives it. `loaded` lays out the image's `/app`
from an agent directory and runs the SDK's own `step.build_agent`, so the manifest and
the code are held to exactly the agreement the sandbox enforces; `FakeMcp` stands where
the gateway would and answers only what `deploy/agent.yaml` grants. The pytest plugin
(`zetra_agents.pytest_plugin`) exposes both as the `agent` fixture, and
`zetra-agents run` uses the same pieces for a whole session.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zetra_agents.layout import AgentDir

Call = tuple[str, str, dict]
_RUNNING: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "zetra_agents_running_tool", default=None
)


class UnscriptedCall(LookupError):
    """The agent called a granted MCP tool the test gave no reply for."""


def mcp_grants(agent_cr: dict) -> dict[str, set[str]]:
    """backend -> tool names, from an Agent's `spec.capabilities.mcp`."""
    grants: dict[str, set[str]] = {}
    capabilities = (agent_cr.get("spec") or {}).get("capabilities") or {}
    for grant in capabilities.get("mcp") or []:
        grants.setdefault(grant["backend"], set()).update(grant.get("tools") or [])
    return grants


class FakeMcp:
    """`ctx.mcp` without a gateway: the same signature, argument checks and result shape
    as `agent_runtime.mcp.call_tool`.

    A call outside the grants raises `PolicyDenied`, as the gateway's 403 does; a granted
    call with no scripted reply raises `UnscriptedCall`. Both are also kept, in `denied`
    and `unscripted`, because an agent may catch every exception: `problems()` reports
    them after the fact, and the `agent` fixture fails the test on any. A test that
    means to exercise a refusal sets `strict = False`.
    """

    def __init__(self, grants: dict[str, set[str]], *, agent: str = "the agent") -> None:
        self.grants = {backend: set(tools) for backend, tools in grants.items()}
        self.agent = agent
        self.strict = True
        self.calls: list[Call] = []
        self.denied: list[Call] = []
        self.unscripted: list[Call] = []
        self.misdeclared: list[tuple[str, str, str, str]] = []
        self._replies: dict[tuple[str | None, str], Any] = {}

    @classmethod
    def for_agent(cls, agent: AgentDir) -> FakeMcp:
        return cls(mcp_grants(agent.agent_cr()), agent=agent.name)

    def reply(self, tool: str, result: Any, *, backend: str | None = None) -> FakeMcp:
        """What `tool` returns from now on: text, a dict or list (sent as JSON text), an
        exception to raise, or a function of the call's arguments returning any of those.
        `backend` narrows the reply to one backend."""
        self._replies[(backend, tool)] = result
        return self

    async def __call__(
        self, backend: str, tool: str, arguments: dict, timeout: float = 25.0
    ) -> dict:
        from agent_runtime import mcp as real
        from agent_runtime.gateway import PolicyDenied

        if not real.BACKEND.match(backend):
            raise ValueError("invalid MCP backend name")
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")
        if len(json.dumps(arguments).encode()) > real.MAX_ARGS:
            raise ValueError("tool arguments exceed 32 KiB")
        call = (backend, tool, arguments)
        self.calls.append(call)
        running = _RUNNING.get()
        if running is not None and running[1] != "remote_read":
            self.misdeclared.append((*running, backend, tool))
        if tool not in self.grants.get(backend, ()):
            self.denied.append(call)
            raise PolicyDenied(f"MCP {backend} refused the call")
        for key in ((backend, tool), (None, tool)):
            if key in self._replies:
                result = self._replies[key]
                break
        else:
            self.unscripted.append(call)
            raise UnscriptedCall(f"no reply scripted for MCP {backend}/{tool}")
        if callable(result) and not isinstance(result, type):
            result = result(arguments)
        if isinstance(result, BaseException) or (
            isinstance(result, type) and issubclass(result, BaseException)
        ):
            raise result
        text = result if isinstance(result, str) else json.dumps(result)
        return {"tool": tool, "backend": backend, "text": real._truncate(text, real.MAX_RESULT)}

    def problems(self) -> list[str]:
        """Calls the deployed agent could not have made, or the test did not account for."""
        found = []
        for backend, tool in sorted({(b, t) for b, t, _ in self.denied}):
            found.append(
                f"{self.agent} called MCP {backend}/{tool}, which deploy/agent.yaml does not "
                f"grant; the gateway would refuse it. If the agent needs it, add `{tool}` to "
                f"the tools of the `{backend}` entry under spec.capabilities.mcp (a "
                "capability change for review); otherwise remove the call"
            )
        for backend, tool in sorted({(b, t) for b, t, _ in self.unscripted}):
            found.append(
                f"{self.agent} called MCP {backend}/{tool} with no scripted reply; "
                f'say what it returns: agent.mcp.reply("{tool}", "...")'
            )
        for name, effect, backend, tool in sorted(set(self.misdeclared)):
            found.append(
                f"tool {name} is declared {effect} but called MCP {backend}/{tool}; "
                "a tool that reads through the gateway is remote_read"
            )
        return found


@dataclass
class LocalAgent:
    """One agent built by the SDK from a local copy of its image layout."""

    dir: AgentDir
    app: Path
    sandbox: Path
    manifest: dict
    instructions: str
    tools: dict[str, Any]
    mcp: FakeMcp

    @property
    def name(self) -> str:
        return self.dir.name

    @property
    def workspace(self) -> Path:
        return self.sandbox / "workspace"

    async def call(self, tool: str, /, **arguments: Any) -> Any:
        """Run one tool as the model would call it and return what it returns. Exceptions
        propagate; in a sandbox the SDK reports them to the model as `{"error": <type>}`."""
        if tool not in self.tools:
            raise KeyError(f"{self.name} has no tool {tool!r}; it has {sorted(self.tools)}")
        token = _RUNNING.set((tool, self.tools[tool].effect))
        try:
            return await self.tools[tool].fn(**arguments)
        finally:
            _RUNNING.reset(token)

    def schema(self, tool: str) -> dict:
        """The function schema the model is given for one tool."""
        from agent_runtime import step

        return step.schema(self.tools[tool])


def lay_out(agent: AgentDir, app: Path) -> Path:
    """Copy what the Dockerfile copies: `agent.yaml` and everything under `src/`."""
    shutil.copytree(agent.src, app, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(agent.manifest_path, app / "agent.yaml")
    return app


@contextlib.contextmanager
def loaded(
    agent_dir: Path | AgentDir, tmp: Path, *, mcp: FakeMcp | None = None
) -> Iterator[LocalAgent]:
    """Build the agent in `agent_dir` under the empty directory `tmp`, for as long as the
    block runs. The process environment, `sys.path`, the imported agent package and the
    SDK's MCP client are restored afterwards."""
    from agent_runtime import mcp as runtime_mcp
    from agent_runtime import step

    agent = agent_dir if isinstance(agent_dir, AgentDir) else AgentDir(Path(agent_dir).resolve())
    app = lay_out(agent, Path(tmp).resolve() / "app")
    sandbox = Path(tmp).resolve() / "sandbox"
    (sandbox / "workspace").mkdir(parents=True)
    fake = mcp if mcp is not None else FakeMcp.for_agent(agent)

    environment = {"AGENT_APP_DIR": str(app), "AGENT_SANDBOX_DIR": str(sandbox)}
    before = {name: os.environ.get(name) for name in environment}
    path, modules, call_tool = list(sys.path), set(sys.modules), runtime_mcp.call_tool
    os.environ.update(environment)
    runtime_mcp.call_tool = fake
    try:
        manifest = step.load_manifest()
        instructions, tools = step.build_agent(manifest)
        yield LocalAgent(agent, app, sandbox, manifest, instructions, tools, fake)
    finally:
        runtime_mcp.call_tool = call_tool
        sys.path[:] = path
        for name in set(sys.modules) - modules:
            origin = getattr(sys.modules[name], "__file__", None)
            if origin and Path(origin).is_relative_to(app):
                del sys.modules[name]
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
