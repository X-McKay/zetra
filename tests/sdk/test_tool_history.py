import pytest

from zetra import AgentContext
from zetra._runtime.artifacts import Receipt
from zetra._runtime.definition import Definition
from zetra._runtime.runtime import Runtime
from zetra.manifest import load_agent
from zetra.scaffold import scaffold


def test_history_is_empty_without_runtime_binding(tmp_path):
    ctx = AgentContext(None, workspace=tmp_path, model="fixture")
    assert ctx.prior_tool_results("read_document") == ()


def test_history_filters_unsuccessful_or_other_operations_and_is_immutable(tmp_path):
    def entry(kind="tool", name="read_document", status="succeeded"):
        return {
            "operation": {
                "kind": kind,
                "executor": name,
                "request": {"arguments": {"id": "guide"}},
            },
            "outcome": {"status": status, "value": {"skill": {"name": "grounded_answer"}}},
        }

    history = [entry(), entry(status="known_error"), entry(name="other"), entry(kind="model")]
    ctx = AgentContext(None, workspace=tmp_path, model="fixture", history_binding=lambda: history)
    results = ctx.prior_tool_results("read_document")
    assert results == (
        {"arguments": {"id": "guide"}, "result": {"skill": {"name": "grounded_answer"}}},
    )
    with pytest.raises(TypeError):
        results[0]["result"]["skill"]["name"] = "forged"
    assert history[0]["outcome"]["value"]["skill"]["name"] == "grounded_answer"


async def test_history_uses_only_accepted_tape_and_survives_cold_reconstruction(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    scaffold(project, "history_reader")
    factory = project / "src/history_reader/agents/document_reader/factory.py"
    factory.write_text('''from google.adk.agents import LlmAgent
from zetra import Tool

def create_agent(ctx):
    async def read_document():
        """Read the guide."""
        return {"document_id": "guide", "skill": {"name": "grounded_answer"}}
    async def write_answer():
        """Write after an accepted read."""
        reads = ctx.prior_tool_results("read_document")
        if not reads:
            return {"status": "READ_REQUIRED"}
        (ctx.workspace / "answer.md").write_text(reads[-1]["result"]["document_id"])
        return {"status": "WRITTEN"}
    return LlmAgent(name="document_reader", model=ctx.model, instruction=ctx.instruction,
        tools=ctx.tools(Tool(read_document, "workspace_read"), Tool(write_answer, "workspace_write")))
''')
    monkeypatch.syspath_prepend(str(project / "src"))
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()

    def fresh():
        definition = Definition(load_agent(project).model_dump(), runtime_root / "workspace")
        runtime = Runtime(
            runtime_root,
            session="history",
            manifest="history-v1",
            key=b"x" * 32,
            message="Read and write",
            definition=definition,
        )
        return runtime, definition

    runtime, definition = fresh()
    position = runtime.tape.empty(runtime.workspace.capture())
    try:
        for cursor in range(6):
            command = {
                "binding": {"session": "history"},
                "position": position.model_dump(),
                "pending": None,
            }
            step = await runtime.handle(runtime.authority.sign("advance", command))
            if cursor == 5:
                assert step["status"] == "done"
                assert (runtime.workdir / "answer.md").read_text() == "guide"
                break
            result = await runtime.handle(
                runtime.authority.sign(
                    "execute",
                    {**command, "operation": step["operation"], "attempt_id": str(cursor)},
                )
            )
            if cursor == 1:
                # A successful candidate read is still not accepted progress.
                assert definition.context(None).prior_tool_results("read_document") == ()
            position = Receipt.model_validate(result["receipt"]).position
            await runtime.close()
            runtime, definition = fresh()
    finally:
        await runtime.close()
