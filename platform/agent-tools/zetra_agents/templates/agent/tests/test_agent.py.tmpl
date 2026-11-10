"""`agent` is this agent, built by the SDK as its image builds it (zetra-agents provides
the fixture). `agent.mcp` stands in for the gateway: script what a granted MCP tool
returns with `agent.mcp.reply(tool, text)` and read what was called from `agent.mcp.calls`.
The last test runs the whole scripted session in scenarios/smoke.yaml, as `just run` does.
"""

from zetra_agents import local


def test_the_model_is_given_the_instructions_and_the_report_tool(agent):
    assert "Report ready" in agent.instructions
    assert set(agent.tools) == {"write_report"}
    assert agent.schema("write_report")["function"]["parameters"]["required"] == ["content"]


async def test_report_is_bounded(agent):
    assert "error" in await agent.call("write_report", content="")
    assert "error" in await agent.call("write_report", content="word " * 151)
    assert await agent.call("write_report", content="All good.") == {"path": "report.md"}
    assert (agent.workspace / "report.md").read_text() == "All good.\n"


def test_the_smoke_scenario_ends_with_a_report(agent_dir, tmp_path):
    result = local.run(agent_dir, "smoke", out=tmp_path / "run")
    assert result.ok, (result.outcome, result.detail, result.problems)
    assert result.report.read_text() == "The starter agent is running.\n"
