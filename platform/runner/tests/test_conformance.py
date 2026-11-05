"""The conformance harness: pass criteria, evidence decoding and token hygiene, without a
cluster. The T4/T5 runs themselves are the cluster tests (docs/platform.md)."""

from __future__ import annotations

import asyncio
import json
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.converter import DataConverter

from runner import conformance as c
from runner.activities import pod_name
from runner.types import BoundaryOut, SessionInput, Usage

FAKE_JWT = "eyJ" + "h" * 24 + "." + "p" * 24 + "." + "s" * 24  # built at runtime, not a secret
TENANT, AGENT = "tenant-test", "conformance"
SESSION = "6f1d2c3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"


def run(outcome="COMPLETED", tool="probe_files", summary="", session=SESSION) -> c.SessionRun:
    r = c.SessionRun(session, pod_name(session, 1), outcome)
    r.boundaries = [
        {
            "boundary": 0,
            "kind": "model",
            "done": False,
            "tool": None,
            "usage": None,
            "summary": "t",
        },
        {"boundary": 1, "kind": "tool", "done": False, "tool": tool, "usage": None, "summary": "x"},
    ]
    r.final_summary = summary
    return r


def issued(pod: str, namespace: str = TENANT) -> dict:
    return {"msg": "token issued", "jti": "j1", "agent_id": "a", "pod": pod, "namespace": namespace}


# --- definitions ----------------------------------------------------------------------------------


def decision(pod: str, tool: str, reason: str, did: str, **extra) -> dict:
    return {
        "ts": "t",
        "decision_id": did,
        "reason": reason,
        "route": "mcp",
        "backend": "kubernetes",
        "method": "tools/call",
        "tool": tool,
        "pod": pod,
        "agent_id": f"{TENANT}/{AGENT}",
        "session_id": SESSION,
        "principal": None,
        **extra,
    }


def alternation(pod: str, rounds: int) -> list[dict]:
    out = []
    for i in range(rounds):
        out.append(decision(pod, c.MCP_ALLOWED_TOOL, "allow", f"a{i}"))
        out.append(decision(pod, c.MCP_FORBIDDEN_TOOL, c.DENY_ROUTE, f"d{i}"))
    return out


def test_every_case_is_a_valid_session_input():
    for case in [
        c.t4_case(),
        *c.t5_sandbox_cases(TENANT, AGENT),
        c.t8_case(TENANT),
        c.t19_case(TENANT),
    ]:
        SessionInput(TENANT, AGENT, "conformance-r1", SESSION, None, case.task()).validate()


def test_t5_cases_cover_the_refusal_matrix():
    cases = {k.name: k for k in c.t5_sandbox_cases(TENANT, AGENT)}
    assert set(cases) == {
        "password_grant",
        "token_exchange_grant",
        "client_secret",
        "non_jwt_assertion",
        "extra_field",
        "duplicate_field",
    }
    for case in cases.values():
        assert case.args["host_header"] == c.TOKEN_HOST and case.args["auth"] == "none"
        assert json.loads(case.args["form"])  # a form the probe can encode
    assert (
        json.loads(cases["duplicate_field"].args["form"])["client_id"]
        == ["agent-tenant-test-conformance"] * 2
    )
    assert [d.expect_status for d in c.t5_direct_cases("/i", "/a")] == [403, 401]


def test_mcp_cases_name_the_bundle_tools_and_the_forged_headers():
    t19 = c.t19_case(TENANT, rounds=3)
    assert t19.tool == "probe_mcp" and t19.args["rounds"] == "3"
    assert (t19.args["tool"], t19.args["alt_tool"]) == (c.MCP_ALLOWED_TOOL, c.MCP_FORBIDDEN_TOOL)
    assert json.loads(t19.args["args"]) == {"namespace": TENANT}
    t8 = c.t8_case(TENANT)
    assert json.loads(t8.args["headers"]) == c.FORGED_HEADERS and "alt_tool" not in t8.args


# --- evidence -------------------------------------------------------------------------------------


