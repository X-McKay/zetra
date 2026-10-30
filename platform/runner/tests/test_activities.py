from __future__ import annotations

import functools
import hashlib
import io
import json
import tarfile
import tracemalloc

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from runner import activities as act
from runner.types import BoundaryIn, GenIn, SessionInput, SnapshotIn
from tests.fakes import (
    ALLOW_ALL_EGRESS,
    RELEASE,
    SESSION,
    TENANT,
    FakeAPS,
    FakeCore,
    FakeCustom,
    FakeNet,
    FakeStream,
    FakeWS,
    boundary_doc,
)


def inp(grant: str | None = None, task=None) -> SessionInput:
    return SessionInput(
        TENANT,
        "investigator",
        RELEASE,
        SESSION,
        grant,
        task if task is not None else {"goal": "list pods"},
    )


def setup(tmp_path, *, core=None, net=None, custom=None, aps=None, stream=None):
    core = core or FakeCore()
    d = act.Deps(
        core=core,
        net=net or FakeNet(),
        custom=custom or FakeCustom(),
        aps=aps or FakeAPS(),
        store=act.FilesystemStore(tmp_path / "artifacts"),
        exec=functools.partial(act.kube_exec, core, stream_fn=stream or FakeStream()),
    )
    act.configure(d)
    return d


def run(fn, arg):
    return ActivityEnvironment().run(fn, arg)


def refused(fn, arg) -> ApplicationError:
    with pytest.raises(ApplicationError) as e:
        run(fn, arg)
    return e.value


# --- preflight ------------------------------------------------------------------------------


def test_preflight_passes_for_ready_release_and_exact_policies(tmp_path):
    setup(tmp_path)
    run(act.preflight, inp())


@pytest.mark.parametrize(
    "status",
    [
        {"ready": False},  # conformance-ok after approved: false
        {"ready": None},  # conformance-unsigned never becomes ready
    ],
)
def test_t9a_preflight_refuses_unready_release(tmp_path, status):
    d = setup(tmp_path, custom=FakeCustom(status))
    err = refused(act.preflight, inp())
    assert err.type == "Forbidden" and err.non_retryable
    assert d.core.created == []


def test_preflight_refuses_release_of_another_agent(tmp_path):
    setup(tmp_path, custom=FakeCustom(agent="other"))
    assert refused(act.preflight, inp()).type == "Forbidden"


def test_t28_preflight_refuses_extra_network_policy(tmp_path):
    setup(tmp_path, net=FakeNet(extra=[ALLOW_ALL_EGRESS]))
    err = refused(act.preflight, inp())
    assert err.type == "Forbidden" and "NetworkPolicy" in str(err)


def test_t28_preflight_refuses_digest_mismatch(tmp_path):
    setup(tmp_path, core=FakeCore(digest="sha256:" + "0" * 64))
    assert refused(act.preflight, inp()).type == "Forbidden"


def test_preflight_refuses_missing_namespace_annotation(tmp_path):
    core = FakeCore()
    core.annotations = None
    setup(tmp_path, core=core)
    assert refused(act.preflight, inp()).type == "Forbidden"


def test_netpol_digest_is_the_reference_algorithm():
    from tests.fakes import netpols

    pols = {
        p.metadata.name: json.dumps(
            act.client.ApiClient().sanitize_for_serialization(p.spec),
            sort_keys=True,
            separators=(",", ":"),
        )
        for p in netpols()
    }
    want = "sha256:" + hashlib.sha256(json.dumps(pols, sort_keys=True).encode()).hexdigest()
    names, have = act.netpol_digest(netpols())
    assert names == act.EXPECTED_POLICIES and have == want


DELEGATED = {"identityMode": "on-behalf-of", "delegationRequired": True}


def test_preflight_refuses_delegated_session_without_grant(tmp_path):
    setup(tmp_path, custom=FakeCustom(DELEGATED))
    assert refused(act.preflight, inp(grant=None)).type == "Forbidden"


def test_preflight_refuses_unknown_grant(tmp_path):
    setup(tmp_path, custom=FakeCustom(DELEGATED))
    assert refused(act.preflight, inp(grant="gh_missing")).type == "Forbidden"


