import zetra


def test_public_agent_api_exists():
    assert hasattr(zetra, "AgentContext")
    assert hasattr(zetra, "Tool")


import pytest
from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext

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


async def required_context(path: str, tool_context: ToolContext):
    raise AssertionError("unsupported tool must not execute")


async def default_context(path: str, tool_context: ToolContext = None):
    raise AssertionError("unsupported tool must not execute")


async def alias_context(path: str, ctx: ToolContext):
    raise AssertionError("unsupported tool must not execute")


async def forward_context(path: str, ctx: "ToolContext"):
    raise AssertionError("unsupported tool must not execute")


async def keyword_context(path: str, *, tool_context: ToolContext):
    raise AssertionError("unsupported tool must not execute")


async def untyped_context(path: str, tool_context):
    raise AssertionError("unsupported tool must not execute")


async def streaming_input(path: str, input_stream: str):
    raise AssertionError("unsupported tool must not execute")


@pytest.mark.parametrize(
    "function",
    [
        required_context,
        default_context,
        alias_context,
        forward_context,
        keyword_context,
        untyped_context,
        streaming_input,
    ],
)
def test_adk_injected_parameters_rejected_during_registration(tmp_path, function):
    ctx = AgentContext(object(), workspace=tmp_path, model="fixture")
    with pytest.raises(ValueError, match="ADK-injected"):
        ctx.tools(Tool(function, "workspace_read"))


@pytest.mark.asyncio
async def test_ordinary_context_argument_remains_model_supplied(tmp_path):
    async def contextual_read(context: str) -> dict:
        """Read a caller-supplied context."""
        return {"context": context}

    ctx = AgentContext(object(), workspace=tmp_path, model="fixture")
    wrapped = ctx.tools(Tool(contextual_read, "workspace_read"))
    ctx.seal(LlmAgent(name="reader", model=ctx.model, tools=wrapped))
    assert await ctx.executors()["contextual_read"]({"context": "docs"}, {}, "id") == {
        "context": "docs"
    }


async def test_registered_remote_read_uses_existing_executor_boundary(tmp_path):
    async def inspect_remote() -> dict:
        """Read one approved remote resource."""
        return {"phase": "Running"}

    ctx = AgentContext(object(), workspace=tmp_path, model="fixture")
    wrapped = ctx.tools(Tool(inspect_remote, "remote_read"))
    ctx.seal(LlmAgent(name="investigator", model=ctx.model, tools=wrapped))
    assert await ctx.executors()["inspect_remote"]({}, {}, "id") == {"phase": "Running"}


def test_remote_writes_are_not_admitted():
    with pytest.raises(ValueError, match="effect"):
        Tool(required_context, "remote_write")
