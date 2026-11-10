from __future__ import annotations

import dataclasses
import functools
import hashlib
import io
import json
import tarfile
import tracemalloc
from datetime import UTC, datetime

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
    policy_bundle,
    policy_digest,
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
    assert run(act.preflight, inp()) == 64


def bundled(tmp_path, bundle: str, digest: str | None = None):
    """A release whose policy ConfigMap holds `bundle` and whose status names `digest`."""
    core = FakeCore()
    core.policy = {"bundle.json": bundle}
    custom = FakeCustom({"policyDigest": policy_digest(bundle) if digest is None else digest})
    return setup(tmp_path, core=core, custom=custom)


@pytest.mark.parametrize("limit", [1, 7, 64])
def test_preflight_returns_the_agents_boundary_limit_from_the_release_bundle(tmp_path, limit):
    bundled(tmp_path, policy_bundle(limit))
    assert run(act.preflight, inp()) == limit


def test_preflight_refuses_a_bundle_that_is_not_the_one_the_release_names(tmp_path):
    # The ConfigMap was rewritten to a larger limit after the controller published the digest.
    bundled(tmp_path, policy_bundle(64), digest=policy_digest(policy_bundle(3)))
    err = refused(act.preflight, inp())
    assert err.type == "Forbidden" and err.non_retryable and "policy digest" in str(err)


@pytest.mark.parametrize(
    "policy,status",
    [
        (None, {}),  # no ConfigMap
        ({}, {}),  # no bundle in it
        ({"bundle.json": policy_bundle()}, {"policyDigest": None}),  # no digest to hold it to
    ],
)
def test_preflight_refuses_a_release_without_a_verifiable_bundle(tmp_path, policy, status):
    core = FakeCore()
    core.policy = policy
    d = setup(tmp_path, core=core, custom=FakeCustom(status))
    err = refused(act.preflight, inp())
    assert err.type in ("Forbidden", "NotFound") and err.non_retryable
    assert d.core.created == []


@pytest.mark.parametrize(
    "bundle",
    [
        "not json",
        "[]",
        '{"runtime":{}}',
        '{"runtime":null}',
        policy_bundle(0),
        policy_bundle(65),
        policy_bundle(True),
        policy_bundle("8"),
        policy_bundle(8.0),
    ],
)
def test_preflight_refuses_a_bundle_without_a_usable_boundary_limit(tmp_path, bundle):
    bundled(tmp_path, bundle)
    err = refused(act.preflight, inp())
    assert err.type == "Forbidden" and err.non_retryable and "boundary limit" in str(err)


def test_preflight_reads_the_bundle_only_after_every_other_refusal(tmp_path):
    core = FakeCore()
    core.policy = None
    setup(tmp_path, core=core, custom=FakeCustom({"ready": False}))
    assert "release not ready" in str(refused(act.preflight, inp()))


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
    [evidence] = err.details
    assert evidence["kind"] == "effective_network_policy_mismatch"
    assert evidence["session_id"] == SESSION
    assert evidence["digest"] != evidence["published_digest"]
    assert "allow-all-egress" in {p["name"] for p in evidence["policies"]}


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


