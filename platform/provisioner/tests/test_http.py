import json
import logging
import urllib.parse

import httpx
import pytest

from registry_provisioner.registry import ASSERTION_TYPE, HttpRegistry, RegistryError, TokenSource
from registry_provisioner.settings import Settings, SettingsError
from tests.fakes import example

TOKEN_URL = "https://keycloak.test/realms/r/protocol/openid-connect/token"
SA_TOKEN = "sa-token-AAAA"
ACCESS = "access-token-BBBB"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text(SA_TOKEN + "\n")
    return path


@pytest.fixture
def clock():
    return Clock()


class Server:
    """Plays the realm and the registry."""

    def __init__(self):
        self.token_requests: list[dict] = []
        self.api_requests: list[httpx.Request] = []
        self.issued = 0
        self.api_answers: list = []  # httpx.Response or Exception, consumed in order
        self.expires_in = 300
        self.token_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            self.token_requests.append(dict(urllib.parse.parse_qsl(request.content.decode())))
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            self.issued += 1
            return httpx.Response(
                200, json={"access_token": f"{ACCESS}-{self.issued}", "expires_in": self.expires_in}
            )
        self.api_requests.append(request)
        answer = self.api_answers.pop(0) if self.api_answers else httpx.Response(200, json={})
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def make(server, token_file, clock):
    def build(**overrides):
        http = httpx.Client(transport=httpx.MockTransport(server))
        tokens = TokenSource(
            http, TOKEN_URL, "registry-provisioner-kubani", str(token_file), clock=clock
        )
        return HttpRegistry("https://registry.test/", "kubani", tokens, http)

    return build


def desired_response():
    return httpx.Response(200, json=example("desired.json"))


def test_token_request_form_and_bearer(make, server):
    server.api_answers = [desired_response()]
    answer = make().desired(7)
    assert answer["generation"] == 3
    assert server.token_requests == [
        {
            "grant_type": "client_credentials",
            "client_id": "registry-provisioner-kubani",
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": SA_TOKEN,
        }
    ]
    (req,) = server.api_requests
    assert req.headers["Authorization"] == f"Bearer {ACCESS}-1"
    assert str(req.url) == "https://registry.test/v1/clusters/kubani/desired?lastGeneration=7"


def test_report_posts_json(make, server):
    server.api_answers = [
        httpx.Response(200, json={"mode": "normal", "generation": 3, "stored": 4})
    ]
    assert make().report({"observedAt": "x", "lastGeneration": 3})["stored"] == 4
    (req,) = server.api_requests
    assert req.method == "POST" and req.url.path == "/v1/clusters/kubani/observations"
    assert json.loads(req.content) == {"observedAt": "x", "lastGeneration": 3}


def test_token_is_reused_then_refreshed_before_expiry(make, server, clock, token_file):
    server.api_answers = [desired_response()] * 3
    registry = make()
    registry.desired(0)
    registry.desired(0)
    assert server.issued == 1
    token_file.write_text("rotated\n")
    clock.now += 300 - 30  # inside the margin
    registry.desired(0)
    assert server.issued == 2
    assert server.token_requests[1]["client_assertion"] == "rotated"
    assert server.api_requests[2].headers["Authorization"] == f"Bearer {ACCESS}-2"


def test_401_refreshes_once_and_retries(make, server):
    err = {"error": {"code": "unauthorized", "message": "expired", "retryable": False}}
    server.api_answers = [httpx.Response(401, json=err), desired_response()]
    assert make().desired(0)["mode"] == "normal"
    assert server.issued == 2 and len(server.api_requests) == 2
    assert server.api_requests[1].headers["Authorization"] == f"Bearer {ACCESS}-2"


def test_second_401_is_an_error(make, server):
    err = {"error": {"code": "unauthorized", "message": "no", "retryable": False}}
    server.api_answers = [httpx.Response(401, json=err)] * 3
    with pytest.raises(RegistryError) as info:
        make().desired(0)
    assert info.value.status == 401 and info.value.code == "unauthorized"
    assert len(server.api_requests) == 2


def test_error_body_becomes_the_one_error(make, server):
    err = {"error": {"code": "unavailable", "message": "try later", "retryable": True}}
    server.api_answers = [httpx.Response(500, json=err)]
    with pytest.raises(RegistryError) as info:
        make().desired(0)
    assert (info.value.status, info.value.code, info.value.retryable) == (500, "unavailable", True)
    assert "try later" in str(info.value)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
        httpx.Response(502, text="<html>bad gateway</html>"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[1]),
        httpx.Response(200, json={"mode": "normal"}),
    ],
)
def test_everything_else_is_the_one_error(make, server, answer):
    server.api_answers = [answer]
    with pytest.raises(RegistryError):
        make().desired(0)


def test_token_endpoint_failure_is_the_one_error(make, server):
    server.token_status = 401
    with pytest.raises(RegistryError, match="401: invalid_client"):
        make().desired(0)
    assert server.api_requests == []


def test_unreadable_token_file_is_the_one_error(server, clock, tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(server))
    tokens = TokenSource(http, TOKEN_URL, "c", str(tmp_path / "missing"), clock=clock)
    with pytest.raises(RegistryError):
        HttpRegistry("https://registry.test", "kubani", tokens, http).desired(0)


def test_no_token_in_errors_or_logs(make, server, caplog):
    caplog.set_level(logging.DEBUG)
    texts = []
    scenarios = [
        [httpx.Response(500, json={"error": {"code": "x", "message": "m", "retryable": True}})],
        [httpx.ReadTimeout("slow")],
        [httpx.Response(401, json={})] * 2,
    ]
    for answers in scenarios:
        server.api_answers = list(answers)
        with pytest.raises(RegistryError) as info:
            make().desired(0)
        texts.append(str(info.value) + repr(info.value))
    server.token_status = 403
    with pytest.raises(RegistryError) as info:
        make().desired(0)
    texts.append(str(info.value) + repr(info.value))
    everything = " ".join(texts) + " ".join(r.getMessage() for r in caplog.records)
    assert ACCESS not in everything and SA_TOKEN not in everything


# settings


def test_settings_defaults_and_client_id():
    s = Settings.from_env(
        {
            "PROVISIONER_CLUSTER": "kubani",
            "PROVISIONER_REGISTRY_URL": "https://r.test/",
            "PROVISIONER_NAMESPACE": "ns",
        }
    )
    assert (s.interval_seconds, s.client_id, s.registry_url) == (
        30.0,
        "registry-provisioner-kubani",
        "https://r.test",
    )


@pytest.mark.parametrize(
    "extra", [{"PROVISIONER_REGISTRY_URL": "http://r.test"}, {"PROVISIONER_INTERVAL_SECONDS": "0"}]
)
def test_settings_refuse(extra):
    base = {
        "PROVISIONER_CLUSTER": "kubani",
        "PROVISIONER_REGISTRY_URL": "https://r.test",
        "PROVISIONER_NAMESPACE": "ns",
    }
    with pytest.raises(SettingsError):
        Settings.from_env({**base, **extra})


def test_real_client_needs_token_settings():
    s = Settings.from_env(
        {
            "PROVISIONER_CLUSTER": "k",
            "PROVISIONER_REGISTRY_URL": "https://r.test",
            "PROVISIONER_NAMESPACE": "ns",
        }
    )
    with pytest.raises(SettingsError):
        HttpRegistry.from_settings(s)
