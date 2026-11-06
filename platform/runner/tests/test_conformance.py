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


def test_t19_orders_decisions_by_time_across_aps_replicas():
    # Two APS pods: the log text is one pod's lines after the other's (run 7).
    r = run(tool="probe_mcp", summary='{"calls":["ok","deny","ok","deny","ok","deny"]}')
    lines = alternation(r.pod, 3)
    pod_a, pod_b = lines[1:4] + [lines[5]], [lines[0], lines[4]]
    out = c.evaluate_t19(r, pod_a + pod_b, rounds=3)
    assert out["checks"]["exact_alternation"] is True and out["result"] == "pass"
    assert [d["decision_id"] for d in out["evidence"]["decisions"]] == [
        "a0",
        "d0",
        "a1",
        "d1",
        "a2",
        "d2",
    ]


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