async def test_boundaries_are_decoded_from_run_boundary_results_only():
    conv = DataConverter.default
    out = BoundaryOut(1, "tool", False, "probe_files", Usage(1, 2), "probe_files -> ['files']")
    scheduled = HistoryEvent(event_id=5, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    scheduled.activity_task_scheduled_event_attributes.activity_type.name = "run_boundary"
    other = HistoryEvent(event_id=7, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    other.activity_task_scheduled_event_attributes.activity_type.name = "snapshot_workspace"
    done = HistoryEvent(event_id=6, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED)
    done.activity_task_completed_event_attributes.scheduled_event_id = 5
    done.activity_task_completed_event_attributes.result.payloads.extend(
        await conv.encode(
            [
                json.loads(
                    json.dumps(out.__dict__ | {"usage": {"input_tokens": 1, "output_tokens": 2}})
                )
            ]
        )
    )
    snap = HistoryEvent(event_id=8, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED)
    snap.activity_task_completed_event_attributes.scheduled_event_id = 7
    snap.activity_task_completed_event_attributes.result.payloads.extend(await conv.encode(["abc"]))

    assert await c.boundaries_from_events([scheduled, other, done, snap], conv) == [out]


async def test_failures_are_decoded_from_history():
    conv = DataConverter.default
    sched = HistoryEvent(event_id=3, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    sched.activity_task_scheduled_event_attributes.activity_type.name = "preflight"
    plain = HistoryEvent(event_id=4, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED)
    plain.activity_task_failed_event_attributes.scheduled_event_id = 3
    plain.activity_task_failed_event_attributes.failure.message = "release not ready"
    plain.activity_task_failed_event_attributes.failure.application_failure_info.type = "Forbidden"
    plain.activity_task_failed_event_attributes.failure.application_failure_info.non_retryable = (
        True
    )
    sched2 = HistoryEvent(event_id=5, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    sched2.activity_task_scheduled_event_attributes.activity_type.name = "set_generation"
    encoded = HistoryEvent(event_id=6, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED)
    encoded.activity_task_failed_event_attributes.scheduled_event_id = 5
    encoded.activity_task_failed_event_attributes.failure.message = "Encoded failure"
    encoded.activity_task_failed_event_attributes.failure.application_failure_info.type = (
        "APSUnavailable"
    )
    [payload] = await conv.encode([{"message": "APS unreachable: URLError", "stack_trace": "x"}])
    encoded.activity_task_failed_event_attributes.failure.encoded_attributes.CopyFrom(payload)
    timed = HistoryEvent(event_id=7, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT)
    timed.activity_task_timed_out_event_attributes.scheduled_event_id = 5
    wf = HistoryEvent(event_id=8, event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED)
    wf.workflow_execution_failed_event_attributes.failure.message = "deadline"

    out = await c.failures_from_events([sched, plain, sched2, encoded, timed, wf], conv)
    assert out[0] == {
        "activity": "preflight",
        "type": "Forbidden",
        "non_retryable": True,
        "message": "release not ready",
    }
    assert out[1]["activity"] == "set_generation" and out[1]["type"] == "APSUnavailable"
    assert out[1]["message"] == "APS unreachable: URLError"
    assert out[2]["activity"] == "set_generation" and out[2]["type"] == "TimedOut"
    assert out[3] == {
        "activity": "workflow",
        "type": "Failure",
        "non_retryable": True,
        "message": "deadline",
    }


def test_aps_log_keeps_only_token_route_events():
    text = "\n".join(
        [
            "not json",
            json.dumps({"msg": "ext_authz allow", "x": 1}),
            json.dumps({"msg": "token route refused", "reason": "unsupported_grant_type"}),
            json.dumps(issued("s-1-g1")),
            "",
        ]
    )
    assert [line["msg"] for line in c.parse_aps_log(text)] == [
        "token route refused",
        "token issued",
    ]


def test_aps_log_keeps_decisions_too():
    text = (
        json.dumps(decision("s-1-g1", "pods_get", c.DENY_ROUTE, "d1"))
        + "\n"
        + json.dumps({"msg": "ext_authz allow", "x": 1})
    )
    [kept] = c.parse_aps_log(text)
    assert kept["decision_id"] == "d1"


@pytest.mark.parametrize(
    "summary,status",
    [
        ('{"status": 401, "body": "{\\"error\\":\\"invalid_client\\"}"}', 401),
        ("The probe returned status=400 (invalid_request).", 400),
        ("The request was refused with 403 forbidden", 403),
        ("connected", None),
    ],
)
def test_status_is_read_from_the_model_report(summary, status):
    assert c.status_from_summary(summary) == status


# --- pass criteria --------------------------------------------------------------------------------


def test_t4_passes_when_the_aps_issued_a_token_to_that_pod():
    r = run(
        summary='{"files": [{"path": "/sandbox/state/token.json", "claims": [{"aud": "agent-gateway"}]}]}'
    )
    out = c.evaluate_t4(r, [issued(r.pod)], TENANT)
    assert out["result"] == "pass" and all(out["checks"].values())
    assert out["evidence"]["token_issued"][0]["pod"] == r.pod


@pytest.mark.parametrize(
    "r,lines",
    [
        (run(), []),  # no issuance seen
        (run(), [issued("s-other-g1")]),  # another pod's token
        (run(), [issued(pod_name(SESSION, 1), "tenant-other")]),  # another tenant
        (run(outcome="FAILED"), [issued(pod_name(SESSION, 1))]),
        (run(tool="probe_env"), [issued(pod_name(SESSION, 1))]),
    ],
)
def test_t4_fails_without_its_required_evidence(r, lines):
    assert c.evaluate_t4(r, lines, TENANT)["result"] == "fail"


def test_t4_accepts_the_token_of_a_replaced_sandbox():
    r = run()
    r.generation, r.pod = 2, pod_name(SESSION, 2)
    out = c.evaluate_t4(r, [issued(pod_name(SESSION, 2))], TENANT)
    assert out["result"] == "pass" and out["evidence"]["session"]["pod"] == pod_name(SESSION, 2)


def test_failure_type_finds_the_application_error_behind_the_wrapping():
    from temporalio.exceptions import ApplicationError

    inner = ApplicationError("preflight refused", type="Forbidden")
    outer = RuntimeError("activity failed")  # stands in for the SDK's ActivityError wrapper
    outer.__cause__ = inner
    assert c.failure_type(outer) == "Forbidden"
    assert c.failure_type(RuntimeError("x")) == "RuntimeError"


async def test_run_session_cancels_the_workflow_on_harness_timeout(monkeypatch):
    class Handle:
        cancelled = False

        async def result(self):
            await asyncio.sleep(10)

        async def cancel(self):
            self.cancelled = True

        async def fetch_history_events(self):
            return
            yield  # an empty async iterator

    handle = Handle()

    async def fake_submit(client, inp):
        return handle

    monkeypatch.setattr(c, "submit", fake_submit)

    async def no_logs(since: float) -> str:
        return ""

    client = types.SimpleNamespace(data_converter=DataConverter.default)
    h = c.Harness(client, TENANT, AGENT, "r1", no_logs, session_timeout=0.01)
    r = await h.run_session(c.t4_case())
    assert r.outcome == "HARNESS_TIMEOUT" and handle.cancelled and r.boundaries == []


def test_t4_records_a_second_issuance_without_failing():
    r = run()
    out = c.evaluate_t4(r, [issued(r.pod), issued(r.pod)], TENANT)
    assert out["result"] == "pass" and out["checks"]["one_token_per_sandbox"] is False


def test_t5_sandbox_case_passes_on_expected_status_and_logged_refusal():
    case = c.t5_sandbox_cases(TENANT, AGENT)[0]
    r = run(tool="probe_http", summary='{"status": 400, "body": "...", "claims": []}')
    refusal = {"msg": "token route refused", "reason": "unsupported_grant_type"}
    out = c.evaluate_t5_sandbox(case, r, [refusal, issued(r.pod)])
    assert out["result"] == "pass" and out["observed_status"] == 400
    assert out["evidence"]["refusals"] == [{"reason": "unsupported_grant_type"}]
    assert out["evidence"]["issued_during_case"] == 1


@pytest.mark.parametrize(
    "summary,lines",
    [
        ('{"status": 200}', [{"msg": "token route refused"}]),  # wrong status
        ('{"status": 400}', []),  # nothing refused at the APS
        ("done", [{"msg": "token route refused"}]),  # no status reported
    ],
)
def test_t5_sandbox_case_fails_otherwise(summary, lines):
    case = c.t5_sandbox_cases(TENANT, AGENT)[0]
    assert (
        c.evaluate_t5_sandbox(case, run(tool="probe_http", summary=summary), lines)["result"]
        == "fail"
    )


def test_t5_direct_case_never_accepts_a_token_in_the_reply():
    case = c.DirectCase("non_sandbox_pod", "/t", 403)
    assert (
        c.evaluate_t5_direct(case, 403, '{"error":"forbidden","reason":"not_an_agent"}', None)[
            "result"
        ]
        == "pass"
    )
    assert c.evaluate_t5_direct(case, 401, "{}", None)["result"] == "fail"
    assert c.evaluate_t5_direct(case, None, "", "URLError")["result"] == "fail"
    bad = c.evaluate_t5_direct(case, 403, '{"access_token":"' + FAKE_JWT + '"}', None)
    assert bad["result"] == "fail" and FAKE_JWT not in json.dumps(bad)


def test_t19_passes_on_an_exact_alternation_of_fresh_decisions():
    r = run(tool="probe_mcp", summary='{"calls":["ok","deny","ok","deny"]}')
    other = decision("s-other-g1", c.MCP_ALLOWED_TOOL, "allow", "x")  # another session's call
    init = {**decision(r.pod, None, "allow", "i"), "method": "initialize", "tool": None}
    out = c.evaluate_t19(r, [init, *alternation(r.pod, 2), other], rounds=2)
    assert out["result"] == "pass" and all(out["checks"].values()), out["checks"]
    assert [d["decision_id"] for d in out["evidence"]["decisions"]] == ["a0", "d0", "a1", "d1"]


@pytest.mark.parametrize(
    "r,lines,failed_check",
    [
        (run(tool="probe_mcp"), alternation(pod_name(SESSION, 1), 1), "one_decision_per_request"),
        (  # a cached allow served the forbidden tool
            run(tool="probe_mcp"),
            [
                decision(pod_name(SESSION, 1), c.MCP_ALLOWED_TOOL, "allow", "a0"),
                decision(pod_name(SESSION, 1), c.MCP_FORBIDDEN_TOOL, "allow", "d0"),
                *alternation(pod_name(SESSION, 1), 1),
            ],
            "exact_alternation",
        ),
        (  # one decision id reused: the second request was not evaluated
            run(tool="probe_mcp"),
            [
                decision(pod_name(SESSION, 1), c.MCP_ALLOWED_TOOL, "allow", "same"),
                decision(pod_name(SESSION, 1), c.MCP_FORBIDDEN_TOOL, c.DENY_ROUTE, "same"),
                *alternation(pod_name(SESSION, 1), 1),
            ],
            "distinct_decision_ids",
        ),
        (
            run(outcome="FAILED", tool="probe_mcp"),
            alternation(pod_name(SESSION, 1), 2),
            "session_completed",
        ),
        (run(tool="probe_http"), alternation(pod_name(SESSION, 1), 2), "probe_mcp_ran"),
    ],
)
def test_t19_fails_without_its_required_evidence(r, lines, failed_check):
    out = c.evaluate_t19(r, lines, rounds=2)
    assert out["result"] == "fail" and out["checks"][failed_check] is False


def test_t8_passes_when_the_decision_carries_the_sandboxes_identity():
    r = run(tool="probe_mcp", summary='{"calls":["ok"]}')
    out = c.evaluate_t8(r, [decision(r.pod, c.MCP_ALLOWED_TOOL, "allow", "a0")], TENANT, AGENT)
    assert out["result"] == "pass" and all(out["checks"].values()), out["checks"]
    assert "not captured" in out["evidence"]["upstream_capture"]


@pytest.mark.parametrize(
    "forged",
    [
        {"agent_id": "tenant-test/other"},
        {"principal": "mallory"},
        {"session_id": "0000"},
    ],
)
def test_t8_fails_when_a_forged_value_wins(forged):
    line = decision(pod_name(SESSION, 1), c.MCP_ALLOWED_TOOL, "allow", "a0", **forged)
    out = c.evaluate_t8(run(tool="probe_mcp"), [line], TENANT, AGENT)
    assert out["result"] == "fail"
    assert out["checks"]["identity_is_the_sandboxes"] is False
    assert out["checks"]["no_forged_value_in_decisions"] is False


def test_t8_fails_when_the_allowed_call_is_denied():
    line = decision(pod_name(SESSION, 1), c.MCP_ALLOWED_TOOL, c.DENY_ROUTE, "a0")
    out = c.evaluate_t8(run(tool="probe_mcp"), [line], TENANT, AGENT)
    assert out["result"] == "fail" and out["checks"]["call_allowed"] is False


def test_summary_is_the_worst_result():
    assert c.summarize([{"result": "pass"}, {"result": "fail"}]) == "fail"
    assert c.summarize([{"result": "fail"}, {"result": "error"}]) == "error"
    assert c.summarize([{"result": "pass"}]) == "pass"


def test_scrub_removes_every_token_shape():
    doc = {"a": [FAKE_JWT, {"b": "Bearer " + "x" * 30}], FAKE_JWT: "ok"}
    text = json.dumps(c.scrub(doc))
    assert FAKE_JWT not in text and "Bearer " + "x" * 30 not in text
    assert text.count("[redacted]") == 3


# --- the direct cases against a fake APS ----------------------------------------------------------


@pytest.fixture
def fake_aps():
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            form = dict(
                (k, v[0])
                for k, v in __import__("urllib.parse")
                .parse.parse_qs(self.rfile.read(length).decode())
                .items()
            )
            seen.append(form)
            status = 403 if form.get("client_assertion") == "issuer-token" else 401
            body = json.dumps(
                {"error": "forbidden" if status == 403 else "invalid_client"}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()


async def test_t5_direct_cases_post_the_pod_tokens_and_never_log_them(fake_aps, tmp_path, caplog):
    url, seen = fake_aps
    (tmp_path / "issuer").write_text("issuer-token\n")
    (tmp_path / "aps").write_text(FAKE_JWT)

    async def no_logs(since: float) -> str:
        return ""

    h = c.Harness(
        client=None,
        tenant=TENANT,
        agent=AGENT,
        release="conformance-r1",
        aps_logs=no_logs,
        aps_url=url,
        issuer_token_path=str(tmp_path / "issuer"),
        aps_token_path=str(tmp_path / "aps"),
    )
    # Only the direct cases: no Temporal client, so the sandbox cases are skipped by monkeypatch.
    h.run_session = None  # type: ignore[assignment]
    c_cases = c.t5_sandbox_cases  # keep a reference for restoration
    c.t5_sandbox_cases = lambda tenant, agent: []  # type: ignore[assignment]
    try:
        out = await h.t5()
    finally:
        c.t5_sandbox_cases = c_cases  # type: ignore[assignment]
    assert [k["result"] for k in out["cases"]] == ["pass", "pass"] and out["result"] == "pass"
    assert [s["client_assertion"] for s in seen] == ["issuer-token", FAKE_JWT]
    assert all(s["client_id"] == "agent-tenant-test-conformance" for s in seen)
    assert FAKE_JWT not in json.dumps(c.scrub(out))


async def test_t5_direct_case_is_an_error_when_the_token_file_is_missing(fake_aps, tmp_path):
    url, _ = fake_aps

    async def no_logs(since: float) -> str:
        return ""

    h = c.Harness(
        None,
        TENANT,
        AGENT,
        "r1",
        no_logs,
        aps_url=url,
        issuer_token_path=str(tmp_path / "missing"),
        aps_token_path=str(tmp_path / "missing"),
    )
    saved = c.t5_sandbox_cases
    c.t5_sandbox_cases = lambda tenant, agent: []  # type: ignore[assignment]
    try:
        out = await h.t5()
    finally:
        c.t5_sandbox_cases = saved  # type: ignore[assignment]
    assert out["result"] == "error" and {k["result"] for k in out["cases"]} == {"error"}


def test_cli_rejects_unknown_tests(capsys):
    with pytest.raises(SystemExit):
        c.parse_args(["--release", "r1", "--tests", "T4,T99"])
    args = c.parse_args(["--release", "r1", "--tests", "t5,T19,t8"])
    assert args.tests == ["T5", "T19", "T8"] and args.tenant == "tenant-test"
