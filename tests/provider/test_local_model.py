import copy
import json

import pytest
from aiohttp import web
from google.adk.models.llm_response import LlmResponse

from tests.provider.test_vllm import RESPONSE, request
from zetra._runtime.records import decode_model, encode_model

MODEL = "openai/developer-model"
KEY = "test-local-endpoint-secret"


@pytest.fixture
async def local_endpoint():
    sent = []
    settings = {"response": copy.deepcopy(RESPONSE), "status": 200}

    async def handle(req):
        sent.append(
            {
                "path": req.path,
                "authorization": req.headers.get("Authorization"),
                "host": req.headers.get("Host"),
                "body": await req.json(),
            }
        )
        if "raw_body" in settings:
            return web.Response(text=settings["raw_body"], content_type="application/json")
        return web.json_response(settings["response"], status=settings["status"])

    app = web.Application()
    app.router.add_post("/explicit/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/explicit/v1", sent, settings
    finally:
        await runner.cleanup()


def model_request():
    value = request()
    value.model = MODEL
    return encode_model(value)


async def test_explicit_local_model_settings_and_credentials(local_endpoint):
    from zetra._runtime.chat_model import ChatExecutor

    base_url, sent, _ = local_endpoint
    executor = ChatExecutor(
        base_url=base_url, model=MODEL, api_key=KEY, max_tokens=123, timeout=5, temperature=0.25
    )
    result = await executor(model_request(), {}, "local-operation")
    assert len(sent) == 1
    assert sent[0]["path"] == "/explicit/v1/chat/completions"
    assert sent[0]["authorization"] == f"Bearer {KEY}"
    body = sent[0]["body"]
    assert body["model"] == "developer-model"
    assert body["max_tokens"] == 123
    assert body["temperature"] == 0.25
    assert body["parallel_tool_calls"] is False
    assert body.get("stream", False) is False
    assert "chat_template_kwargs" not in body
    assert KEY not in json.dumps(result)
    decoded = decode_model(LlmResponse, result)
    assert decoded.content.parts[0].function_call.id == "call-synthetic"
    assert decoded.usage_metadata.total_token_count == 314


async def test_explicit_extra_body_is_forwarded(local_endpoint):
    from zetra._runtime.chat_model import ChatExecutor

    base_url, sent, _ = local_endpoint
    executor = ChatExecutor(
        base_url=base_url,
        model=MODEL,
        api_key=KEY,
        extra_body={"chat_template_kwargs": {"enable_thinking": True}},
    )
    await executor(model_request(), {}, "local-operation")
    assert sent[0]["body"]["chat_template_kwargs"] == {"enable_thinking": True}


async def test_injected_client_preserves_gateway_auth_and_host(local_endpoint):
    import httpx
    from openai import AsyncOpenAI

    from zetra._runtime.chat_model import ChatExecutor

    base_url, sent, _ = local_endpoint

    class GatewayAuth(httpx.Auth):
        def auth_flow(self, request):
            request.headers["Authorization"] = "Bearer reviewed-gateway-identity"
            yield request

    def client_factory():
        return AsyncOpenAI(
            base_url=base_url,
            api_key="placeholder",
            max_retries=0,
            http_client=httpx.AsyncClient(
                auth=GatewayAuth(),
                headers={"Host": "inference.agents.internal"},
                follow_redirects=False,
            ),
        )

    executor = ChatExecutor(
        base_url=base_url, model=MODEL, api_key="placeholder", client_factory=client_factory
    )
    response = await executor(model_request(), {}, "gateway-operation")
    assert len(sent) == 1
    assert sent[0]["host"] == "inference.agents.internal"
    assert sent[0]["authorization"] == "Bearer reviewed-gateway-identity"
    assert decode_model(LlmResponse, response).content.parts[0].function_call.id == "call-synthetic"


@pytest.mark.parametrize("failure", [429, 500, "unknown_tool", "parallel"])
async def test_invalid_or_failed_local_response_is_not_retried(local_endpoint, failure):
    from litellm import InternalServerError, RateLimitError

    from zetra._runtime.chat_model import ChatExecutor

    base_url, sent, settings = local_endpoint
    if isinstance(failure, int):
        settings.update(
            status=failure, response={"error": {"message": "failure", "type": "server_error"}}
        )
    else:
        calls = settings["response"]["choices"][0]["message"]["tool_calls"]
        if failure == "unknown_tool":
            calls[0]["function"]["name"] = "unregistered_tool"
        else:
            calls.append(copy.deepcopy(calls[0]))
    executor = ChatExecutor(base_url=base_url, model=MODEL, api_key=KEY)
    with pytest.raises((ValueError, InternalServerError, RateLimitError)):
        await executor(model_request(), {}, "local-operation")
    assert len(sent) == 1


@pytest.mark.parametrize("alteration", ["model", "transport", "generation"])
async def test_local_request_cannot_override_binding(local_endpoint, alteration):
    from google.genai import types

    from zetra._runtime.chat_model import ChatExecutor

    base_url, sent, _ = local_endpoint
    value = request()
    value.model = MODEL
    if alteration == "model":
        value.model = "openai/other-model"
    elif alteration == "transport":
        value.config.http_options = types.HttpOptions(base_url="https://invalid.example")
    else:
        value.config.temperature = 1
    executor = ChatExecutor(base_url=base_url, model=MODEL, api_key=KEY)
    with pytest.raises(ValueError):
        await executor(encode_model(value), {}, "local-operation")
    assert sent == []


@pytest.mark.parametrize(
    "failure,reason,status",
    [
        (429, "http_error", 429),
        (503, "http_error", 503),
        ("length", "response_truncated", None),
        ("content_filter", "response_filtered", None),
        ("parallel", "parallel_tool_calls", None),
        ("unknown_tool", "unknown_tool", None),
        ("bad_json", "invalid_tool_arguments", None),
        ("array_args", "invalid_tool_arguments", None),
        ("empty", "empty_response", None),
        ("malformed_body", "invalid_provider_response", None),
    ],
)
async def test_model_failures_have_bounded_private_diagnostics(
    local_endpoint, failure, reason, status
):
    from zetra._runtime.chat_model import ChatExecutor

    base_url, sent, settings = local_endpoint
    secret = "private-provider-payload-and-credential"
    choice = settings["response"]["choices"][0]
    calls = choice["message"]["tool_calls"]
    if isinstance(failure, int):
        settings.update(
            status=failure, response={"error": {"message": secret, "type": "server_error"}}
        )
    elif failure in {"length", "content_filter"}:
        choice["finish_reason"] = failure
    elif failure == "parallel":
        calls.append(copy.deepcopy(calls[0]))
    elif failure == "unknown_tool":
        calls[0]["function"]["name"] = secret
    elif failure in {"bad_json", "array_args"}:
        calls[0]["function"]["arguments"] = (
            secret if failure == "bad_json" else '["' + secret + '"]'
        )
    elif failure == "empty":
        choice.update(finish_reason="stop", message={"role": "assistant", "content": None})
    elif failure == "malformed_body":
        settings["raw_body"] = "{" + secret
    with pytest.raises(Exception) as caught:
        await ChatExecutor(base_url=base_url, model=MODEL, api_key=KEY)(
            model_request(), {}, "operation"
        )
    diagnostic = caught.value.diagnostic.model_dump()
    assert diagnostic == {"reason": reason, "http_status": status}
    assert secret not in json.dumps(diagnostic)
    assert KEY not in json.dumps(diagnostic)
    assert len(sent) == 1


@pytest.mark.parametrize(
    "failure,reason", [("timeout", "provider_timeout"), ("connection", "connection_error")]
)
async def test_transport_failures_keep_reason_without_request_details(failure, reason):
    import httpx
    from openai import AsyncOpenAI

    from zetra._runtime.chat_model import ChatExecutor

    calls = []

    async def fail(request):
        calls.append(request.method)
        error = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
        raise error("sensitive-request-details", request=request)

    def client():
        return AsyncOpenAI(
            base_url="http://localhost/v1",
            api_key=KEY,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)),
        )

    with pytest.raises(ValueError) as caught:
        await ChatExecutor(
            base_url="http://localhost/v1", model=MODEL, api_key=KEY, client_factory=client
        )(model_request(), {}, "operation")
    assert caught.value.diagnostic.model_dump() == {"reason": reason, "http_status": None}
    assert str(caught.value) == reason
    assert calls == ["POST"]
