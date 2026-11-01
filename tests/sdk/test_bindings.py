import asyncio
import json
import subprocess
import sys

import pytest
from google.adk.agents import LlmAgent

from zetra import AgentContext, Tool
from zetra.local import local_run
from zetra.scaffold import scaffold


def fixtures():
    return {
        "schema_version": 1,
        "calls": [
            {
                "backend": "documents",
                "tool": "read",
                "arguments": {"document_id": "guide"},
                "result": {"text": "Approved guide."},
            }
        ],
    }


async def test_mcp_is_bound_only_during_a_registered_remote_executor(tmp_path):
    calls = []

    async def transport(backend, tool, arguments):
        calls.append((backend, tool, arguments))
        return {"text": "Approved guide."}

    ctx = AgentContext(None, workspace=tmp_path, model="fixture", mcp_binding=transport)

    async def read_document(document_id: str):
        """Read an approved document."""
        return await ctx.mcp("documents", "read", {"document_id": document_id})

    ctx.seal(
        LlmAgent(
            name="reader", model=ctx.model, tools=ctx.tools(Tool(read_document, "remote_read"))
        )
    )
    with pytest.raises(ValueError, match="remote_read"):
        await ctx.mcp("documents", "read", {"document_id": "guide"})
    result = await ctx.executors()["read_document"]({"document_id": "guide"}, {}, "op")
    assert result == {"text": "Approved guide."}
    assert calls == [("documents", "read", {"document_id": "guide"})]
    with pytest.raises(ValueError, match="remote_read"):
        await ctx.mcp("documents", "read", {"document_id": "guide"})


async def test_workspace_tool_cannot_borrow_mcp_and_background_scope_expires(tmp_path):
    calls = []

    async def transport(*args):
        calls.append(args)
        return {"text": "not authorized"}

    ctx = AgentContext(None, workspace=tmp_path, model="fixture", mcp_binding=transport)
    gate = asyncio.Event()
    tasks = []

    async def workspace_tool():
        """Read local workspace."""
        return await ctx.mcp("documents", "read", {})

    async def remote_tool():
        """Read remote document."""

        async def late():
            await gate.wait()
            return await ctx.mcp("documents", "read", {})

        tasks.append(asyncio.create_task(late()))
        return "done"

    ctx.seal(
        LlmAgent(
            name="reader",
            model=ctx.model,
            tools=ctx.tools(
                Tool(workspace_tool, "workspace_read"), Tool(remote_tool, "remote_read")
            ),
        )
    )
    with pytest.raises(ValueError, match="remote_read"):
        await ctx.executors()["workspace_tool"]({}, {}, "workspace")
    await ctx.executors()["remote_tool"]({}, {}, "remote")
    gate.set()
    with pytest.raises(ValueError, match="remote_read"):
        await tasks[0]
    assert calls == []


async def test_mcp_fixtures_are_exact_isolated_and_never_fallback():
    from zetra.bindings import MCPFixtures

    data = fixtures()
    bound = MCPFixtures(data)
    data["calls"][0]["result"]["text"] = "mutated source"
    result = await bound.call("documents", "read", {"document_id": "guide"})
    assert result == {"text": "Approved guide."}
    result["text"] = "mutated consumer"
    assert await bound.call("documents", "read", {"document_id": "guide"}) == {
        "text": "Approved guide."
    }
    for backend, tool, args in [
        ("private", "read", {"document_id": "guide"}),
        ("documents", "write", {"document_id": "guide"}),
        ("documents", "read", {"document_id": "secret"}),
    ]:
        with pytest.raises(ValueError, match="fixture"):
            await bound.call(backend, tool, args)


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "version", "nonfinite", "oversize"])
def test_mcp_fixtures_reject_ambiguous_or_unbounded_inputs(mutation):
    from zetra.bindings import MCPFixtures

    data = fixtures()
    if mutation == "duplicate":
        data["calls"].append(data["calls"][0].copy())
    elif mutation == "unknown":
        data["fallback_url"] = "https://example.invalid"
    elif mutation == "version":
        data["schema_version"] = True
    elif mutation == "nonfinite":
        data["calls"][0]["result"] = float("nan")
    else:
        data["calls"][0]["result"] = "x" * 65537
    with pytest.raises(ValueError):
        MCPFixtures(data)