def test_preflight_refuses_grant_of_another_session(tmp_path):
    aps = FakeAPS(grants={"gh_1": {"session_id": "00000000-0000-4000-8000-000000000000"}})
    setup(tmp_path, custom=FakeCustom(DELEGATED), aps=aps)
    assert refused(act.preflight, inp(grant="gh_1")).type == "Forbidden"


def test_preflight_accepts_delegated_session_with_its_grant(tmp_path):
    aps = FakeAPS(grants={"gh_1": {"session_id": SESSION, "tenant": TENANT}})
    setup(tmp_path, custom=FakeCustom(DELEGATED), aps=aps)
    run(act.preflight, inp(grant="gh_1"))
    assert ("GET", "/v1/grants/gh_1") in aps.calls


def test_preflight_refuses_token_in_task(tmp_path):
    setup(tmp_path)
    jwt = "eyJ" + "a" * 20 + "." + "b" * 20
    assert refused(act.preflight, inp(task={"note": jwt})).type == "Forbidden"


# --- create_sandbox ---------------------------------------------------------------------------


def test_pod_name_values():
    digest = hashlib.sha256(SESSION.encode()).hexdigest()[:16]
    assert act.pod_name(SESSION, 1) == f"s-{digest}-g1"
    assert act.pod_name(SESSION, 3) == f"s-{digest}-g3"
    assert len(act.pod_name(SESSION, 1)) == 2 + 16 + 3


def test_create_sandbox_sets_per_session_fields_once(tmp_path):
    d = setup(tmp_path)
    uid = run(act.create_sandbox, GenIn(inp(), 2, None))
    body = d.core.created[0]
    assert uid and body["metadata"]["name"] == act.pod_name(SESSION, 2)
    labels = body["metadata"]["labels"]
    assert labels["agentplatform.io/session"] == SESSION
    assert labels["agentplatform.io/generation"] == "2"
    env = body["spec"]["containers"][0]["env"]
    session_env = [e for e in env if e["name"] == "AGENT_SESSION_ID"]
    assert session_env == [{"name": "AGENT_SESSION_ID", "value": SESSION}]


def test_create_sandbox_409_for_same_identity_is_idempotent(tmp_path):
    d = setup(tmp_path)
    first = run(act.create_sandbox, GenIn(inp(), 1, None))
    assert run(act.create_sandbox, GenIn(inp(), 1, None)) == first
    assert len(d.core.created) == 1


def test_create_sandbox_409_identity_conflict(tmp_path):
    d = setup(tmp_path)
    run(act.create_sandbox, GenIn(inp(), 1, None))
    pod = d.core.pods[act.pod_name(SESSION, 1)]
    pod.metadata.labels["agentplatform.io/release"] = "someone-else"
    err = refused(act.create_sandbox, GenIn(inp(), 1, None))
    assert err.type == "IdentityConflict" and err.non_retryable


def test_create_sandbox_admission_refusal_is_final(tmp_path):
    core = FakeCore()
    core.reject_create = 403
    setup(tmp_path, core=core)
    err = refused(act.create_sandbox, GenIn(inp(), 1, None))
    assert err.type == "Forbidden" and err.non_retryable


def test_activities_module_does_not_load_cluster_config_at_import():
    import subprocess
    import sys

    code = "import runner.activities as a, runner.workflow; assert a._DEPS is None"
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}  # no kubeconfig, no cluster
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


# --- run_boundary: section 10.4 and T25 ----------------------------------------------------------


def boundary(tmp_path, *sockets, refuse=False):
    stream = FakeStream(*sockets, refuse=refuse)
    setup(tmp_path, stream=stream)
    return stream


def test_run_boundary_frames_input_and_parses_output(tmp_path):
    stream = boundary(tmp_path, FakeWS([boundary_doc(0)], rc=0))
    out = run(act.run_boundary, BoundaryIn(inp(), 1, 0))
    assert out.boundary == 0 and not out.done and out.usage.input_tokens == 10
    ws = stream.used[0]
    header, body = bytes(ws.stdin).split(b"\n", 1)
    assert int(header) == len(body) and json.loads(body)["boundary"] == 0
    assert stream.commands[0] == act.STEP_CMD
    assert stream.kwargs[0]["binary"] is True and stream.kwargs[0]["container"] == "agent"


