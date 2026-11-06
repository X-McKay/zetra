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
        out.append(decision(pod, c.MCP_ALLOWED_TOOL, "allow", f"a{i}", ts=f"t{2 * i:03d}"))
        out.append(
            decision(pod, c.MCP_FORBIDDEN_TOOL, c.DENY_ROUTE, f"d{i}", ts=f"t{2 * i + 1:03d}")
        )
    return out


def capture(
    session: str, jti: str, sub: str = "agent-sa", act: str = "", ts: str = "t1", **agent
) -> dict:
    return {
        "ts": ts,
        "method": "POST",
        "path": "/kubernetes/mcp",
        "status": 200,
        "ms": 3,
        "x_agent": {"x-agent-id": f"{TENANT}/{AGENT}", "x-agent-session-id": session, **agent},
        "bearer": {
            "sub": sub,
            "act_sub": act,
            "aud": ["https://mcp.almckay.io/kubernetes/mcp"],
            "azp": "agent-policy-service",
            "jti": jti,
        },
    }


GATEWAY_LINE = (
    "2026-11-05T22:17:40.1Z\tinfo\trequest gateway=ai-gateway/agents listener=agents-https "
    "route=ai-gateway/agents-mcp-kubernetes src.addr=10.42.1.93:1 http.method=POST "
    "http.host=mcp.agents.internal http.path=/kubernetes/mcp http.version=HTTP/1.1 http.status=200 "
    "jwt.sub=dcf3e4d4-7779-4a71-835f-6e278d278b9d protocol=mcp mcp.method.name=tools/call "
    'duration=6ms agent.id="tenant-test/conformance" agent.session_id="{session}" '
    'agent.decision="allow" agent.decision_id="d0" error="mcp: denied for tool: x"'
)


