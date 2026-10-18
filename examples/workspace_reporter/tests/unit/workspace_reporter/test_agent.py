import asyncio

from workspace_reporter.agents.workspace_reporter.tools.workspace import tools_for


def test_tools_are_scoped(tmp_path):
    inspect_workspace, write_summary = tools_for(tmp_path)
    assert asyncio.run(inspect_workspace(".")) == {"files": []}
    asyncio.run(write_summary("hello"))
    assert (tmp_path / "summary.md").read_text() == "hello"
