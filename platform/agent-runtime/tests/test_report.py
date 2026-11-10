"""Evidence files are content-addressed and bounded; the report tool cites only what was
recorded."""

import hashlib
import json

import pytest

from agent_runtime import step
from agent_runtime.report import Evidence, EvidenceError, EvidenceLimit, report_tool


def test_record_writes_a_content_addressed_file_and_returns_its_path(tmp_path):
    evidence = Evidence(tmp_path)
    out = evidence.record("pods", {"tool": "pods_list", "text": "demo"})
    path = tmp_path / out["evidence"]
    raw = path.read_bytes()
    assert out == {"tool": "pods_list", "text": "demo", "evidence": out["evidence"]}
    assert path.name == f"pods-{hashlib.sha256(raw).hexdigest()}.json"
    assert json.loads(raw) == {"tool": "pods_list", "text": "demo"}
    # The same observation is the same file; a different one is another.
    assert evidence.record("pods", {"text": "demo", "tool": "pods_list"}) == out
    evidence.record("pods", {"tool": "pods_list", "text": "changed"})
    assert len(evidence.links()) == 2
    assert all(link.startswith("evidence/pods-") for link in evidence.links())


@pytest.mark.parametrize("label", ["", "Pods", "../x", "a/b", "-x", "x" * 82, None])
def test_record_refuses_labels_that_are_not_plain_names(tmp_path, label):
    with pytest.raises(ValueError):
        Evidence(tmp_path).record(label, {})
    assert not (tmp_path / "evidence").exists()


def test_record_is_bounded_in_size_and_count(tmp_path):
    evidence = Evidence(tmp_path, limit=2, max_bytes=200)
    with pytest.raises(ValueError, match="exceeds 200 bytes"):
        evidence.record("big", {"text": "x" * 200})
    with pytest.raises(TypeError):
        evidence.record("list", ["not", "an", "object"])
    evidence.record("a", {"n": 1})
    evidence.record("a", {"n": 2})
    with pytest.raises(EvidenceLimit):
        evidence.record("a", {"n": 3})
    assert evidence.record("a", {"n": 1})["evidence"] in evidence.links()  # already recorded
    assert len(evidence.links()) == 2


def test_files_refuses_anything_record_did_not_write(tmp_path):
    evidence = Evidence(tmp_path)
    first = tmp_path / evidence.record("a", {"n": 1})["evidence"]
    first.write_text("{}")  # no longer the bytes its name commits to
    with pytest.raises(EvidenceError, match="does not match its name"):
        evidence.files()
    first.unlink()
    (tmp_path / "evidence" / "notes.txt").write_text("x")
    with pytest.raises(EvidenceError, match="not written by record"):
        evidence.files()
    (tmp_path / "evidence" / "notes.txt").unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    name = "a-" + hashlib.sha256(b"{}").hexdigest() + ".json"
    (tmp_path / "evidence" / name).symlink_to(target)
    with pytest.raises(EvidenceError, match="not written by record"):
        evidence.files()


def test_a_symlinked_evidence_directory_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "evidence").symlink_to(tmp_path / "real")
    with pytest.raises(EvidenceError, match="symbolic link"):
        Evidence(tmp_path).record("a", {})


async def test_a_report_never_writes_through_a_link(tmp_path):
    (tmp_path / "state.json").write_text("kept")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.md").symlink_to(tmp_path / "state.json")
    assert await report_tool(workspace).fn("All good.") == {"path": "report.md"}
    assert (tmp_path / "state.json").read_text() == "kept"
    assert not (workspace / "report.md").is_symlink()
    assert (workspace / "report.md").read_text() == "All good.\n"


async def test_report_without_evidence_is_a_bounded_text(tmp_path):
    tool = report_tool(tmp_path)
    assert tool.effect == "workspace_write" and tool.fn.__name__ == "write_report"
    assert step.schema(tool)["function"]["parameters"]["required"] == ["content"]
    assert "at most 150 words" in step.schema(tool)["function"]["description"]
    assert "error" in await tool.fn("")
    assert "error" in await tool.fn("word " * 151)
    assert "error" in await tool.fn(None)
    assert not (tmp_path / "report.md").exists()
    assert await tool.fn("All good.  \n") == {"path": "report.md"}
    assert (tmp_path / "report.md").read_text() == "All good.\n"
    assert await report_tool(tmp_path, max_bytes=10).fn("far too long a report") == {
        "error": "Keep the report below 10 UTF-8 bytes."
    }


async def test_report_with_evidence_appends_exact_links(tmp_path):
    evidence = Evidence(tmp_path)
    tool = report_tool(tmp_path, evidence)
    assert await tool.fn("Nothing observed yet.") == {
        "error": "Gather evidence before writing the report."
    }
    link = evidence.record("pods", {"text": "demo"})["evidence"]
    assert await tool.fn(f"See {link}.") == {
        "error": "Omit evidence paths from the narrative; exact links are appended."
    }
    assert await tool.fn("The pod is failing.") == {"path": "report.md"}
    assert (tmp_path / "report.md").read_text() == (
        f"The pod is failing.\n\n## Evidence\n\n- [{link}]({link})\n"
    )


async def test_report_refuses_evidence_it_cannot_verify(tmp_path):
    evidence = Evidence(tmp_path)
    (tmp_path / evidence.record("pods", {"text": "demo"})["evidence"]).write_text("edited")
    with pytest.raises(EvidenceError):
        await report_tool(tmp_path, evidence).fn("The pod is failing.")
    assert not (tmp_path / "report.md").exists()


def test_the_description_can_be_replaced(tmp_path):
    tool = report_tool(tmp_path, description="Write the incident summary.")
    assert step.schema(tool)["function"]["description"] == "Write the incident summary."
