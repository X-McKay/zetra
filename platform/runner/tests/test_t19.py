"""T19's complete evidence contract; no model summary participates in the verdict."""

import io
import json
import hashlib
import tarfile

import pytest

from runner import conformance as c
from tests.test_conformance import run, decision, alternation, TENANT, AGENT


def evidence():
    r = run(tool="probe_mcp", summary="truncated prose is not evidence")
    lines = alternation(r.pod, 50)
    requests = []
    for i, line in enumerate(lines):
        trace = f"{i + 1:032x}"
        line.update(trace_id=trace, token_jti="fixed-jti")
        requests.append(
            {"tool": line["tool"], "status": 200 if i % 2 == 0 else 403, "trace_id": trace}
        )
    models = []
    for i, backend in enumerate(["llm-main", "llm-fast"] * 2):
        trace = f"{i + 101:032x}"
        models.append({"backend": backend, "status": 200 if i % 2 == 0 else 403, "trace_id": trace})
        lines.append(
            decision(
                r.pod,
                None,
                "allow" if i % 2 == 0 else c.DENY_ROUTE,
                f"m{i}",
                route="inference",
                backend=backend,
                method=None,
                trace_id=trace,
                token_jti="fixed-jti",
            )
        )
    probe = {
        "calls": ["ok", "deny"] * 50,
        "requests": requests,
        "elapsed_ms": 3000.0,
        "token_jti": "fixed-jti",
        "models": models,
    }
    return r, lines, probe


def test_requires_50_cycles_actual_statuses_one_token_and_model_routes():
    r, lines, probe = evidence()
    lines.append(decision(r.pod, None, "allow", "driver", route="inference", backend="llm-main"))
    out = c.evaluate_t19(r, lines, probe)
    assert out["result"] == "pass" and all(out["checks"].values()), out
    assert len(out["evidence"]["mcp_decisions"]) == 100
    assert len(out["evidence"]["model_decisions"]) == 4
    assert out["evidence"]["elapsed_ms"] == 3000.0


def test_correlates_requests_across_aps_replicas_by_trace():
    r, lines, probe = evidence()
    out = c.evaluate_t19(r, lines[1::2] + lines[::2], probe)
    assert out["result"] == "pass"
    assert [d["decision_id"] for d in out["evidence"]["mcp_decisions"]][:4] == [
        "a0",
        "d0",
        "a1",
        "d1",
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "five_cycles",
        "slow",
        "nan_time",
        "negative_time",
        "wrong_http",
        "missing_http",
        "missing_decision",
        "duplicate_decision",
        "reused_decision",
        "wrong_tool",
        "cached_allow",
        "changed_token",
        "no_token",
        "duplicate_trace",
        "mcp_error",
        "model_429",
        "model_wrong_reason",
        "model_missing",
        "model_no_decision",
        "model_wrong_token",
        "failed_session",
        "no_probe",
    ],
)
def test_rejects_each_missing_condition(mutation):
    r, lines, probe = evidence()
    if mutation == "five_cycles":
        probe["calls"] = probe["calls"][:10]
        probe["requests"] = probe["requests"][:10]
        lines = lines[:10] + lines[100:]
    elif mutation == "slow":
        probe["elapsed_ms"] = 10000.001
    elif mutation == "nan_time":
        probe["elapsed_ms"] = float("nan")
    elif mutation == "negative_time":
        probe["elapsed_ms"] = -1
    elif mutation == "wrong_http":
        probe["requests"][1]["status"] = 200
    elif mutation == "missing_http":
        probe["requests"][0]["status"] = None
    elif mutation == "missing_decision":
        lines.pop(0)
    elif mutation == "duplicate_decision":
        lines.append(dict(lines[0]))
    elif mutation == "reused_decision":
        lines[1]["decision_id"] = lines[0]["decision_id"]
    elif mutation == "wrong_tool":
        lines[1]["tool"] = c.MCP_ALLOWED_TOOL
    elif mutation == "cached_allow":
        lines[1]["reason"] = "allow"
    elif mutation == "changed_token":
        lines[1]["token_jti"] = "other-jti"
    elif mutation == "no_token":
        probe["token_jti"] = None
    elif mutation == "duplicate_trace":
        probe["requests"][1]["trace_id"] = probe["requests"][0]["trace_id"]
    elif mutation == "mcp_error":
        probe["calls"][0] = "error"
    elif mutation == "model_429":
        probe["models"][0]["status"] = 429
    elif mutation == "model_wrong_reason":
        lines[101]["reason"] = "deny.budget_exhausted"
    elif mutation == "model_missing":
        probe["models"] = []
    elif mutation == "model_no_decision":
        lines.pop()
    elif mutation == "model_wrong_token":
        lines[-1]["token_jti"] = "other-jti"
    elif mutation == "failed_session":
        r.outcome = "FAILED"
    elif mutation == "no_probe":
        probe = None
    assert c.evaluate_t19(r, lines, probe)["result"] == "fail"


