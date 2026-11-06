"""Exact T4 claims and genuine negative credential fixtures for T5."""

import json
import base64
import time
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from runner import conformance as c
from tests.test_conformance import issued, run, TENANT
from tests.test_t19 import snapshot

ISSUER = "https://idp.example.test/realms/agents"


def agent_claims():
    return {
        "iss": ISSUER,
        "aud": ["agent-gateway", "agent-policy-service"],
        "client_type": "agent",
        "agent_id": "tenant-test/conformance",
        "iat": 1000,
        "exp": 1300,
        "jti": "j1",
        "sub": "service-account-user-id",
    }


def file_probe(claims=None):
    return {"files": [{"path": "/sandbox/state/token.json", "claims": [claims or agent_claims()]}]}


def t4_evidence():
    r = run(summary="prose is not evidence")
    line = issued(r.pod)
    line["agent_id"] = "tenant-test/conformance"
    return r, [line]


def test_t4_does_not_pass_on_issuance_and_model_summary_without_snapshot_claims():
    r = run(summary='{"aud":["agent-gateway","agent-policy-service"]}')
    assert c.evaluate_t4(r, [issued(r.pod)], TENANT)["result"] == "fail"


def test_api_audience_refusal_requires_invalid_client_not_just_http_401():
    case = c.DirectCase("api_audience", "/unused", 401)
    out = c.evaluate_t5_direct(case, 401, json.dumps({"error": "server_error"}), None)
    assert out["result"] == "fail"


def test_t4_reads_only_allowlisted_runtime_token_claims_from_verified_snapshot(tmp_path):
    claims = agent_claims() | {"secret_extension": "must-never-be-output"}
    probe = file_probe(claims)
    probe["files"].insert(0, {"path": "/var/run/secrets/agent-identity/token", "claims": [claims]})
    digest, path = snapshot(tmp_path, probe, wrong_tool=True)
    assert c.read_t4_claims(str(tmp_path), digest) == agent_claims()
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError):
        c.read_t4_claims(str(tmp_path), digest)


@pytest.mark.parametrize("problem", ["wrong_path", "duplicate_file", "duplicate_token", "missing"])
def test_t4_rejects_missing_or_ambiguous_runtime_token_claims(tmp_path, problem):
    probe = file_probe()
    if problem == "wrong_path":
        probe["files"][0]["path"] = "/other/token.json"
    elif problem == "duplicate_file":
        probe["files"].append(probe["files"][0])
    elif problem == "duplicate_token":
        probe["files"][0]["claims"].append(agent_claims())
    else:
        probe["files"] = []
    digest, _ = snapshot(tmp_path, probe, wrong_tool=True)
    with pytest.raises(ValueError):
        c.read_t4_claims(str(tmp_path), digest)


@pytest.mark.parametrize(
    "change",
    [
        {"aud": ["agent-gateway"]},
        {"aud": ["agent-policy-service"]},
        {"aud": "agent-gateway agent-policy-service"},
        {"client_type": "human"},
        {"agent_id": "tenant-test/other"},
        {"iss": "https://wrong.example.test"},
        {"exp": 1301},
        {"iat": "1000"},
        {"iat": True, "exp": 301},
        {"jti": "not-issued"},
        {"jti": None},
    ],
)
def test_t4_requires_every_exact_claim(change):
    r, lines = t4_evidence()
    out = c.evaluate_t4(r, lines, TENANT, agent_claims() | change, issuer=ISSUER)
    assert out["result"] == "fail"


def test_t4_passes_exact_claims_and_keeps_only_safe_evidence():
    r, lines = t4_evidence()
    out = c.evaluate_t4(
        r, lines, TENANT, agent_claims() | {"secret": "never-output"}, issuer=ISSUER
    )
    assert out["result"] == "pass" and all(out["checks"].values())
    assert out["evidence"]["claims"] == agent_claims()
    assert "never-output" not in json.dumps(out)
    assert out["evidence"]["token_issued"][0]["jti"] == "j1"