def test_run_boundary_completion(tmp_path):
    boundary(tmp_path, FakeWS([boundary_doc(4, done=True)], rc=2))
    assert run(act.run_boundary, BoundaryIn(inp(), 1, 4)).done


def unknown(tmp_path, *sockets) -> ApplicationError:
    boundary(tmp_path, *sockets)
    err = refused(act.run_boundary, BoundaryIn(inp(), 1, 3))
    assert err.type == "OutcomeUnknown" and not err.non_retryable
    return err


def test_t25_ten_mib_output_is_outcome_unknown_with_bounded_memory(tmp_path):
    chunk = b"x" * 32768
    ws = FakeWS([chunk] * 320, rc=0)  # 10 MiB
    tracemalloc.start()
    unknown(tmp_path, ws)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert ws.closed and ws.pulled <= act.MAX_OUTPUT + len(chunk)
    assert peak < 2 * 1024 * 1024


def test_t25_invalid_json(tmp_path):
    unknown(tmp_path, FakeWS([b"{not json"], rc=0))


def test_t25_wrong_boundary_number(tmp_path):
    unknown(tmp_path, FakeWS([boundary_doc(7)], rc=0))


def test_t25_done_with_exit_zero(tmp_path):
    unknown(tmp_path, FakeWS([boundary_doc(3, done=True)], rc=0))


def test_t25_not_done_with_completion_code(tmp_path):
    unknown(tmp_path, FakeWS([boundary_doc(3)], rc=2))


@pytest.mark.parametrize("rc", [1, 5, 137, -1, None])
def test_t25_unknown_exit_code(tmp_path, rc):
    unknown(tmp_path, FakeWS([boundary_doc(3)], rc=rc))


def test_t25_no_output(tmp_path):
    unknown(tmp_path, FakeWS([], rc=0))


@pytest.mark.parametrize(
    "over",
    [
        {"extra": 1},
        {"boundary": "3"},
        {"boundary": True},
        {"done": 0},
        {"kind": "shell"},
        {"summary": "s" * 513},
        {"usage": {"input_tokens": 1}},
        {"usage": {"input_tokens": -1, "output_tokens": 0}},
    ],
)
def test_t25_schema_violations(tmp_path, over):
    unknown(tmp_path, FakeWS([boundary_doc(3, **over)], rc=0))


def test_t25_hang_is_outcome_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(act, "EXEC_DEADLINE", 0.2)
    ws = FakeWS([], rc=0, never_close=True)
    unknown(tmp_path, ws)
    assert ws.closed


def test_exec_lost_midway_is_exec_failed(tmp_path):
    boundary(tmp_path, FakeWS([b"{"] * 10, fail_after=2))
    assert refused(act.run_boundary, BoundaryIn(inp(), 1, 0)).type == "ExecFailed"


def test_exec_not_started_is_sandbox_lost(tmp_path):
    boundary(tmp_path, refuse=True)
    assert refused(act.run_boundary, BoundaryIn(inp(), 1, 0)).type == "SandboxLost"


def test_agent_error_keeps_summary_out_of_the_message(tmp_path):
    secret_summary = "customer record 4711"
    boundary(tmp_path, FakeWS([boundary_doc(0, summary=secret_summary)], rc=3))
    err = refused(act.run_boundary, BoundaryIn(inp(), 1, 0))
    assert err.type == "AgentError" and err.non_retryable
    assert secret_summary not in str(err) and err.details == (secret_summary,)


def test_budget_exceeded(tmp_path):
    boundary(tmp_path, FakeWS([boundary_doc(0)], rc=4))
    assert refused(act.run_boundary, BoundaryIn(inp(), 1, 0)).type == "BudgetExceeded"


# --- snapshot and restore -------------------------------------------------------------------------