def snapshot(tmp_path, probe, *, member="state/state.json", wrong_tool=False):
    state = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "probe_files" if wrong_tool else "probe_mcp",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": json.dumps(probe)},
            {"role": "assistant", "content": "model summary is deliberately different"},
        ]
    }
    data = json.dumps(state).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(buf.getvalue()).hexdigest()
    path = tmp_path / digest[:2] / digest
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(buf.getvalue())
    return digest, path


def test_reads_exact_tool_result_from_verified_accepted_snapshot(tmp_path):
    _, _, probe = evidence()
    digest, path = snapshot(tmp_path, probe)
    assert c.read_t19_probe_result(str(tmp_path), digest) == probe
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError):
        c.read_t19_probe_result(str(tmp_path), digest)


@pytest.mark.parametrize(
    "member,wrong_tool", [("workspace/state.json", False), ("state/state.json", True)]
)
def test_never_substitutes_model_prose_or_another_tool(tmp_path, member, wrong_tool):
    _, _, probe = evidence()
    digest, _ = snapshot(tmp_path, probe, member=member, wrong_tool=wrong_tool)
    with pytest.raises((ValueError, KeyError)):
        c.read_t19_probe_result(str(tmp_path), digest)


async def test_harness_uses_snapshot_evidence_and_exact_session(monkeypatch, tmp_path):
    r, lines, probe = evidence()
    r.snapshot, _ = snapshot(tmp_path, probe)

    async def logs(since):
        return "\n".join(json.dumps(line) for line in lines)

    async def session(case):
        assert case.args["rounds"] == "50" and case.args["model_routes"] == "true"
        return r

    h = c.Harness(None, TENANT, AGENT, "r1", logs, artifact_dir=str(tmp_path))
    monkeypatch.setattr(h, "run_session", session)
    assert (await h.run(["T19"]))["result"] == "pass"


@pytest.mark.parametrize("kind", ["duplicate", "symlink", "oversized"])
def test_snapshot_rejects_ambiguous_or_unbounded_state(tmp_path, kind):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("state/state.json")
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "../workspace/other"
            tar.addfile(info)
        elif kind == "oversized":
            info.size = (1 << 20) + 1
            tar.addfile(info, io.BytesIO(b" " * info.size))
        else:
            for _ in range(2):
                tar.addfile(info)
    digest = hashlib.sha256(buf.getvalue()).hexdigest()
    path = tmp_path / digest[:2] / digest
    path.parent.mkdir()
    path.write_bytes(buf.getvalue())
    with pytest.raises(ValueError):
        c.read_t19_probe_result(str(tmp_path), digest)


def test_report_does_not_copy_unrecognized_probe_fields():
    r, lines, probe = evidence()
    probe["authorization"] = "opaque-secret"
    probe["requests"][0]["body"] = "opaque-secret"
    assert "opaque-secret" not in json.dumps(c.evaluate_t19(r, lines, probe))


def test_unreadable_aps_replica_prevents_claiming_complete_decision_evidence():
    r, lines, probe = evidence()
    lines.append({"msg": c.LOG_UNAVAILABLE, "pod": "aps-unreadable"})
    out = c.evaluate_t19(r, lines, probe)
    assert out["result"] == "fail" and out["checks"]["all_aps_logs_readable"] is False