@pytest.mark.parametrize(
    "problem", ["no_issuance", "other_pod", "other_tenant", "failed", "wrong_probe", "unreadable"]
)
def test_t4_still_requires_session_and_complete_issuance_evidence(problem):
    r, lines = t4_evidence()
    if problem == "no_issuance":
        lines = []
    elif problem == "other_pod":
        lines[0]["pod"] = "s-other-g1"
    elif problem == "other_tenant":
        lines[0]["namespace"] = "tenant-other"
    elif problem == "failed":
        r.outcome = "FAILED"
    elif problem == "wrong_probe":
        r.boundaries[1]["tool"] = "probe_env"
    else:
        lines.append({"msg": c.LOG_UNAVAILABLE, "pod": "aps-unreadable"})
    assert c.evaluate_t4(r, lines, TENANT, agent_claims(), issuer=ISSUER)["result"] == "fail"


def test_t4_accepts_refreshed_issuance_and_replaced_sandbox_when_claims_match():
    from runner.activities import pod_name

    r, lines = t4_evidence()
    r.generation, r.pod = 2, pod_name(r.session_id, 2)
    lines.append({**lines[0], "pod": r.pod, "jti": "generation-2-token"})
    out = c.evaluate_t4(
        r, lines, TENANT, agent_claims() | {"jti": "generation-2-token"}, issuer=ISSUER
    )
    assert out["result"] == "pass" and out["checks"]["one_token_per_sandbox"] is False


def test_t4_current_snapshot_cannot_use_another_generations_token_issuance():
    from runner.activities import pod_name

    r, lines = t4_evidence()
    r.generation, r.pod = 2, pod_name(r.session_id, 2)
    assert c.evaluate_t4(r, lines, TENANT, agent_claims(), issuer=ISSUER)["result"] == "fail"


async def test_t4_harness_reads_committed_snapshot_instead_of_summary(monkeypatch, tmp_path):
    r, lines = t4_evidence()
    r.snapshot, _ = snapshot(tmp_path, file_probe(), wrong_tool=True)

    async def logs(since):
        return "\n".join(json.dumps(line) for line in lines)

    async def session(case):
        return r

    h = c.Harness(
        None, TENANT, "conformance", "r1", logs, artifact_dir=str(tmp_path), issuer=ISSUER
    )
    monkeypatch.setattr(h, "run_session", session)
    assert (await h.run(["T4"]))["result"] == "pass"


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unsigned_token(claims):
    header = b64(json.dumps({"alg": "none"}).encode())
    return f"{header}.{b64(json.dumps(claims).encode())}.unsigned"


def api_claims():
    return {
        "iss": "https://kubernetes.default.svc.cluster.local",
        "sub": "system:serviceaccount:agent-platform:conformance-harness",
        "aud": ["https://kubernetes.default.svc.cluster.local"],
        "iat": int(time.time()) - 10,
        "exp": int(time.time()) + 300,
        "kubernetes.io": {
            "namespace": "agent-platform",
            "serviceaccount": {"name": "conformance-harness", "uid": "sa-uid"},
            "pod": {"name": "harness-pod", "uid": "pod-uid"},
        },
    }


def test_api_fixture_requires_live_api_authentication_with_the_same_identity():
    from runner.token_conformance import verify_api_fixture

    claims = api_claims()
    proof = verify_api_fixture(unsigned_token(claims), claims["sub"], claims["sub"], ISSUER)
    assert proof["sub"] == claims["sub"] and "kubernetes.io" not in proof
    with pytest.raises(ValueError):
        verify_api_fixture(unsigned_token(claims), "system:anonymous", claims["sub"], ISSUER)


@pytest.mark.parametrize(
    "problem", ["expired", "aps_audience", "issuer_audience", "unbound", "wrong_sa"]
)
def test_api_fixture_rejects_substitutes(problem):
    from runner.token_conformance import verify_api_fixture

    claims = api_claims()
    expected = claims["sub"]
    if problem == "expired":
        claims["exp"] = int(time.time()) - 1
    elif problem.endswith("audience"):
        claims["aud"] = ["agent-policy-service" if problem == "aps_audience" else ISSUER]
    elif problem == "unbound":
        claims["kubernetes.io"].pop("pod")
    else:
        claims["kubernetes.io"]["serviceaccount"]["name"] = "other"
    with pytest.raises(ValueError):
        verify_api_fixture(unsigned_token(claims), expected, expected, ISSUER)


