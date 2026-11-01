import httpx
import pytest
from zetra._runtime.model_diagnostics import ModelCallError

from agent_runtime import core_model, gateway, identity
from agent_runtime.core_protocol import ModelBinding
from agent_runtime.identity import AgentIdentity
from tests.fakes import agent_token, chat_response


def request():
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    from zetra._runtime.records import encode_model

    return encode_model(
        LlmRequest(
            model="openai/test-model",
            contents=[types.Content(role="user", parts=[types.Part(text="Answer.")])],
        )
    )


def bound(monkeypatch, handler):
    issued = []

    def token(req):
        issued.append(1)
        return httpx.Response(
            200, json={"access_token": agent_token(len(issued)), "expires_in": 300}
        )

    identity._IDENTITY = AgentIdentity(transport=httpx.MockTransport(token))

    def client(host, timeout, *, auth):
        return gateway.async_client_for(
            host, timeout, auth=auth, transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(core_model, "async_client_for", client)
    return core_model.GatewayModel(ModelBinding(backend="llm-main", name="test-model")), issued


async def test_one_401_refresh_preserves_fixed_route_and_identity(agent_env, monkeypatch):
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers["host"], req.headers["authorization"]))
        return (
            httpx.Response(401) if len(seen) == 1 else httpx.Response(200, json=chat_response("ok"))
        )

    model, issued = bound(monkeypatch, handler)
    await model(request(), {}, "test")
    assert len(seen) == len(issued) == 2
    assert all(req[0] == "https://10.43.250.10/models/llm-main/v1/chat/completions" for req in seen)
    assert all(req[1] == "inference.agents.internal" for req in seen)
    assert seen[0][2] != seen[1][2]


@pytest.mark.parametrize(
    "failure,reason,status",
    [
        ("redirect", "http_error", 307),
        ("disconnect", "connection_error", None),
        (500, "http_error", 500),
    ],
)
async def test_no_redirect_or_effect_retry(agent_env, monkeypatch, failure, reason, status):
    seen = []

    def handler(req):
        seen.append(req)
        if failure == "disconnect":
            raise httpx.ReadError("lost response")
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://elsewhere.invalid/steal"})
        return httpx.Response(500, json={"error": {"message": "failed"}})

    model, _ = bound(monkeypatch, handler)
    with pytest.raises(ModelCallError) as caught:
        await model(request(), {}, "test")
    assert caught.value.diagnostic.model_dump() == {"reason": reason, "http_status": status}
    assert len(seen) == 1


async def test_envelope_backend_selects_nondefault_permitted_route(agent_env, monkeypatch):
    # The Agent permits llm-a and llm-main; the legacy default selects llm-a.
    monkeypatch.setenv("AGENT_MODEL_ROUTE", "/models/llm-a")
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers["host"]))
        return httpx.Response(200, json=chat_response("ok"))

    model, _ = bound(monkeypatch, handler)
    await model(request(), {}, "test")
    assert seen == [
        ("https://10.43.250.10/models/llm-main/v1/chat/completions", "inference.agents.internal")
    ]


def test_core_transport_requires_valid_ca(agent_env, monkeypatch, tmp_path):
    ca = tmp_path / "bad-ca"
    ca.write_text("not a certificate")
    monkeypatch.setenv("AGENT_GATEWAY_CA", str(ca))
    import ssl

    with pytest.raises(ssl.SSLError):
        gateway.async_client_for(
            "AGENT_INFERENCE_HOST", 1, auth=httpx.BasicAuth("unused", "unused")
        )
