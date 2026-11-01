import copy
import json

import pytest
from aiohttp import web

from zetra.local import LocalModelConfig, local_run
from zetra.scaffold import scaffold


def test_model_configuration_identity_survives_process_serialization():
    from zetra.manifest import digest

    config = LocalModelConfig.model_validate(
        {"base_url": "http://localhost:8000/v1", "model": "test"}
    )
    restored = LocalModelConfig.model_validate(config.model_dump())
    assert digest(config.model_dump()) == digest(restored.model_dump())


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "agent"
    scaffold(root, "local_reader")
    return root


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "http://example.com/v1"},
        {"base_url": "https://secret@example.com/v1"},
        {"base_url": "https://example.com/v1?api_key=secret"},
        {"base_url": "file:///tmp/model"},
        {"max_tokens": True},
        {"timeout": float("nan")},
        {"api_key": "secret"},
    ],
)
def test_local_model_config_rejects_unsafe_or_ambiguous_inputs(change):
    with pytest.raises(ValueError):
        LocalModelConfig.model_validate(
            {"base_url": "http://127.0.0.1:8000/v1", "model": "test-model", **change}
        )


def test_invalid_model_config_does_not_echo_inline_credentials():
    with pytest.raises(ValueError) as caught:
        LocalModelConfig.model_validate(
            {
                "base_url": "http://localhost:8000/v1",
                "model": "test",
                "api_key": "must-not-echo-this-secret",
            }
        )
    assert "must-not-echo-this-secret" not in str(caught.value)


async def test_output_overlap_is_checked_after_normalizing_parent_segments(project, tmp_path):
    detour = tmp_path / "detour"
    detour.mkdir()
    with pytest.raises(ValueError, match="overlap"):
        await local_run(project, "Read", output=detour / ".." / "agent" / "new-output")
    assert not (project / "new-output").exists()


async def test_fixture_dev_runs_without_services_and_preserves_input_workspace(project, tmp_path):
    workspace = tmp_path / "input"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("local evidence")
    output = tmp_path / "result"
    result = await local_run(project, "Read guide", workspace=workspace, output=output)
    assert result["status"] == "SUCCEEDED"
    assert result["mode"] == "fixture"
    assert result["tools"] == {"read_document": "workspace_read"}
    assert [event["kind"] for event in result["trajectory"]] == ["model", "tool", "model"]
    assert result["trajectory"][1]["arguments"] == {"document_id": "guide"}
    assert result["metrics"]["input_tokens"] is None
    assert result["metrics"]["steps"] == 3
    assert result["file_contents"]["notes.txt"] == "local evidence"
    assert (workspace / "notes.txt").read_text() == "local evidence"
    assert (output / "workspace/notes.txt").read_text() == "local evidence"
    assert json.loads((output / "report.json").read_text())["status"] == "SUCCEEDED"
    assert (output / "report.json").stat().st_mode & 0o077 == 0


async def test_output_cannot_replace_existing_data_or_overlap_source(project, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep").write_text("keep")
    with pytest.raises(ValueError, match="exist"):
        await local_run(project, "Read", output=output)
    assert (output / "keep").read_text() == "keep"
    with pytest.raises(ValueError, match="overlap"):
        await local_run(project, "Read", output=project / "new-output")


async def test_input_workspace_cannot_be_a_symlink(project, tmp_path):
    target = tmp_path / "private"
    target.mkdir()
    (target / "secret.txt").write_text("must-not-copy")
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        await local_run(project, "Read", workspace=link)


async def test_nonfixture_manifest_never_implicitly_contacts_production(project):
    manifest = project / "src/local_reader/agents/document_reader/agent.yaml"
    manifest.write_text(
        manifest.read_text()
        .replace("model: fixture", "model: vllm")
        .replace("policy: workspace", "policy: vllm-chat")
    )
    with pytest.raises(ValueError, match="explicit"):
        await local_run(project, "Read")


async def test_tool_exception_preserves_unknown_outcome_without_redispatch(project):
    tool = project / "src/local_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "async def read_document(document_id: str) -> str:\n"
        '    """Read the guide."""\n'
        '    raise RuntimeError("private-secret")\n'
    )
    result = await local_run(project, "Read")
    assert result["status"] == "OUTCOME_UNKNOWN"
    attempts = [e for e in result["trajectory"] if e["kind"] == "tool"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "outcome_unknown"
    assert "private-secret" not in json.dumps(result)


async def test_source_mutation_does_not_erase_unknown_local_effect(project):
    tool = project / "src/local_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "from pathlib import Path\nasync def read_document(document_id: str) -> str:\n"
        '    """Read guide."""\n'
        "    source = Path(__file__)\n    source.write_text(source.read_text() + '\\n# changed')\n"
        "    raise RuntimeError('outcome lost after mutation')\n"
    )
    result = await local_run(project, "Read")
    assert result["status"] == "OUTCOME_UNKNOWN", result
    assert result["error_type"] == "SourceChanged"
    assert result["trajectory"][-1]["status"] == "outcome_unknown"


async def test_configured_local_http_model_uses_real_tool_and_records_usage(project, monkeypatch):
    monkeypatch.setenv("TEMPORAL_ADDRESS", "unreachable.invalid:7233")
    monkeypatch.setenv("LOCAL_MODEL_TEST_KEY", "test-private-token")
    requests = []

    async def handle(request):
        body = await request.json()
        requests.append(copy.deepcopy(body))
        assert request.headers["Authorization"] == "Bearer test-private-token"
        message = (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read-1",
                        "type": "function",
                        "function": {
                            "name": "read_document",
                            "arguments": '{"document_id":"guide"}',
                        },
                    }
                ],
            }
            if len(requests) == 1
            else {"role": "assistant", "content": "The guide says to verify metadata."}
        )
        return web.json_response(
            {
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await local_run(
            project,
            "Read the guide",
            model_config={
                "base_url": f"http://127.0.0.1:{port}/v1",
                "model": "test-model",
                "api_key_env": "LOCAL_MODEL_TEST_KEY",
            },
        )
    finally:
        await runner.cleanup()
    assert result["status"] == "SUCCEEDED", result
    assert result["mode"] == "local"
    assert len(requests) == 2
    assert any("verify metadata" in str(m) for m in requests[-1]["messages"] if m["role"] == "tool")
    assert [e["kind"] for e in result["trajectory"]] == ["model", "tool", "model"]
    assert result["metrics"]["input_tokens"] == 20
    assert result["metrics"]["output_tokens"] == 10
    assert "test-private-token" not in json.dumps(result)


def test_dev_cli_is_explicitly_local(project, capsys):
    from zetra import cli

    assert cli.main(["dev", str(project), "--input", "Read guide"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "SUCCEEDED"
    assert report["mode"] == "fixture"


async def test_model_call_limit_stops_before_second_request(project):
    requests = []

    async def handle(request):
        requests.append(await request.json())
        return web.json_response(
            {
                "id": "one",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "read-one",
                                    "type": "function",
                                    "function": {
                                        "name": "read_document",
                                        "arguments": '{"document_id":"guide"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await local_run(
            project,
            "Read guide",
            model_config={"base_url": f"http://127.0.0.1:{port}/v1", "model": "test-model"},
            model_call_limit=1,
        )
    finally:
        await runner.cleanup()
    assert len(requests) == 1
    assert result["status"] == "FAILED"
    assert result["error_type"] == "ModelCallLimit"
    assert result["metrics"]["input_tokens"] == 10
    assert result["metrics"]["steps"] == 2