def mcp_project(path):
    scaffold(path, "mcp-reader")
    factory = path / "src/mcp_reader/agents/document_reader/factory.py"
    factory.write_text(
        "from google.adk.agents import LlmAgent\nfrom zetra import Tool\n"
        "def create_agent(ctx):\n"
        "    async def read_document(document_id: str):\n"
        '        """Read an approved document."""\n'
        "        return await ctx.mcp('documents', 'read', {'document_id': document_id})\n"
        "    return LlmAgent(name='document_reader', model=ctx.model, instruction=ctx.instruction, tools=ctx.tools(Tool(read_document, 'remote_read')))\n"
    )
    return path


async def test_local_child_uses_explicit_mcp_fixtures_and_reports_identity(tmp_path):
    project = mcp_project(tmp_path / "agent")
    config = fixtures()
    result = await local_run(project, "Read guide", mcp_fixtures=config)
    assert result["status"] == "SUCCEEDED", result
    assert result["tools"] == {"read_document": "remote_read"}
    assert result["trajectory"][1]["result"] == {"text": "Approved guide."}
    assert result["mcp_binding"]["mode"] == "fixture"
    assert len(result["mcp_binding"]["sha256"]) == 64
    changed = json.loads(json.dumps(config))
    changed["calls"][0]["result"]["text"] = "Different guide."
    other = await local_run(project, "Read guide", mcp_fixtures=changed)
    assert other["mcp_binding"]["sha256"] != result["mcp_binding"]["sha256"]
    unbound = await local_run(project, "Read guide")
    assert unbound["status"] == "OUTCOME_UNKNOWN"
    assert unbound["mcp_binding"]["mode"] == "unbound"


async def test_fixture_evaluation_accounts_for_bound_mcp_results(tmp_path):
    from zetra.evaluation import evaluate

    project = mcp_project(tmp_path / "agent")
    report = await evaluate(project, mode="fixture", mcp_fixtures=fixtures())
    assert report["status"] == "PASSED", report
    assert report["counts"] == {"planned": 1, "passed": 1, "failed": 0, "error": 0}
    assert report["mcp_binding"]["mode"] == "fixture"
    assert report["cases"][0]["trajectory"][1]["result"] == {"text": "Approved guide."}
    with pytest.raises(ValueError, match="MCP"):
        await evaluate(project, mode="sandbox", mcp_fixtures=fixtures())


def test_cli_explicit_mcp_fixtures_run_offline(tmp_path):
    project = mcp_project(tmp_path / "agent")
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(fixtures()))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "zetra",
            "dev",
            str(project),
            "--input",
            "Read guide",
            "--mcp-fixtures",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "SUCCEEDED"
    assert report["mcp_binding"]["mode"] == "fixture"


async def test_quality_forwards_explicit_fixture_binding_and_marks_evidence(tmp_path):
    from zetra.quality import run_quality

    project = mcp_project(tmp_path / "agent")
    contract_path = project / "evals/document_reader/quality.json"
    contract = json.loads(contract_path.read_text())
    contract["tools"]["read_document"] = "remote_read"
    contract_path.write_text(json.dumps(contract))
    report = await run_quality(project, mcp_fixtures=fixtures())
    assert report["status"] == "PASSED", report
    assert report["mcp_binding"] == report["evaluation"]["mcp_binding"]
    assert report["mcp_binding"]["mode"] == "fixture"
    assert report["evaluation"]["cases"][0]["tools"] == {"read_document": "remote_read"}
    unbound = await run_quality(project)
    assert unbound["status"] == "FAILED"
