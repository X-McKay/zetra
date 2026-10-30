import json

import httpx
import openai
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from agent_runtime import identity, mcp
from agent_runtime.gateway import BudgetExhausted, PolicyDenied
from agent_runtime.identity import AgentIdentity
from agent_runtime.models import chat_client
from tests.fakes import agent_token, chat_response


def token_transport() -> httpx.MockTransport:
    issued = []

    def handler(request):
        issued.append(1)
        return httpx.Response(
            200, json={"access_token": agent_token(len(issued)), "expires_in": 300}
        )

    return httpx.MockTransport(handler)


def fake_mcp_server() -> FastMCP:
    server = FastMCP(
        "kubernetes",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool()
    def pods_list_in_namespace(namespace: str) -> str:
        return f"pods in {namespace}"

    @server.tool()
    def big() -> str:
        return "é" * 100_000

    return server


class Gate:
    """ASGI front for the fake MCP server, playing the gateway route /<backend>/mcp."""

    def __init__(self, app, status: int | None = None) -> None:
        self.app, self.status, self.seen = app, status, []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        self.seen.append(
            (
                scope["method"],
                scope["path"],
                headers.get(b"host"),
                headers.get(b"authorization", b"")[:7],
            )
        )
        if self.status:
            await send({"type": "http.response.start", "status": self.status, "headers": []})
            return await send({"type": "http.response.body", "body": b""})
        assert scope["path"] == "/kubernetes/mcp"
        return await self.app({**scope, "path": "/mcp", "raw_path": b"/mcp"}, receive, send)


@pytest.fixture
def server(agent_env):
    identity._IDENTITY = AgentIdentity(transport=token_transport())
    return fake_mcp_server()


async def call(server, gate_status=None, *args, **kw):
    """Run one call_tool against the fake server behind a Gate; returns (result, gate)."""
    gate = Gate(server.streamable_http_app(), status=gate_status)
    async with server.session_manager.run():
        return await mcp.call_tool(*args, transport=httpx.ASGITransport(gate), **kw), gate


async def test_mcp_call_goes_to_the_gateway_route_with_the_bearer(server):
    out, gate = await call(server, None, "kubernetes", "pods_list_in_namespace", {"namespace": "a"})
    assert out == {"tool": "pods_list_in_namespace", "backend": "kubernetes", "text": "pods in a"}
    assert gate.seen and all(
        h == b"mcp.agents.internal" and a == b"Bearer " for _, _, h, a in gate.seen
    )


async def test_mcp_result_is_truncated_before_it_enters_state(server):
    out, _ = await call(server, None, "kubernetes", "big", {})
    assert len(out["text"].encode()) <= mcp.MAX_RESULT
    assert out["text"] == "é" * (mcp.MAX_RESULT // 2)


async def test_mcp_arguments_over_32_kib_are_refused_before_sending(server):
    seen = []
    transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(500))
    with pytest.raises(ValueError, match="32 KiB"):
        await mcp.call_tool(
            "kubernetes", "pods_list_in_namespace", {"namespace": "a" * 32768}, transport=transport
        )
    assert seen == []


@pytest.mark.parametrize("backend", ["../v1/token", "Kubernetes", "", "a/b"])
async def test_mcp_backend_name_is_validated(server, backend):
    with pytest.raises(ValueError):
        await mcp.call_tool(backend, "t", {})


@pytest.mark.parametrize("status,error", [(403, PolicyDenied), (429, BudgetExhausted)])
async def test_mcp_403_is_policy_and_429_is_budget(server, status, error):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status)

    with pytest.raises(error):
        await mcp.call_tool(
            "kubernetes",
            "pods_list_in_namespace",
            {"namespace": "a"},
            transport=httpx.MockTransport(handler),
            timeout=5,
        )
    assert len(seen) == 1  # never retried


def inference_with(monkeypatch, handler):
    from agent_runtime import gateway

    identity._IDENTITY = AgentIdentity(transport=token_transport())
    client = gateway.client_for("AGENT_INFERENCE_HOST", 5.0, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(gateway, "_INFERENCE", client)


def test_model_call_uses_the_route_and_the_agent_token(agent_env, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=chat_response("hi"))

    inference_with(monkeypatch, handler)
    resp = chat_client().chat.completions.create(
        model="m", messages=[{"role": "user", "content": "x"}]
    )
    assert resp.choices[0].message.content == "hi"
    (req,) = seen
    assert str(req.url) == "https://10.43.250.10/models/llm-main/v1/chat/completions"
    assert req.headers["host"] == "inference.agents.internal"
    assert req.headers["authorization"] == "Bearer " + agent_token(1)  # never the placeholder
    assert json.loads(req.content)["model"] == "m"


@pytest.mark.parametrize(
    "status,error", [(403, openai.PermissionDeniedError), (429, openai.RateLimitError)]
)
def test_model_403_and_429_are_not_retried(agent_env, monkeypatch, status, error):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={"error": {"message": "no"}})

    inference_with(monkeypatch, handler)
    with pytest.raises(error):
        chat_client().chat.completions.create(model="m", messages=[])
    assert len(seen) == 1
