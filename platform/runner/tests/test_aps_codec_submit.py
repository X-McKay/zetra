from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.exceptions import ApplicationError

from runner.aps import APS
from runner.codec import AesGcmCodec
from runner.submit import main as submit_main
from runner.types import BoundaryOut, InvalidInput, SessionInput

FAKE_JWT = "eyJ" + "h" * 24 + "." + "p" * 24 + "." + "s" * 24  # built at runtime, not a secret
SESSION = "6f1d2c3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"


@pytest.fixture
def aps_server(tmp_path):
    routes: dict[tuple[str, str], tuple[int, dict | None]] = {}
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            seen.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            status, doc = routes.get((self.command, self.path), (404, None))
            data = json.dumps(doc).encode() if doc is not None else b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_PUT = do_POST = do_DELETE = _serve

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = tmp_path / "token"
    token.write_text(FAKE_JWT + "\n")
    client = APS(f"http://127.0.0.1:{server.server_port}", str(token), timeout=5)
    yield client, routes, seen
    server.shutdown()


def test_aps_sends_projected_token_and_parses_json(aps_server, caplog):
    client, routes, seen = aps_server
    routes[("GET", "/v1/grants/gh_1")] = (200, {"session_id": SESSION})
    assert client.get("/v1/grants/gh_1") == {"session_id": SESSION}
    routes[("PUT", f"/v1/sessions/{SESSION}/generation")] = (200, None)
    client.put(f"/v1/sessions/{SESSION}/generation", {"generation": 2})
    assert seen[0]["auth"] == "Bearer " + FAKE_JWT
    assert json.loads(seen[1]["body"]) == {"generation": 2}


def test_aps_rereads_rotated_token(aps_server, tmp_path):
    client, routes, seen = aps_server
    routes[("POST", f"/v1/sessions/{SESSION}/stop")] = (200, {})
    client.post(f"/v1/sessions/{SESSION}/stop", {})
    client.token_path.write_text("rotated")
    client.post(f"/v1/sessions/{SESSION}/stop", {})
    assert [s["auth"] for s in seen] == ["Bearer " + FAKE_JWT, "Bearer rotated"]


def test_aps_4xx_is_forbidden_and_final(aps_server):
    client, routes, _ = aps_server
    routes[("PUT", "/v1/sessions/x/generation")] = (409, {"error": "stale"})
    with pytest.raises(ApplicationError) as e:
        client.put("/v1/sessions/x/generation", {"generation": 1})
    assert e.value.type == "Forbidden" and e.value.non_retryable
    with pytest.raises(ApplicationError) as e:
        client.get("/v1/grants/missing")
    assert e.value.type == "Forbidden"


def test_aps_5xx_is_retryable(aps_server):
    client, routes, _ = aps_server
    routes[("POST", "/v1/sessions/x/stop")] = (503, None)
    with pytest.raises(ApplicationError) as e:
        client.post("/v1/sessions/x/stop", {})
    assert e.value.type == "APSUnavailable" and not e.value.non_retryable


def test_aps_delete_404_is_success(aps_server):
    client, _, seen = aps_server
    assert client.delete("/v1/grants/gone") is None
    assert seen[0]["method"] == "DELETE"


def test_aps_unreachable_is_retryable_and_never_logs_the_token(tmp_path, caplog):
    token = tmp_path / "token"
    token.write_text(FAKE_JWT)
    client = APS("http://127.0.0.1:9", str(token), timeout=1)
    with caplog.at_level(logging.DEBUG), pytest.raises(ApplicationError) as e:
        client.get("/v1/grants/gh")
    assert e.value.type == "APSUnavailable" and FAKE_JWT not in str(e.value)


# --- codec ---------------------------------------------------------------------------------------


async def test_codec_round_trip_and_tenant_binding():
    key = bytes(range(32))
    a, b = AesGcmCodec(key, "tenant-a"), AesGcmCodec(key, "tenant-b")
    plain = Payload(metadata={"encoding": b"json/plain"}, data=b'{"task":"list pods"}')
    [sealed] = await a.encode([plain])
    assert b"list pods" not in sealed.data and sealed.metadata["encoding"] == b"binary/encrypted"
    assert (await a.decode([sealed]))[0] == plain
    with pytest.raises(Exception):
        await b.decode([sealed])  # another tenant's AAD does not open it
    tampered = Payload(metadata=sealed.metadata, data=sealed.data[:-1] + b"\0")
    with pytest.raises(Exception):
        await a.decode([tampered])


