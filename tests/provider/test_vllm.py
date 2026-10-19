import copy

import pytest
from aiohttp import web
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from zetra._runtime.records import decode_model, encode_model
from zetra._runtime.vllm import MODEL, VllmExecutor

RESPONSE = {
    "id": "chatcmpl-synthetic",
    "object": "chat.completion",
    "created": 1792281485,
    "model": "Qwen3.6-35B-A3B-NVFP4",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "logprobs": None,
            "message": {
                "role": "assistant",
                "content": None,
                "reasoning": None,
                "tool_calls": [
                    {
                        "id": "call-synthetic",
                        "type": "function",
                        "function": {"name": "inspect_repository", "arguments": '{"path":"."}'},
                    }
                ],
            },
        }
    ],
    "usage": {
        "prompt_tokens": 288,
        "completion_tokens": 26,
        "total_tokens": 314,
        "completion_tokens_details": {"reasoning_tokens": 0},
    },
}


@pytest.fixture
async def endpoint():
    requests = []
    settings = {"response": copy.deepcopy(RESPONSE), "status": 200}

    async def handle(request):
        requests.append(await request.json())
        return web.json_response(settings["response"], status=settings["status"])

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield VllmExecutor(base_url=f"http://127.0.0.1:{port}/v1"), requests, settings
    finally:
        await runner.cleanup()


def request():
    return LlmRequest(
        model=MODEL,
        contents=[types.Content(role="user", parts=[types.Part(text="Inspect")])],
        config=types.GenerateContentConfig(
            system_instruction="Inspect the workspace.",
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name="inspect_repository",
                            description="List files",
                            parameters_json_schema={
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                                "required": ["path"],
                            },
                        )
                    ]
                )
            ],
        ),
    )


async def test_official_connector_preserves_call_identity_usage_and_wire_controls(endpoint):
    executor, sent, _ = endpoint
    value = await executor(encode_model(request()), {}, "operation-1")
    result = decode_model(LlmResponse, value)
    assert result.content.parts[0].function_call.id == "call-synthetic"
    assert result.content.parts[0].function_call.args == {"path": "."}
    assert result.usage_metadata.total_token_count == 314
    assert encode_model(decode_model(LlmResponse, value)) == value
    assert len(sent) == 1
    assert sent[0]["model"] == "Qwen3.6-35B-A3B-NVFP4"
    assert sent[0]["parallel_tool_calls"] is False
    assert sent[0].get("stream", False) is False
    assert sent[0]["chat_template_kwargs"]["enable_thinking"] is False
    assert sent[0]["max_tokens"] == 512


@pytest.mark.parametrize("status", [429, 500])
async def test_http_failure_sends_exactly_one_request(endpoint, status):
    executor, sent, settings = endpoint
    settings.update(
        status=status, response={"error": {"message": "injected failure", "type": "server_error"}}
    )
    from litellm import InternalServerError, RateLimitError

    with pytest.raises((InternalServerError, RateLimitError)):
        await executor(encode_model(request()), {}, "operation-1")
    assert len(sent) == 1


@pytest.mark.parametrize("failure", ["parallel", "malformed", "truncated", "unknown_tool"])
async def test_invalid_provider_result_rejected_before_acceptance(endpoint, failure):
    executor, sent, settings = endpoint
    choice = settings["response"]["choices"][0]
    calls = choice["message"]["tool_calls"]
    if failure == "parallel":
        calls.append(copy.deepcopy(calls[0]))
    if failure == "malformed":
        calls[0]["function"]["arguments"] = '{path: "."}'
    if failure == "truncated":
        choice["finish_reason"] = "length"
    if failure == "unknown_tool":
        calls[0]["function"]["name"] = "run_shell"
    with pytest.raises(ValueError):
        await executor(encode_model(request()), {}, "operation-1")
    assert len(sent) == 1


async def test_live_profile_captures_pinned_model_identity(tmp_path):
    from zetra._runtime.capture import capture_boundary
    from zetra._runtime.vllm import MANIFEST
    from zetra.testing.reporter import factory

    result = await capture_boundary(
        lambda bridge: factory(bridge, tmp_path, provider="vllm"), [], manifest=MANIFEST
    )
    assert result["operation"]["executor"] == MODEL
    assert result["operation"]["manifest_digest"] == MANIFEST


def test_live_profile_rejects_unbound_manifest():
    # Fixture sandbox transport.
    from tests.fixtures.sandbox import OpenShellSession

    with pytest.raises(ValueError, match="manifest"):
        OpenShellSession(
            None, None, session="test", manifest="demo-v1", key=b"x" * 32, provider="vllm"
        )


async def test_recorded_provider_responses_reconstruct_without_network(endpoint, tmp_path):
    from zetra._runtime.capture import capture_boundary
    from zetra._runtime.vllm import MANIFEST
    from zetra.testing.reporter import factory, registry

    executor, sent, settings = endpoint
    tape = []
    funcs = registry(tmp_path)
    for index in range(6):
        next_step = await capture_boundary(
            lambda bridge: factory(bridge, tmp_path, provider="vllm"), tape, manifest=MANIFEST
        )
        if index == 5:
            assert next_step["status"] == "done"
            break
        op = next_step["operation"]
        if op["kind"] == "model":
            choice = settings["response"]["choices"][0]
            if index == 2:
                choice["message"]["tool_calls"] = [
                    {
                        "id": "write-synthetic",
                        "type": "function",
                        "function": {
                            "name": "write_report",
                            "arguments": '{"content":"README.md inspected."}',
                        },
                    }
                ]
            elif index == 4:
                choice["finish_reason"] = "stop"
                choice["message"] = {"role": "assistant", "content": "Report ready."}
            value = await executor(op["request"], {}, op["operation_id"])
        else:
            value = await funcs[op["executor"]](op["request"]["arguments"], {}, op["operation_id"])
        tape.append({"operation": op, "outcome": value})
    assert len(sent) == 3
    before = len(sent)
    replayed = await capture_boundary(
        lambda bridge: factory(bridge, tmp_path, provider="vllm"), tape, manifest=MANIFEST
    )
    assert replayed["result"]["final_text"] == "Report ready."
    assert len(sent) == before
    assert (tmp_path / "report.md").read_text() == "README.md inspected."


@pytest.mark.parametrize(
    "field,value", [("max_output_tokens", 4096), ("temperature", 1.0), ("top_p", 0.5)]
)
async def test_manifest_generation_controls_cannot_be_overridden(endpoint, field, value):
    executor, sent, _ = endpoint
    altered = request()
    setattr(altered.config, field, value)
    with pytest.raises(ValueError, match="generation"):
        await executor(encode_model(altered), {}, "operation-1")
    assert sent == []