def workspace_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"# Report\n3 pods\n"
        info = tarfile.TarInfo("workspace/report.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_snapshot_is_content_addressed_and_restored(tmp_path):
    blob = workspace_tar()
    stream = FakeStream(FakeWS([blob[:4096], blob[4096:]], rc=0), FakeWS([], rc=0))
    d = setup(tmp_path, stream=stream)
    key = run(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
    assert key == hashlib.sha256(blob).hexdigest()
    assert "--exclude=state/token.json" in stream.commands[0]
    with d.store.open(key) as f, tarfile.open(fileobj=f) as tar:
        assert "workspace/report.md" in tar.getnames()
    # A new generation restores the committed snapshot before it is used.
    run(act.create_sandbox, GenIn(inp(), 2, key))
    run(act.wait_ready, GenIn(inp(), 2, key))
    assert stream.commands[1] == act.RESTORE_CMD + [str(len(blob))]
    assert bytes(stream.used[1].stdin) == blob


def test_snapshot_over_limit_is_snapshot_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(act, "MAX_SNAPSHOT", 1 << 20)
    ws = FakeWS([b"\0" * 65536] * 32, rc=0)
    setup(tmp_path, stream=FakeStream(ws))
    err = refused(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
    assert err.type == "SnapshotFailed" and err.non_retryable and ws.closed


def test_snapshot_nonzero_exit_is_retryable_snapshot_failed(tmp_path):
    setup(tmp_path, stream=FakeStream(FakeWS([b"partial"], rc=2)))
    err = refused(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
    assert err.type == "SnapshotFailed" and not err.non_retryable


def test_restore_refuses_tampered_snapshot(tmp_path):
    d = setup(tmp_path)
    key = hashlib.sha256(b"original").hexdigest()
    d.store.put(key, io.BytesIO(b"original"))
    path = d.store._path(key)
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    run(act.create_sandbox, GenIn(inp(), 2, key))
    assert refused(act.wait_ready, GenIn(inp(), 2, key)).type == "RestoreFailed"


def test_store_is_write_once(tmp_path):
    store = act.FilesystemStore(tmp_path)
    key = hashlib.sha256(b"a").hexdigest()
    store.put(key, io.BytesIO(b"a"))
    store.put(key, io.BytesIO(b"b"))
    assert store.open(key).read() == b"a"
    with pytest.raises(ValueError):
        store.put("../escape", io.BytesIO(b"x"))


# --- wait_ready, delete, stop, grant ----------------------------------------------------------------


def test_wait_ready_sandbox_lost_when_pod_failed(tmp_path):
    core = FakeCore()
    core.phase = "Failed"
    setup(tmp_path, core=core)
    run(act.create_sandbox, GenIn(inp(), 1, None))
    err = refused(act.wait_ready, GenIn(inp(), 1, None))
    assert err.type == "SandboxLost" and err.non_retryable


def test_delete_sandbox_confirms_404(tmp_path):
    d = setup(tmp_path)
    run(act.create_sandbox, GenIn(inp(), 1, None))
    assert run(act.delete_sandbox, GenIn(inp(), 1, None)) == "DELETED"
    assert run(act.delete_sandbox, GenIn(inp(), 1, None)) == "DELETED"  # already gone
    assert d.core.pods == {}


def test_delete_sandbox_not_confirmed_is_a_value(tmp_path, monkeypatch):
    monkeypatch.setattr(act, "DELETE_DEADLINE", 0.05)
    monkeypatch.setattr(act, "POLL", 0.01)
    d = setup(tmp_path)
    run(act.create_sandbox, GenIn(inp(), 1, None))
    d.core.finalized.add(act.pod_name(SESSION, 1))
    assert run(act.delete_sandbox, GenIn(inp(), 1, None)) == "NOT_CONFIRMED"


def test_stop_and_grant_calls(tmp_path):
    d = setup(tmp_path)
    assert run(act.stop_session, inp("gh_1")) == "OK"
    assert run(act.delete_grant, inp("gh_1")) == "OK"
    assert d.aps.calls == [("POST", f"/v1/sessions/{SESSION}/stop"), ("DELETE", "/v1/grants/gh_1")]
