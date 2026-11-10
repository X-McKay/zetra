"""pytest fixtures for agent tests, loaded wherever `zetra-agents` is installed.

    async def test_pods_are_listed(agent):
        agent.mcp.reply("pods_list_in_namespace", "demo-1 Running")
        result = await agent.call("list_pods")
        assert agent.mcp.calls == [("kubernetes", "pods_list_in_namespace", {...})]

`agent` is the agent whose directory holds the test, built by the SDK exactly as its
image builds it; a manifest and code that disagree fail there, with the SDK's own error.
"""

from pathlib import Path

import pytest


def find_agent_dir(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if (directory / "agent.yaml").is_file() and (directory / "deploy/agent.yaml").is_file():
            return directory
    return None


@pytest.fixture
def agent_dir(request) -> Path:
    """The `agents/<name>/` directory the running test belongs to."""
    found = find_agent_dir(Path(request.node.path).resolve().parent)
    if found is None:
        raise pytest.UsageError(
            f"{request.node.path} is not under an agent directory "
            "(one holding agent.yaml and deploy/agent.yaml)"
        )
    return found


@pytest.fixture
def agent(agent_dir, tmp_path_factory):
    """The agent under test: `.tools`, `.call(name, **arguments)`, `.schema(name)`,
    `.instructions`, `.manifest`, `.workspace` and the scripted `.mcp`. The test fails
    if the agent made an MCP call its Agent does not grant or the test did not script."""
    from zetra_agents import testing

    with testing.loaded(agent_dir, tmp_path_factory.mktemp("agent")) as local:
        yield local
        problems = local.mcp.problems() if local.mcp.strict else []
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)