def test_codec_key_file_accepts_raw_or_base64(tmp_path):
    import base64

    (tmp_path / "raw").write_bytes(bytes(32))
    (tmp_path / "b64").write_text(base64.b64encode(bytes(32)).decode())
    assert (
        AesGcmCodec.from_file(tmp_path / "raw", "tenant-a").key_id
        == AesGcmCodec.from_file(tmp_path / "b64", "tenant-a").key_id
    )
    (tmp_path / "short").write_bytes(b"short")
    with pytest.raises(Exception):
        AesGcmCodec.from_file(tmp_path / "short", "tenant-a")


# --- input validation and submit -------------------------------------------------------------------


def valid(**over) -> SessionInput:
    fields = dict(
        tenant="tenant-a",
        agent="investigator",
        release="investigator-r1",
        session_id=SESSION,
        grant_handle="gh_7c1f",
        task={"goal": "list pods"},
    )
    fields.update(over)
    return SessionInput(**fields)


def test_session_input_valid():
    valid().validate()
    assert valid().workflow_id == f"session:tenant-a:investigator:{SESSION}"
    assert valid().task_queue == "sessions-tenant-a"


@pytest.mark.parametrize(
    "over",
    [
        {"tenant": "a"},
        {"tenant": "tenant-A"},
        {"agent": "Bad_Name"},
        {"session_id": SESSION.upper()},
        {"session_id": "6f1d2c3e-4b5a-1c6d-8e7f-9a0b1c2d3e4f"},
        {"task": {"note": FAKE_JWT}},
        {"task": {"h": "Bearer " + "a" * 30}},
        {"task": {"access_token": "x"}},
        {"task": [{"nested": {"Password": "x"}}]},
        {"task": "x" * 65537},
        {"max_boundaries": 65},
        {"max_boundaries": 0},
        {"max_boundaries": True},
        {"grant_handle": FAKE_JWT},
        {"task": float("nan")},
    ],
)
def test_session_input_rejections(over):
    with pytest.raises(InvalidInput):
        valid(**over).validate()


def test_submit_cli_refuses_token_like_input_before_connecting(capsys):
    rc = submit_main(
        [
            "--tenant",
            "tenant-a",
            "--agent",
            "investigator",
            "--release",
            "r1",
            "--session-id",
            SESSION,
            "--task",
            json.dumps({"t": FAKE_JWT}),
        ]
    )
    assert rc == 2 and FAKE_JWT not in capsys.readouterr().err


def test_submit_cli_refuses_oversized_task(capsys):
    rc = submit_main(
        [
            "--tenant",
            "tenant-a",
            "--agent",
            "investigator",
            "--release",
            "r1",
            "--task",
            json.dumps({"t": "x" * 70000}),
        ]
    )
    assert rc == 2


async def test_submit_uses_workflow_id_and_reject_duplicate():
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from runner.submit import submit

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def start_workflow(self, fn, arg, **kw):
            self.calls.append(kw)
            if len(self.calls) > 1:
                raise WorkflowAlreadyStartedError(kw["id"], "SessionWorkflow")
            return "handle"

        def get_workflow_handle_for(self, fn, wid):
            return ("existing", wid)

    c = FakeClient()
    assert await submit(c, valid()) == "handle"
    assert await submit(c, valid()) == ("existing", valid().workflow_id)
    kw = c.calls[0]
    assert kw["id"] == valid().workflow_id and kw["task_queue"] == "sessions-tenant-a"
    assert kw["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert kw["execution_timeout"].total_seconds() == 8.25 * 3600
    with pytest.raises(InvalidInput):
        await submit(c, valid(task={"t": FAKE_JWT}))


def test_boundary_out_parse_strict_happy():
    doc = {
        "boundary": 3,
        "kind": "tool",
        "done": False,
        "tool": "pods_get",
        "usage": None,
        "summary": "ok",
    }
    assert BoundaryOut.parse_strict(doc).tool == "pods_get"
