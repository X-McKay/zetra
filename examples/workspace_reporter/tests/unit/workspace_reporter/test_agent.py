import asyncio

import pytest
from workspace_reporter.agents.workspace_reporter.tools.workspace import tools_for


def test_tools_are_scoped(tmp_path):
    inspect_workspace, write_summary = tools_for(tmp_path)
    assert asyncio.run(inspect_workspace(".")) == {"files": []}
    asyncio.run(write_summary("hello"))
    assert (tmp_path / "summary.md").read_text() == "hello"


def test_rejected_path_does_not_disclose_parent_files(tmp_path):
    parent_secret = tmp_path.parent / "private.txt"
    parent_secret.write_text("private")
    inspect_workspace, _ = tools_for(tmp_path)
    with pytest.raises(ValueError, match="workspace root"):
        asyncio.run(inspect_workspace(".."))
    assert asyncio.run(inspect_workspace(".")) == {"files": []}


def test_rejected_read_does_not_prevent_later_valid_read(tmp_path):
    inspect_workspace, _ = tools_for(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(inspect_workspace("../private.txt"))
    (tmp_path / "visible.txt").write_text("visible")
    assert asyncio.run(inspect_workspace(".")) == {"files": ["visible.txt"]}


def test_summary_content_cannot_select_output_path(tmp_path):
    _, write_summary = tools_for(tmp_path)
    result = asyncio.run(write_summary("../../private.txt"))
    assert result == {"path": "summary.md"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["summary.md"]
    assert (tmp_path / "summary.md").read_text() == "../../private.txt"


def test_summary_failure_does_not_write_an_alternate_file(tmp_path):
    _, write_summary = tools_for(tmp_path)
    (tmp_path / "summary.md").mkdir()
    with pytest.raises(IsADirectoryError):
        asyncio.run(write_summary("hello"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["summary.md"]