@pytest.mark.parametrize(
    "stderr,stdout,diagnostic",
    [
        ("agent_runtime.step: bad length header\n", b"", "bad length header"),
        ("agent_runtime.step: length out of range\n", b"", "length out of range"),
        ("agent_runtime.step: short body\n", b"", "short body"),
        ("agent_runtime.step: invalid JSON\n", b"", "invalid JSON"),
        (
            "agent_runtime.step: request is not an object with a task\n",
            b"",
            "request is not an object with a task",
        ),
        ("agent_runtime.step: bad boundary\n", b"", "bad boundary"),
        (
            "confinement warning\n",
            boundary_doc(3, summary="boundary mismatch"),
            "boundary mismatch",
        ),
        ("private stderr", b"private stdout", "unrecognized"),
        ("agent_runtime.step: bad boundary private suffix\n", b"", "unrecognized"),
    ],
)
def test_protocol_failure_reports_only_known_diagnostics(
    tmp_path, caplog, stderr, stdout, diagnostic
):
    # Losing stderr/summary hides framing vs restore failures; copying arbitrary output leaks
    # agent content. Exercise the Activity's actual failure and emitted audit record.
    d = setup(tmp_path)
    d.exec = lambda *args, **kwargs: act.ExecResult(5, stdout, stderr, False)
    err = refused(act.run_boundary, BoundaryIn(inp(), 1, 3))
    assert err.type == "OutcomeUnknown" and not err.non_retryable
    assert f"protocol_error={diagnostic}" in str(err)
    records = [
        json.loads(r.message)
        for r in caplog.records
        if r.name == "runner.records" and '"boundary_failed"' in r.message
    ]
    assert records == [
        {
            "event": "boundary_failed",
            "session_id": SESSION,
            "generation": 1,
            "boundary": 3,
            "rc": 5,
            "truncated": False,
            "timed_out": False,
            "protocol_error": diagnostic,
        }
    ]
    assert "private" not in str(err) + caplog.text


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
    run(act.create_sandbox, GenIn(inp(), 1, None))
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