@pytest.fixture
def human_signer():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "kid": "test-key",
                "n": b64(numbers.n.to_bytes(256, "big")),
                "e": b64(b"\x01\x00\x01"),
            }
        ]
    }

    def sign(claims, **header):
        header = {"alg": "RS256", "kid": "test-key", "typ": "JWT", **header}
        message = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
        signature = key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{message}.{b64(signature)}"

    return sign, jwks


def human_claims():
    return {
        "iss": ISSUER,
        "sub": "human-user-uuid",
        "preferred_username": "alice",
        "azp": "agent-cli",
        "typ": "Bearer",
        "aud": ["agent-policy-service"],
        "iat": int(time.time()) - 10,
        "exp": int(time.time()) + 300,
        "jti": "human-jti",
    }


def test_human_fixture_verifies_signature_and_only_returns_allowlisted_claims(human_signer):
    from runner.token_conformance import verify_human_fixture

    sign, jwks = human_signer
    token = sign(human_claims() | {"private_extension": "not-output"})
    proof = verify_human_fixture(token, jwks, ISSUER, "human-user-uuid", "alice", "agent-cli")
    assert proof["jti"] == "human-jti" and proof["sub"] == "human-user-uuid"
    assert token not in json.dumps(proof) and "not-output" not in json.dumps(proof)


@pytest.mark.parametrize(
    "problem",
    [
        "unsigned",
        "bad_signature",
        "wrong_kid",
        "wrong_algorithm",
        "expired",
        "future",
        "wrong_issuer",
        "wrong_subject",
        "wrong_username",
        "agent",
        "service_account",
        "wrong_client",
        "id_token",
        "delegated_token",
    ],
)
def test_human_fixture_never_counts_fake_expired_or_nonhuman_tokens(human_signer, problem):
    from runner.token_conformance import verify_human_fixture

    sign, jwks = human_signer
    claims = human_claims()
    updates = {
        "expired": {"exp": int(time.time()) - 1},
        "future": {"nbf": int(time.time()) + 60},
        "wrong_issuer": {"iss": "https://other.example.test"},
        "wrong_subject": {"sub": "other"},
        "wrong_username": {"preferred_username": "bob"},
        "agent": {"client_type": "agent"},
        "service_account": {"preferred_username": "service-account-agent-cli"},
        "wrong_client": {"azp": "other-client"},
        "id_token": {"typ": "ID"},
        "delegated_token": {"act": {"sub": "delegating-client"}},
    }
    claims.update(updates.get(problem, {}))
    token = sign(claims, **({"kid": "unknown"} if problem == "wrong_kid" else {}))
    if problem == "unsigned":
        token = unsigned_token(claims)
    elif problem == "bad_signature":
        token = token.rsplit(".", 1)[0] + "." + b64(b"bad-signature")
    elif problem == "wrong_algorithm":
        token = sign(claims, alg="HS256")
    with pytest.raises(ValueError):
        verify_human_fixture(token, jwks, ISSUER, "human-user-uuid", "alice", "agent-cli")


@pytest.fixture
def credential_server(monkeypatch, human_signer):
    from kubernetes import client

    sign, jwks = human_signer
    api_token = unsigned_token(api_claims())
    seen = []
    state = {"api_user": api_claims()["sub"], "jwks_status": 200, "token_status": 401}

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            assert self.path == "/certs" and "Authorization" not in self.headers
            self.reply(state["jwks_status"], jwks)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/apis/authentication.k8s.io/v1/selfsubjectreviews":
                if self.headers.get("Authorization") != f"Bearer {api_token}":
                    self.reply(401, {"message": "wrong credential"})
                else:
                    self.reply(
                        201,
                        {
                            "apiVersion": "authentication.k8s.io/v1",
                            "kind": "SelfSubjectReview",
                            "status": {"userInfo": {"username": state["api_user"]}},
                        },
                    )
                return
            assert self.path == "/v1/token"
            form = urllib.parse.parse_qs(body.decode())
            assertion = form["client_assertion"][0]
            seen.append(assertion)
            if assertion == "issuer-token":
                self.reply(403, {"error": "forbidden"})
            else:
                self.reply(state["token_status"], {"error": "invalid_client", "echo": assertion})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    config = client.Configuration(host=url)
    config.api_key = {"authorization": "must-not-use-default-token"}
    monkeypatch.setattr(client.Configuration, "get_default_copy", lambda: config)
    yield url, seen, state, api_token, sign
    server.shutdown()