def test_every_case_is_a_valid_session_input():
    for case in [
        c.t4_case(),
        *c.t5_sandbox_cases(TENANT, AGENT),
        c.t8_case(TENANT),
        c.t11_case(TENANT),
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
    assert [d.expect_status for d in c.t5_direct_cases("/i", "/a")] == [403, 401, 401]


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


def test_unreadable_aps_pods_are_evidence_not_silence():
    text = "\n".join(
        [
            json.dumps({"msg": c.LOG_UNAVAILABLE, "pod": "aps-b", "error": "ApiException"}),
            json.dumps(issued("s-1-g1")),
        ]
    )
    lines = c.parse_aps_log(text)
    assert c.unreadable_pods(lines) == ["aps-b"]
    out = c.evaluate_t4(run(), lines, TENANT)
    assert out["evidence"]["aps_log_unreadable"] == ["aps-b"]


def test_aps_log_reader_retries_a_failed_pod_read():
    class Core:
        def __init__(self):
            self.calls = 0

        def list_namespaced_pod(self, ns, label_selector):
            return types.SimpleNamespace(
                items=[types.SimpleNamespace(metadata=types.SimpleNamespace(name="aps-a"))]
            )

        def read_namespaced_pod_log(self, name, ns, container, since_seconds):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return json.dumps(issued("s-1-g1"))

    core = Core()
    text = asyncio.run(c.aps_log_reader(core)(5.0))
    assert core.calls == 2 and c.parse_aps_log(text)[0]["msg"] == "token issued"


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


def test_capture_and_gateway_logs_are_parsed():
    text = (
        json.dumps(capture(SESSION, "trrtte:1"))
        + "\n"
        + json.dumps({"msg": "other"})
        + "\nnot json"
    )
    [rec] = c.parse_capture_log(text)
    assert rec["bearer"]["jti"] == "trrtte:1"
    [line] = c.parse_gateway_log(
        GATEWAY_LINE.format(session=SESSION) + "\nnoise without the path key"
    )
    assert line["jwt.sub"] == "dcf3e4d4-7779-4a71-835f-6e278d278b9d"
    assert line["agent.session_id"] == SESSION and line["http.status"] == "200"
    assert line["error"] == "mcp: denied for tool: x"
    r = run(tool="probe_mcp")
    assert c.inbound_subjects(r, [line]) == {"dcf3e4d4-7779-4a71-835f-6e278d278b9d"}
    other = capture("other-session", "trrtte:9", ts="t0")
    ordered = c.captures_of(
        r, [other, capture(SESSION, "trrtte:2", ts="t2"), capture(SESSION, "trrtte:1", ts="t1")]
    )
    assert [x["bearer"]["jti"] for x in ordered] == ["trrtte:1", "trrtte:2"]


def exchanged(pod: str, jti: str, did: str = "a0") -> dict:
    return {
        **decision(pod, c.MCP_ALLOWED_TOOL, "allow", did),
        "delegation": "agent",
        "exchange": {"cached": False, "ms": 100, "upstream_jti": jti},
    }


SUB = "dcf3e4d4-7779-4a71-835f-6e278d278b9d"


def test_t11_passes_when_the_server_saw_the_agents_own_subject_without_act():
    r = run(tool="probe_mcp", summary='{"calls":["ok"]}')
    caps = [capture(SESSION, "trrtte:1", sub=SUB), capture(SESSION, "trrtte:1", sub=SUB, ts="t2")]
    gw = c.parse_gateway_log(GATEWAY_LINE.format(session=SESSION))
    out = c.evaluate_t11(r, [exchanged(r.pod, "trrtte:1")], caps, gw, TENANT, AGENT)
    assert out["result"] == "pass" and all(out["checks"].values()), out["checks"]
    assert out["evidence"]["inbound_subjects"] == [SUB]
    assert json.dumps(out).count("eyJ") == 0


@pytest.mark.parametrize(
    "caps,gw_session,failed_check",
    [
        ([], SESSION, "upstream_captured"),
        ([capture(SESSION, "j", sub="someone-else")], SESSION, "sub_is_the_agent"),
        ([capture(SESSION, "j", sub=SUB, act="agent")], SESSION, "no_act"),
        ([capture(SESSION, "j", sub=SUB)], "another", "inbound_subject_known"),
    ],
)
def test_t11_fails_without_its_required_evidence(caps, gw_session, failed_check):
    r = run(tool="probe_mcp")
    gw = c.parse_gateway_log(GATEWAY_LINE.format(session=gw_session))
    out = c.evaluate_t11(r, [exchanged(r.pod, "j")], caps, gw, TENANT, AGENT)
    assert out["result"] == "fail" and out["checks"][failed_check] is False


def test_t12_passes_when_only_exchanged_tokens_reached_the_server():
    r = run(tool="probe_mcp")
    lines = [{**issued(r.pod), "jti": "trrtcc:agent"}, exchanged(r.pod, "trrtte:up")]
    caps = [capture(SESSION, "trrtte:up"), capture(SESSION, "trrtte:up", ts="t2")]
    out = c.evaluate_t12(r, lines, caps, TENANT)
    assert out["result"] == "pass" and all(out["checks"].values()), out["checks"]
    assert out["evidence"]["agent_token_jti"] == ["trrtcc:agent"]


@pytest.mark.parametrize(
    "caps,failed_check",
    [
        ([capture(SESSION, "trrtcc:agent")], "agent_token_never_at_server"),
        ([capture(SESSION, "trrtte:unknown")], "every_upstream_token_was_exchanged"),
        ([], "upstream_captured"),
    ],
)
def test_t12_fails_when_the_agent_token_or_a_stranger_reaches_the_server(caps, failed_check):
    r = run(tool="probe_mcp")
    lines = [{**issued(r.pod), "jti": "trrtcc:agent"}, exchanged(r.pod, "trrtte:up")]
    out = c.evaluate_t12(r, lines, caps, TENANT)
    assert out["result"] == "fail" and out["checks"][failed_check] is False


def test_t8_upstream_half_checks_what_the_server_received():
    r = run(tool="probe_mcp", summary='{"calls":["ok"]}')
    line = decision(r.pod, c.MCP_ALLOWED_TOOL, "allow", "a0")
    out = c.evaluate_t8(r, [line], TENANT, AGENT, [capture(SESSION, "j")])
    assert out["result"] == "pass" and all(out["checks"].values()), out["checks"]
    assert out["evidence"]["upstream_capture"][0]["x_agent"]["x-agent-session-id"] == SESSION
    forged = capture(SESSION, "j", **{"x-agent-principal": "mallory"})
    out = c.evaluate_t8(r, [line], TENANT, AGENT, [forged])
    assert out["result"] == "fail" and out["checks"]["upstream_no_forged_value"] is False
    reason = capture(SESSION, "j", **{"x-agent-decision-reason": "allow"})
    out = c.evaluate_t8(r, [line], TENANT, AGENT, [reason])
    assert out["result"] == "fail" and out["checks"]["upstream_no_decision_reason"] is False
    out = c.evaluate_t8(r, [line], TENANT, AGENT, [])
    assert out["result"] == "fail" and out["checks"]["upstream_captured"] is False
    out = c.evaluate_t8(r, [line], TENANT, AGENT, None)
    assert out["result"] == "pass" and "not captured" in out["evidence"]["upstream_capture"]


def jwt(claims: dict) -> str:
    import base64

    seg = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    return seg(b'{"alg":"none"}') + "." + seg(json.dumps(claims).encode()) + "." + seg(b"sig")


def test_t26_reads_the_stashed_token_from_the_snapshot_store(tmp_path):
    import hashlib
    import io
    import tarfile

    token = jwt({"jti": "trrtcc:stash", "aud": "agent-gateway"})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = token.encode()
        info = tarfile.TarInfo("workspace/t26/token")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        other = tarfile.TarInfo("workspace/report.md")
        other.size = 2
        tar.addfile(other, io.BytesIO(b"hi"))
    key = hashlib.sha256(buf.getvalue()).hexdigest()
    (tmp_path / key[:2]).mkdir()
    (tmp_path / key[:2] / key).write_bytes(buf.getvalue())
    assert c.read_stashed_token(str(tmp_path), key) == token
    assert c.jwt_jti(token) == "trrtcc:stash" and c.jwt_jti("opaque") is None
    with pytest.raises(ValueError):
        c.read_stashed_token(str(tmp_path), "not-a-key")


def test_t26_present_case_presents_the_stash_and_carries_no_token():
    case = c.t26_present_case("same_agent_other_session")
    assert case.tool == "probe_http" and case.args["auth"] == "stash" and case.expect_status == 403
    assert case.args["host_header"] == c.MCP_HOST and case.args["path"] == "/kubernetes/mcp"
    assert "Authorization" not in json.loads(case.args["headers"])
    assert json.loads(case.args["body"])["method"] == "initialize"
    SessionInput(TENANT, AGENT, "r1", SESSION, None, case.task(), seed="ab" * 32).validate()
    with pytest.raises(Exception, match="seed"):
        SessionInput(TENANT, AGENT, "r1", SESSION, None, case.task(), seed="nope").validate()
    with pytest.raises(Exception, match="tokens or credentials"):
        # The channel the spec forbids: a token in the session input.
        SessionInput(TENANT, AGENT, "r1", SESSION, None, {"token": FAKE_JWT}).validate()


def t26_runs():
    stash = run(tool="probe_token_stash", summary='{"stashed":"/sandbox/workspace/t26/token"}')
    stash.snapshot = "ab" * 32
    same = run(
        tool="probe_http",
        summary='{"status": 403, "body": "forbidden"}',
        session="1" * 8 + "-2222-4333-8444-" + "5" * 12,
    )
    same.pod = pod_name(same.session_id, 1)
    other = run(
        tool="probe_http",
        summary='{"status": 403, "body": "forbidden"}',
        session="2" * 8 + "-2222-4333-8444-" + "5" * 12,
    )
    other.pod = pod_name(other.session_id, 1)
    return stash, same, other


def t26_lines(stash, same, other, same_reason=c.DENY_BOUND, other_reason=c.DENY_MISMATCH):
    return [
        {**issued(stash.pod), "jti": "trrtcc:stash"},
        {**decision(same.pod, None, same_reason, "d1"), "method": "initialize", "tool": None},
        {**decision(other.pod, None, other_reason, "d2"), "method": "initialize", "tool": None},
    ]


def test_t26_passes_when_both_foreign_sandboxes_are_refused_for_the_right_reason():
    stash, same, other = t26_runs()
    out = c.evaluate_t26(
        stash, same, other, "trrtcc:stash", None, t26_lines(stash, same, other), TENANT
    )
    assert out["result"] == "pass" and all(out["checks"].values()), out["checks"]
    assert out["evidence"]["token_jti"] == "trrtcc:stash"
    assert "eyJ" not in json.dumps(out)


@pytest.mark.parametrize(
    "same_reason,other_reason,jti,read_error,failed_check",
    [
        ("allow", c.DENY_MISMATCH, "trrtcc:stash", None, "same_agent_never_allowed"),
        (
            c.DENY_MISMATCH,
            c.DENY_MISMATCH,
            "trrtcc:stash",
            None,
            "same_agent_denied_bound_elsewhere",
        ),
        (c.DENY_BOUND, "allow", "trrtcc:stash", None, "other_agent_never_allowed"),
        (c.DENY_BOUND, c.DENY_BOUND, "trrtcc:stash", None, "other_agent_denied_mismatch"),
        (c.DENY_BOUND, c.DENY_MISMATCH, "trrtcc:someone-else", None, "token_is_the_stash_pods"),
        (
            c.DENY_BOUND,
            c.DENY_MISMATCH,
            None,
            "FileNotFoundError: workspace/t26/token",
            "token_read_from_snapshot",
        ),
    ],
)
def test_t26_fails_without_its_required_evidence(
    same_reason, other_reason, jti, read_error, failed_check
):
    stash, same, other = t26_runs()
    lines = t26_lines(stash, same, other, same_reason, other_reason)
    out = c.evaluate_t26(stash, same, other, jti, read_error, lines, TENANT)
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
        api_token_path=str(tmp_path / "missing"),
        human_token_path=str(tmp_path / "missing"),
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
    args = c.parse_args(
        [
            "--release",
            "r1",
            "--tests",
            "t5,T19,t8,t11,T12,t26",
            "--agent-b",
            "b",
            "--release-b",
            "rb",
        ]
    )
    assert args.tests == ["T5", "T19", "T8", "T11", "T12", "T26"] and args.tenant == "tenant-test"
    assert args.agent_b == "b" and args.release_b == "rb"


@pytest.fixture
def fake_model():
    seen = []
    key = "test-static-engine-key"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            auth = self.headers.get("Authorization")
            seen.append((self.path, auth))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/v1/models")
            else:
                self.send_response(200 if auth == f"Bearer {key}" else 401)
            self.end_headers()
            # An upstream can echo credentials: the harness must never retain its body.
            self.wfile.write(key.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", key, seen
    server.shutdown()
    server.server_close()


async def empty_logs(since):
    return ""


async def test_t6_proves_engine_auth_without_outputting_key_or_response(fake_model, tmp_path):
    url, key, seen = fake_model
    keyfile = tmp_path / "key"
    keyfile.write_text(key + "\n")
    h = c.Harness(
        None,
        TENANT,
        AGENT,
        "r1",
        empty_logs,
        model_url=url + "/v1/models",
        backend_key_file=str(keyfile),
    )
    out = await h.run(["T6"])
    assert out["result"] == "pass", out
    assert seen == [("/v1/models", None), ("/v1/models", f"Bearer {key}")]
    assert out["tests"][0]["evidence"] == {
        "unauthenticated_status": 401,
        "authenticated_status": 200,
    }
    assert key not in json.dumps(out)


@pytest.mark.parametrize("key_text", ["wrong-key", "", "Bearer invalid", "line1\nline2"])
async def test_t6_rejects_wrong_or_invalid_key(fake_model, tmp_path, key_text):
    url, _, _ = fake_model
    keyfile = tmp_path / "key"
    keyfile.write_text(key_text)
    h = c.Harness(
        None,
        TENANT,
        AGENT,
        "r1",
        empty_logs,
        model_url=url + "/v1/models",
        backend_key_file=str(keyfile),
    )
    out = await h.run(["T6"])
    assert out["result"] != "pass"
    if key_text:
        assert key_text not in json.dumps(out)


async def test_t6_missing_file_and_unreachable_engine_are_errors(tmp_path):
    keyfile = tmp_path / "key"
    h = c.Harness(
        None,
        TENANT,
        AGENT,
        "r1",
        empty_logs,
        model_url="http://127.0.0.1:1/v1/models",
        backend_key_file=str(keyfile),
    )
    assert (await h.run(["T6"]))["result"] == "error"
    keyfile.write_text("test-engine-key")
    out = await h.run(["T6"])
    assert out["result"] == "error"
    assert "test-engine-key" not in json.dumps(out)


async def test_t6_does_not_follow_redirects_with_the_key(fake_model, tmp_path):
    url, key, seen = fake_model
    keyfile = tmp_path / "key"
    keyfile.write_text(key)
    h = c.Harness(
        None,
        TENANT,
        AGENT,
        "r1",
        empty_logs,
        model_url=url + "/redirect",
        backend_key_file=str(keyfile),
    )
    assert (await h.run(["T6"]))["result"] == "fail"
    assert [path for path, _ in seen] == ["/redirect", "/redirect"]


def t7_evidence():
    r = run()
    r.boundaries[0]["usage"] = {"input_tokens": 12, "output_tokens": 4}
    cap = {
        **capture(SESSION, ""),
        "path": "/v1/chat/completions",
        "backend_key_match": True,
        "bearer": {"error": "not_a_jwt"},
    }
    gw = {
        "agent.session_id": SESSION,
        "http.method": "POST",
        "http.status": "200",
        "http.path": "/models/llm-main/v1/chat/completions",
        "jwt.sub": "agent-sub",
        "agent.decision": "allow",
        "llm.tokens_in": "12",
        "llm.tokens_out": "4",
    }
    return r, cap, gw


def test_t7_requires_matching_backend_key_chat_path_and_nonzero_usage():
    r, cap, gw = t7_evidence()
    out = c.evaluate_t7(r, [cap, {**cap, "x_agent": {"x-agent-session-id": "other"}}], [gw])
    assert out["result"] == "pass" and all(out["checks"].values()), out
    assert len(out["evidence"]["model_capture"]) == 1


@pytest.mark.parametrize(
    "mutation,failed_check",
    [
        ("wrong_path", "chat_completion_at_model_server"),
        ("get", "chat_completion_at_model_server"),
        ("capture_401", "model_server_status_200"),
        ("no_capture", "model_server_captured"),
        ("wrong_session", "model_server_captured"),
        ("false_key", "backend_key_received"),
        ("absent_key", "backend_key_received"),
        ("truthy_key", "backend_key_received"),
        ("jwt", "no_agent_jwt_at_model_server"),
        ("flat_jti", "no_agent_jwt_at_model_server"),
        ("flat_bearer_jti", "no_agent_jwt_at_model_server"),
        ("zero_boundary", "boundary_usage_nonzero"),
        ("zero_gateway", "gateway_usage_nonzero"),
        ("no_gateway", "gateway_accepted_agent_token"),
        ("no_subject", "gateway_accepted_agent_token"),
        ("failed_session", "session_completed"),
    ],
)
def test_t7_rejects_missing_or_contradictory_evidence(mutation, failed_check):
    r, cap, gw = t7_evidence()
    caps, lines = [cap], [gw]
    if mutation == "wrong_path":
        cap["path"] = "/v1/models"
    elif mutation == "get":
        cap["method"] = "GET"
    elif mutation == "capture_401":
        cap["status"] = 401
    elif mutation == "no_capture":
        caps = []
    elif mutation == "wrong_session":
        cap["x_agent"]["x-agent-session-id"] = "other"
    elif mutation == "false_key":
        cap["backend_key_match"] = False
    elif mutation == "absent_key":
        del cap["backend_key_match"]
    elif mutation == "truthy_key":
        cap["backend_key_match"] = "true"
    elif mutation == "jwt":
        cap["bearer"] = {"jti": "agent-jti"}
    elif mutation == "flat_jti":
        cap["jti"] = "agent-jti"
    elif mutation == "flat_bearer_jti":
        cap["bearer_jti"] = "agent-jti"
    elif mutation == "zero_boundary":
        r.boundaries[0]["usage"] = None
    elif mutation == "zero_gateway":
        gw.update({"llm.tokens_in": "0", "llm.tokens_out": "0"})
    elif mutation == "no_gateway":
        lines = []
    elif mutation == "no_subject":
        del gw["jwt.sub"]
    elif mutation == "failed_session":
        r.outcome = "FAILED"
    out = c.evaluate_t7(r, caps, lines)
    assert out["result"] == "fail" and out["checks"][failed_check] is False, out


def test_t7_does_not_copy_arbitrary_capture_fields_to_report():
    r, cap, gw = t7_evidence()
    cap["authorization"] = "opaque-test-key"
    cap["bearer"]["unexpected"] = "opaque-test-key"
    gw["request.headers.authorization"] = "opaque-test-key"
    assert "opaque-test-key" not in json.dumps(c.evaluate_t7(r, [cap], [gw]))


async def test_t7_harness_reads_model_capture_separately_from_mcp(monkeypatch):
    r, cap, gw = t7_evidence()

    async def model_logs(since):
        return json.dumps(cap)

    h = c.Harness(
        None,
        TENANT,
        AGENT,
        "r1",
        empty_logs,
        capture_logs=empty_logs,
        model_capture_logs=model_logs,
    )

    async def session(case):
        assert case.tool == "probe_files"
        return r

    async def gateway(started):
        return [gw]

    monkeypatch.setattr(h, "run_session", session)
    monkeypatch.setattr(h, "gateway_since", gateway)
    assert (await h.run(["T7"]))["result"] == "pass"


def test_cli_selects_model_auth_tests():
    assert c.parse_args(["--release", "r1", "--tests", "t6,t7"]).tests == ["T6", "T7"]


async def test_cluster_wiring_uses_engine_env_and_vllm_capture(fake_model, tmp_path, monkeypatch):
    from kubernetes import client as k8s, config
    from runner import worker

    url, key, seen = fake_model
    keyfile = tmp_path / "key"
    keyfile.write_text(key)
    monkeypatch.setenv("T6_MODEL_URL", url + "/v1/models")
    monkeypatch.setenv("BACKEND_KEY_FILE", str(keyfile))
    r, cap, gw = t7_evidence()

    class Core:
        def list_namespaced_pod(self, ns, label_selector):
            name = {
                ("vllm", "app=vllm"): "model-pod",
                ("ai-gateway", "app.kubernetes.io/name=agents"): "gateway-pod",
            }.get((ns, label_selector))
            return types.SimpleNamespace(
                items=[types.SimpleNamespace(metadata=types.SimpleNamespace(name=name))]
                if name
                else []
            )

        def read_namespaced_pod_log(self, name, ns, since_seconds, container=None):
            if (name, ns, container) == ("model-pod", "vllm", "capture"):
                return json.dumps(cap)
            if (name, ns, container) == ("gateway-pod", "ai-gateway", None):
                return " ".join(f"{k}={v}" for k, v in gw.items())
            return ""

    async def connect(tenant):
        return None

    async def session(self, case):
        return r

    monkeypatch.setattr(k8s, "CoreV1Api", Core)
    monkeypatch.setattr(config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(worker, "connect", connect)
    monkeypatch.setattr(c.Harness, "run_session", session)
    args = c.parse_args(["--release", "r1", "--tests", "T6,T7"])
    out = await c.run_in_cluster(args)
    assert out["result"] == "pass", out
    assert seen == [("/v1/models", None), ("/v1/models", f"Bearer {key}")]
    assert key not in json.dumps(out)
