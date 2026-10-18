import zetra


def test_public_agent_api_exists():
    assert hasattr(zetra, "AgentContext")
    assert hasattr(zetra, "Tool")


import pytest
from google.adk.agents import LlmAgent

from zetra import AgentContext, Tool


async def read(path: str) -> dict:
    """Read a workspace path."""
    return {"path": path}


@pytest.mark.asyncio
async def test_one_registration_drives_wrapper_and_executor(tmp_path):
    ctx = AgentContext(object(), workspace=tmp_path, model="fixture")
    wrapped = ctx.tools(Tool(read, "workspace_read"))
    agent = ctx.seal(LlmAgent(name="reader", model=ctx.model, tools=wrapped))
    assert agent.tools[0].name == "read"
    assert await ctx.executors()["read"]({"path": "."}, {}, "id") == {"path": "."}
    with pytest.raises(ValueError, match="sealed"):
        ctx.tools(Tool(read, "workspace_read"))


def test_duplicate_and_foreign_registration_rejected(tmp_path):
    ctx = AgentContext(object(), workspace=tmp_path, model="fixture")
    ctx.tools(Tool(read, "workspace_read"))
    with pytest.raises(ValueError, match="duplicate"):
        ctx.tools(Tool(read, "workspace_read"))
    with pytest.raises(ValueError, match="context"):
        ctx.seal(LlmAgent(name="reader", model=ctx.model, tools=[]))


def test_callbacks_rejected_before_execution(tmp_path):
    ctx = AgentContext(object(), workspace=tmp_path, model="fixture")

    async def callback(callback_context):
        raise AssertionError("callback must not run")

    with pytest.raises(ValueError, match="callback"):
        ctx.seal(LlmAgent(name="reader", model=ctx.model, before_agent_callback=callback))


def test_positional_only_tool_rejected():
    async def positional(path: str, /):
        return path

    with pytest.raises(ValueError, match="positional"):
        Tool(positional, "workspace_read")