async def direct_harness(monkeypatch, tmp_path, credential_server, *, human=True, change=None):
    url, _, _, api_token, sign = credential_server
    (tmp_path / "issuer").write_text("issuer-token")
    (tmp_path / "api").write_text(api_token)
    if human:
        (tmp_path / "human").write_text(sign(human_claims() | (change or {})))

    async def logs(since):
        return ""

    monkeypatch.setattr(c, "t5_sandbox_cases", lambda tenant, agent: [])
    harness = c.Harness(
        None,
        TENANT,
        "conformance",
        "r1",
        logs,
        aps_url=url,
        issuer=ISSUER,
        issuer_token_path=str(tmp_path / "issuer"),
        api_token_path=str(tmp_path / "api"),
        human_token_path=str(tmp_path / "human") if human else "",
        human_subject="human-user-uuid",
        human_username="alice",
        human_jwks_url=url + "/certs",
    )
    return await harness.t5()


async def test_t5_requires_genuine_human_fixture_before_full_pass(
    monkeypatch, tmp_path, credential_server
):
    out = await direct_harness(monkeypatch, tmp_path, credential_server, human=False)
    assert [case["case"] for case in out["cases"]] == [
        "non_sandbox_pod",
        "api_audience",
        "human_token",
    ]
    assert [case["result"] for case in out["cases"]] == ["pass", "pass", "error"]
    assert out["result"] == "error"


async def test_t5_posts_verified_api_and_human_assertions_and_never_outputs_them(
    monkeypatch, tmp_path, credential_server, caplog
):
    out = await direct_harness(monkeypatch, tmp_path, credential_server)
    _, seen, _, api_token, _ = credential_server
    assert out["result"] == "pass" and len(seen) == 3
    assert seen[1] == api_token and seen[2] == (tmp_path / "human").read_text()
    assert all(case["checks"]["fixture_verified"] for case in out["cases"][1:])
    for token in seen[1:]:
        assert token not in json.dumps(out) and token not in caplog.text


async def test_t5_uses_short_lived_direct_fixtures_before_starting_sandbox_sessions(
    monkeypatch, tmp_path, credential_server
):
    url, seen, _, api_token, sign = credential_server
    (tmp_path / "issuer").write_text("issuer-token")
    (tmp_path / "api").write_text(api_token)
    (tmp_path / "human").write_text(sign(human_claims()))

    async def logs(since):
        return json.dumps({"msg": "token route refused", "reason": "unsupported_grant_type"})

    async def session(case):
        assert len(seen) == 3, "sandbox work must not consume the human fixture's short lifetime"
        return run(tool="probe_http", summary='{"status":400}')

    case = c.t5_sandbox_cases(TENANT, "conformance")[0]
    monkeypatch.setattr(c, "t5_sandbox_cases", lambda tenant, agent: [case])
    harness = c.Harness(
        None,
        TENANT,
        "conformance",
        "r1",
        logs,
        aps_url=url,
        issuer=ISSUER,
        issuer_token_path=str(tmp_path / "issuer"),
        api_token_path=str(tmp_path / "api"),
        human_token_path=str(tmp_path / "human"),
        human_subject="human-user-uuid",
        human_username="alice",
        human_jwks_url=url + "/certs",
    )
    monkeypatch.setattr(harness, "run_session", session)
    assert (await harness.t5())["result"] == "pass"


@pytest.mark.parametrize(
    "problem", ["expired_human", "api_not_authenticated", "jwks_unavailable", "wrong_status"]
)
async def test_t5_unusable_fixtures_or_wrong_refusal_cannot_pass(
    monkeypatch, tmp_path, credential_server, problem
):
    _, seen, state, _, _ = credential_server
    change = None
    if problem == "expired_human":
        change = {"exp": int(time.time()) - 1}
    elif problem == "api_not_authenticated":
        state["api_user"] = "system:anonymous"
    elif problem == "jwks_unavailable":
        state["jwks_status"] = 503
    else:
        state["token_status"] = 403
    out = await direct_harness(monkeypatch, tmp_path, credential_server, change=change)
    assert out["result"] != "pass"
    assert len(seen) == (3 if problem == "wrong_status" else 2)