def test_seed_restores_only_the_workspace(tmp_path):
    # T26: a session seeded from another session's snapshot gets that workspace and nothing of
    # the donor's state/, whose state.json would make boundary 0 a mismatch (step rc 5).
    blob = workspace_tar()
    stream = FakeStream(FakeWS([blob], rc=0), FakeWS([], rc=0))
    setup(tmp_path, stream=stream)
    run(act.create_sandbox, GenIn(inp(), 1, None))
    key = run(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
    seeded = dataclasses.replace(inp(), session_id="b" * 32, seed=key)
    run(act.create_sandbox, GenIn(seeded, 1, key))
    run(act.wait_ready, GenIn(seeded, 1, key))
    assert stream.commands[1] == act.SEED_RESTORE_CMD + [str(len(blob))]
    assert stream.commands[1][2].endswith("workspace")
    assert bytes(stream.used[1].stdin) == blob


def test_snapshot_over_limit_is_snapshot_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(act, "MAX_SNAPSHOT", 1 << 20)
    ws = FakeWS([b"\0" * 65536] * 32, rc=0)
    setup(tmp_path, stream=FakeStream(ws))
    run(act.create_sandbox, GenIn(inp(), 1, None))
    err = refused(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
    assert err.type == "SnapshotFailed" and err.non_retryable and ws.closed


def test_snapshot_nonzero_exit_is_retryable_snapshot_failed(tmp_path):
    setup(tmp_path, stream=FakeStream(FakeWS([b"partial"], rc=2)))
    run(act.create_sandbox, GenIn(inp(), 1, None))
    err = refused(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
    assert err.type == "SnapshotFailed" and not err.non_retryable


@pytest.mark.parametrize("loss", ["deleted", "replaced", "terminating", "exited"])
def test_snapshot_rejects_pod_loss_during_actual_store_copy(tmp_path, loss):
    # The archive is fully spooled before put starts. Killing the Pod while this
    # real filesystem copy is in progress must not make its result a checkpoint.
    blob = workspace_tar() + b"\0" * (2 << 20)
    d = setup(tmp_path, stream=FakeStream(FakeWS([blob], rc=0)))
    run(act.create_sandbox, GenIn(inp(), 1, None))
    name = act.pod_name(SESSION, 1)
    put = d.store.put
    copies = []

    def put_while_losing_pod(key, src):
        class Source:
            reads = 0

            def read(self, size):
                chunk = src.read(size)
                self.reads += 1
                if self.reads == 2:
                    pod = d.core.pods[name]
                    if loss == "deleted":
                        del d.core.pods[name]
                    elif loss == "replaced":
                        pod.metadata.uid = "replacement-pod"
                    elif loss == "terminating":
                        pod.metadata.deletion_timestamp = datetime.now(UTC)
                    else:
                        pod.status.phase = "Failed"
                return chunk

        put(key, Source())
        copies.append(key)

    d.store.put = put_while_losing_pod
    err = refused(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
    assert err.type == "SnapshotFailed"
    assert copies == [hashlib.sha256(blob).hexdigest()]


def test_snapshot_store_failure_has_retryable_unknown_checkpoint_outcome(tmp_path):
    d = setup(tmp_path, stream=FakeStream(FakeWS([workspace_tar()], rc=0)))
    run(act.create_sandbox, GenIn(inp(), 1, None))

    def fail_put(key, src):
        raise OSError("storage unavailable")

    d.store.put = fail_put
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


OTHER_SESSION = "00000000-0000-4000-8000-000000000000"
GRANT_DELETE = ("DELETE", f"/v1/grants/gh_1?session_id={SESSION}")
# An APS from before deletes were bound to a session, and one that binds them.
either_aps = pytest.mark.parametrize("binds_delete", [False, True])


def test_stop_and_grant_calls(tmp_path):
    d = setup(tmp_path)
    assert run(act.stop_session, inp("gh_1")) == "OK"
    assert run(act.delete_grant, inp("gh_1")) == "OK"
    assert d.aps.calls == [
        ("POST", f"/v1/sessions/{SESSION}/stop"),
        ("GET", "/v1/grants/gh_1"),
        GRANT_DELETE,
    ]


def test_delete_grant_without_a_handle_calls_nothing(tmp_path):
    d = setup(tmp_path)
    assert run(act.delete_grant, inp()) == "OK"
    assert d.aps.calls == []


@either_aps
def test_delete_grant_removes_the_sessions_own_grant(tmp_path, binds_delete):
    grants = {"gh_1": {"session_id": SESSION, "tenant": TENANT}}
    aps = FakeAPS(grants=grants, binds_delete=binds_delete)
    setup(tmp_path, custom=FakeCustom(DELEGATED), aps=aps)
    run(act.preflight, inp(grant="gh_1"))
    assert run(act.delete_grant, inp("gh_1")) == "OK"
    assert aps.grants == {} and aps.calls[-1] == GRANT_DELETE
    assert run(act.delete_grant, inp("gh_1")) == "OK"  # already gone, as on an activity retry


@either_aps
def test_delete_grant_after_stop_removed_the_tokens(tmp_path, binds_delete):
    """Stop and the grant delete run together; once stop wins, APS no longer shows the grant."""
    grants = {"gh_1": {"session_id": SESSION, "tenant": TENANT}}
    aps = FakeAPS(grants=grants, binds_delete=binds_delete)
    setup(tmp_path, aps=aps)
    assert run(act.stop_session, inp("gh_1")) == "OK"
    assert run(act.delete_grant, inp("gh_1")) == "OK"
    assert aps.grants == {} and aps.calls[-1] == GRANT_DELETE


@either_aps
@pytest.mark.parametrize("status", [DELEGATED, None], ids=["refused", "handle-never-read"])
def test_cleanup_leaves_the_grant_of_another_session(tmp_path, binds_delete, status):
    """A session submitted with another session's handle cannot revoke that delegation."""
    theirs = {"session_id": OTHER_SESSION, "tenant": TENANT}
    aps = FakeAPS(grants={"gh_1": dict(theirs)}, binds_delete=binds_delete)
    setup(tmp_path, custom=FakeCustom(status), aps=aps)
    if status:  # a delegated release reads the grant and refuses the session
        assert refused(act.preflight, inp(grant="gh_1")).message == (
            "delegated session without a grant"
        )
    else:  # any other release never reads the handle before cleanup
        run(act.preflight, inp(grant="gh_1"))
    assert run(act.stop_session, inp("gh_1")) == "OK"
    e = refused(act.delete_grant, inp("gh_1"))
    assert e.type == "Forbidden" and e.non_retryable
    assert aps.grants == {"gh_1": theirs}
    assert not [c for c in aps.calls if c[0] == "DELETE"]
    assert aps.get("/v1/grants/gh_1") == theirs  # still usable by its own session


def test_aps_refuses_a_grant_the_runner_cannot_see(tmp_path):
    """The other session's tokens are gone, so only APS can tell whose record this is."""
    theirs = {"session_id": OTHER_SESSION, "tenant": TENANT}
    aps = FakeAPS(grants={"gh_1": dict(theirs)})
    aps.stopped.add(OTHER_SESSION)
    setup(tmp_path, aps=aps)
    e = refused(act.delete_grant, inp("gh_1"))
    assert e.type == "Forbidden" and e.non_retryable
    assert aps.grants == {"gh_1": theirs} and aps.calls[-1] == GRANT_DELETE


def test_delete_grant_escapes_what_it_was_given(tmp_path):
    """Cleanup runs on input preflight refused, so neither value is trusted as a path."""
    theirs = {"session_id": OTHER_SESSION, "tenant": TENANT}
    aps = FakeAPS(grants={"gh_1": dict(theirs)})
    aps.stopped.add(OTHER_SESSION)
    setup(tmp_path, aps=aps)
    forged = dataclasses.replace(inp(f"gh_1?session_id={OTHER_SESSION}#"), session_id="a&b=c d")
    assert run(act.delete_grant, forged) == "OK"  # no grant has that handle
    assert aps.grants == {"gh_1": theirs}
    assert aps.calls[-1] == (
        "DELETE",
        f"/v1/grants/gh_1%3Fsession_id%3D{OTHER_SESSION}%23?session_id=a%26b%3Dc%20d",
    )


@pytest.mark.parametrize("replaced", [False, True])
def test_snapshot_retry_preserves_first_attempt_pod_uid(tmp_path, replaced):
    d = setup(tmp_path, stream=FakeStream(FakeWS([workspace_tar()], rc=0)))
    run(act.create_sandbox, GenIn(inp(), 1, None))
    pod = d.core.pods[act.pod_name(SESSION, 1)]
    original = pod.metadata.uid
    environment = ActivityEnvironment()
    environment.info = dataclasses.replace(
        environment.info, attempt=2, heartbeat_details=[{"snapshot_pod_uid": original}]
    )
    beats = []
    environment.on_heartbeat = lambda *args: beats.append(args)
    if replaced:
        pod.metadata.uid = "replacement-pod"
        with pytest.raises(ApplicationError) as error:
            environment.run(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
        assert error.value.type == "SnapshotFailed" and error.value.non_retryable
        assert beats == []
    else:
        key = environment.run(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
        assert key == hashlib.sha256(workspace_tar()).hexdigest()
        assert beats and all(b == ({"snapshot_pod_uid": original},) for b in beats)


@pytest.mark.parametrize(
    "details",
    [[{}], ["uid"], [{"snapshot_pod_uid": ""}], [{"snapshot_pod_uid": "uid", "other": 1}]],
)
def test_snapshot_retry_rejects_invalid_heartbeat_identity(tmp_path, details):
    setup(tmp_path)
    environment = ActivityEnvironment()
    environment.info = dataclasses.replace(environment.info, attempt=2, heartbeat_details=details)
    with pytest.raises(ApplicationError) as error:
        environment.run(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
    assert error.value.type == "SnapshotFailed" and error.value.non_retryable


def test_pod_loss_after_checkpoint_returns_does_not_invalidate_artifact(tmp_path):
    blob = workspace_tar()
    stream = FakeStream(FakeWS([blob], rc=0), FakeWS([], rc=0))
    d = setup(tmp_path, stream=stream)
    run(act.create_sandbox, GenIn(inp(), 1, None))
    key = run(act.snapshot_workspace, SnapshotIn(inp(), 1, 0))
    del d.core.pods[act.pod_name(SESSION, 1)]
    run(act.create_sandbox, GenIn(inp(), 2, key))
    run(act.wait_ready, GenIn(inp(), 2, key))
    assert bytes(stream.used[-1].stdin) == blob
    with d.store.open(key) as artifact:
        assert hashlib.sha256(artifact.read()).hexdigest() == key
