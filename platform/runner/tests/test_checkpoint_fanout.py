"""The pinned gateway's stateless legacy MCP expansion, not synthetic extra reads."""

import copy

import pytest

from runner.conformance_checkpoint import read_evidence


def traffic():
    aps, gateway, capture = [], [], []
    for index, method in enumerate(
        ("initialize", "notifications/initialized", "tools/list", "tools/call")
    ):
        key = f"decision-{index}"
        aps.append(
            {
                "session_id": "session",
                "route": "mcp",
                "backend": "kubernetes",
                "decision_id": key,
                "method": method,
                "generation": 1,
                "reason": "allow",
                "pod": "pod",
                "tool": "pods_list_in_namespace" if method == "tools/call" else None,
            }
        )
        gateway.append(
            {
                "agent.session_id": "session",
                "agent.decision_id": key,
                "http.path": "/kubernetes/mcp",
                "http.status": "202" if method == "notifications/initialized" else "200",
            }
        )
        methods = (
            [method]
            if method == "initialize"
            else ["initialize", "notifications/initialized", method]
        )
        for position, rpc in enumerate(methods):
            capture.append(
                {
                    "x_agent": {
                        "x-agent-session-id": "session",
                        "x-agent-decision-id": key,
                        "x-agent-id": "tenant-test/conformance-checkpoint",
                        "x-agent-generation": "1",
                    },
                    "method": "POST",
                    "path": "/mcp",
                    "rpc_method": rpc,
                    "status": 202 if rpc == "notifications/initialized" else 200,
                }
            )
    return aps, gateway, capture


def test_exact_stateless_expansion_has_ten_requests_but_one_completed_read():
    aps, gateway, capture = traffic()
    evidence = read_evidence("session", aps, gateway, capture)
    assert len(capture) == 10
    assert evidence["read_coverage"]["complete"] is True
    assert len(evidence["reads"]) == 1
    read = evidence["reads"][0]
    assert read["capture_count"] == 1 and read["capture_status"] == 200
    assert read["backend_request_count"] == 3 and read["handshake_count"] == 2
    assert read["capture_bound"] is True
    assert evidence["read_coverage"]["backend_requests"] == 10


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_tool",
        "unknown",
        "extra_unknown",
        "missing_handshake",
        "missing_metadata",
        "wrong_identity",
        "wrong_generation",
        "wrong_session",
        "wrong_path",
        "non_post",
        "tool_failure",
        "handshake_failure",
        "reordered",
        "extra_initialize",
        "orphan",
        "duplicate_gateway",
        "duplicate_aps",
        "empty_id",
        "wrong_aps_method",
        "gateway_failure",
    ],
)
def test_backend_fanout_is_exact_not_a_greater_than_one_count(fault):
    aps, gateway, capture = traffic()
    if fault == "duplicate_tool":
        capture.append(copy.deepcopy(capture[-1]))
    elif fault == "extra_unknown":
        capture.append(dict(copy.deepcopy(capture[-1]), rpc_method="unclassified"))
    elif fault == "unknown":
        capture[-2]["rpc_method"] = "unclassified"
    elif fault == "missing_handshake":
        capture.pop(-2)
    elif fault == "missing_metadata":
        capture[-1].pop("rpc_method")
    elif fault == "wrong_identity":
        capture[-2]["x_agent"]["x-agent-id"] = "tenant-test/other"
    elif fault == "wrong_generation":
        capture[-2]["x_agent"]["x-agent-generation"] = "2"
    elif fault == "wrong_session":
        capture[-2]["x_agent"]["x-agent-session-id"] = "other"
    elif fault == "wrong_path":
        capture[-1]["path"] = "/other/mcp"
    elif fault == "non_post":
        capture[-1]["method"] = "GET"
    elif fault == "tool_failure":
        capture[-1]["status"] = 403
    elif fault == "handshake_failure":
        capture[-3]["status"] = 500
    elif fault == "reordered":
        capture[-3], capture[-1] = capture[-1], capture[-3]
    elif fault == "extra_initialize":
        capture.insert(0, copy.deepcopy(capture[0]))
    elif fault == "orphan":
        capture[-1]["x_agent"]["x-agent-decision-id"] = "orphan"
    elif fault == "duplicate_gateway":
        gateway.append(copy.deepcopy(gateway[-1]))
    elif fault == "duplicate_aps":
        aps.append(copy.deepcopy(aps[-1]))
    elif fault == "empty_id":
        capture[-1]["x_agent"]["x-agent-decision-id"] = ""
    elif fault == "wrong_aps_method":
        aps[-1]["method"] = "resources/read"
    elif fault == "gateway_failure":
        gateway[-1]["http.status"] = "500"
    assert read_evidence("session", aps, gateway, capture)["read_coverage"]["complete"] is False


def test_same_decision_id_cannot_belong_to_another_aps_session():
    aps, gateway, capture = traffic()
    aps.append(dict(aps[-1], session_id="other"))
    assert read_evidence("session", aps, gateway, capture)["read_coverage"]["complete"] is False
