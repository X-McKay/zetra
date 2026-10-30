import json
import os
import stat
import time

import httpx
import pytest

from agent_runtime import gateway, identity
from agent_runtime.identity import AgentIdentity, BearerAuth
from tests.fakes import agent_token, assert_redacted, projected_token


class TokenRoute:
    """MockTransport handler for the token route; counts and records requests."""

    def __init__(self, **kw) -> None:
        self.requests: list[httpx.Request] = []
        self.kw = kw

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": agent_token(len(self.requests), **self.kw), "expires_in": 300},
        )

    def forms(self) -> list[dict]:
        from urllib.parse import parse_qsl

        return [dict(parse_qsl(r.content.decode())) for r in self.requests]


def test_t4_token_request_is_client_credentials_through_the_gateway(agent_env):
    route = TokenRoute()
    tok = AgentIdentity(transport=httpx.MockTransport(route)).token()
    (req,) = route.requests
    assert str(req.url) == "https://10.43.250.10/v1/token"
    assert req.headers["host"] == "token.agents.internal"
    assert route.forms()[0] == {
        "grant_type": "client_credentials",
        "client_id": "agent-tenant-test-conformance",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": projected_token(0),
        "scope": "agent-gateway",
    }
    cache = identity.token_cache()
    assert json.loads(cache.read_text())["access_token"] == tok
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache.parent.stat().st_mode) == 0o700


def test_token_file_is_0600_before_any_byte_is_written(agent_env, monkeypatch):
    modes = []
    real = os.fdopen

    def spy(fd, *a, **kw):
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        assert os.fstat(fd).st_size == 0
        return real(fd, *a, **kw)

    monkeypatch.setattr(identity.os, "fdopen", spy)
    AgentIdentity(transport=httpx.MockTransport(TokenRoute())).token()
    assert modes == [0o600]


@pytest.mark.parametrize("remaining,refreshes", [(121, 0), (119, 1)])
def test_refresh_threshold_is_120_seconds(agent_env, remaining, refreshes):
    cached = agent_token(99)
    identity.write_private(
        identity.token_cache(), json.dumps({"access_token": cached, "exp": time.time() + remaining})
    )
    route = TokenRoute()
    tok = AgentIdentity(transport=httpx.MockTransport(route)).token()
    assert len(route.requests) == refreshes
    assert (tok == cached) == (refreshes == 0)


def test_persisted_token_is_reused_by_the_next_process(agent_env):
    route = TokenRoute()
    first = AgentIdentity(transport=httpx.MockTransport(route)).token()
    second = AgentIdentity(transport=httpx.MockTransport(route)).token()  # a fresh process
    assert first == second and len(route.requests) == 1


def test_projected_token_is_reread_on_every_refresh(agent_env):
    route = TokenRoute()
    ident = AgentIdentity(transport=httpx.MockTransport(route))
    ident.token()
    with open(agent_env["AGENT_TOKEN_PATH"], "w") as f:
        f.write(projected_token(1))  # the kubelet rotated it
    ident.invalidate()
    ident.token()
    assert [f["client_assertion"] for f in route.forms()] == [
        projected_token(0),
        projected_token(1),
    ]


def test_token_not_for_the_gateway_is_refused(agent_env):
    route = TokenRoute(aud=("account",))
    with pytest.raises(RuntimeError, match="not for the gateway"):
        AgentIdentity(transport=httpx.MockTransport(route)).token()
    assert not identity.token_cache().exists()


def test_token_route_error_names_only_the_status_and_code(agent_env):
    leaked = agent_token(7)

    def handler(request):
        return httpx.Response(401, json={"error": "invalid_client", "echo": leaked})

    with pytest.raises(RuntimeError) as e:
        AgentIdentity(transport=httpx.MockTransport(handler)).token()
    assert str(e.value) == "token endpoint returned 401: invalid_client"


def test_gateway_url_must_be_https(agent_env, monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_URL", "http://10.43.250.10:80")
    with pytest.raises(RuntimeError, match="https"):
        identity.gateway_url()


def test_tls_verification_is_never_disabled(agent_env, tmp_path, monkeypatch):
    from tests.fakes import make_ca

    ca, _, _ = make_ca(tmp_path)
    monkeypatch.setenv("AGENT_GATEWAY_CA", str(ca))
    ctx = identity.tls_verify()
    assert ctx.check_hostname and ctx.verify_mode.name == "CERT_REQUIRED"
    assert [c["subject"] for c in ctx.get_ca_certs()] == [((("commonName", "test-ca"),),)]


def test_env_is_read_lazily():
    """Importing every module needs no pod environment; using one names the missing variable."""
    import subprocess
    import sys

    code = (
        "import agent_runtime.step, agent_runtime.mcp, agent_runtime.models, agent_runtime.wait\n"
        "from agent_runtime import identity\n"
        "try:\n    identity.gateway_url()\nexcept RuntimeError as e:\n    print(e)\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "AGENT_GATEWAY_URL is not set"


class Gateway:
    """MockTransport handler for the token route plus a scripted inference route."""

    def __init__(self, statuses: list[int]) -> None:
        self.tokens = TokenRoute()
        self.statuses = statuses
        self.calls: list[httpx.Request] = []
        self.auths: list[str] = []  # snapshots: a retried request is the same object

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            return self.tokens(request)
        self.calls.append(request)
        self.auths.append(request.headers["authorization"])
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 302:
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        return httpx.Response(status, json={"status": status})


def gateway_client(handler) -> httpx.Client:
    identity._IDENTITY = AgentIdentity(transport=httpx.MockTransport(handler))
    return gateway.client_for("AGENT_INFERENCE_HOST", 5.0, transport=httpx.MockTransport(handler))


def test_401_refreshes_once_and_retries_once(agent_env):
    gw = Gateway([401, 200])
    r = gateway_client(gw).post("/models/llm-main/v1/chat/completions", json={})
    assert r.status_code == 200
    assert len(gw.calls) == 2 and len(gw.tokens.requests) == 2
    first, second = gw.auths
    assert first != second and second == "Bearer " + agent_token(2)
    assert all(c.headers["host"] == "inference.agents.internal" for c in gw.calls)
    assert all(c.content == b"{}" for c in gw.calls)  # the body is replayed on retry


def test_second_401_is_not_retried_again(agent_env):
    gw = Gateway([401, 401, 401])
    r = gateway_client(gw).post("/x", json={})
    assert r.status_code == 401 and len(gw.calls) == 2


def test_403_is_terminal_and_never_retried(agent_env):
    gw = Gateway([403, 200])
    client = gateway_client(gw)
    r = client.post("/x", json={})
    assert r.status_code == 403 and len(gw.calls) == 1 and len(gw.tokens.requests) == 1
    assert client.auth.status == 403


def test_429_is_reported_not_retried(agent_env):
    gw = Gateway([429, 200])
    client = gateway_client(gw)
    assert client.post("/x").status_code == 429 and len(gw.calls) == 1
    assert client.auth.status == 429


def test_redirects_are_never_followed(agent_env):
    gw = Gateway([302])
    r = gateway_client(gw).get("/x")
    assert r.status_code == 302 and len(gw.calls) == 1
    assert all(c.url.host == "10.43.250.10" for c in gw.calls)


def test_bearer_auth_never_logs_the_token(agent_env, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    gw = Gateway([401, 200])
    gateway_client(gw).post("/x", json={})
    assert_redacted(caplog.text)


def test_bearer_auth_is_an_httpx_auth():
    assert issubclass(BearerAuth, httpx.Auth)
