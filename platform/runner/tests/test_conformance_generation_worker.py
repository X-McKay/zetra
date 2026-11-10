import copy
import threading
from types import SimpleNamespace as NS

import pytest
from kubernetes.client.exceptions import ApiException

from runner import activities as act
from runner.conformance_generation_control import FINALIZER
from runner.conformance_generation_worker import GenerationActivities, marker
from runner.types import BoundaryIn, SessionInput
from tests.test_conformance_generation import config

WORKER = "00000000-0000-4000-8000-000000000001"


def fixture(case):
    cfg = config()
    sid = cfg["sessions"][case]
    inp = SessionInput(
        "tenant-test",
        "conformance-b" if case == "fence_only" else "conformance-checkpoint",
        cfg["probe_release"] if case == "fence_only" else cfg["recovery_release"],
        sid,
        None,
        {"tool": "probe_revocation", "args": {"marker": marker(sid), "duration": "180"}}
        if case == "fence_only"
        else {"checkpoint_case": "a", "run_id": cfg["run_id"]},
        max_boundaries=8 if case == "fence_only" else 3,
    )
    pod = NS(
        metadata=NS(
            uid="old-uid",
            name=act.pod_name(sid, 1),
            deletion_timestamp=None,
            finalizers=[FINALIZER] if case == "blocked_deletion" else [],
            labels={
                act.LABEL + k: v
                for k, v in {
                    "sandbox": "true",
                    "tenant": "tenant-test",
                    "agent": inp.agent,
                    "release": inp.release,
                    "session": sid,
                    "generation": "1",
                }.items()
            },
        ),
        status=NS(phase="Running", container_statuses=[]),
    )
    calls = []
    core = NS(
        read_namespaced_pod=lambda *a, **kw: pod,
        delete_namespaced_pod=lambda *a, **kw: calls.append((a, kw)),
    )
    control = NS(config=cfg, core=core, read=lambda: None, commanded=lambda *a: True)
    deps = NS(
        core=core,
        aps=NS(
            put=lambda path, body: calls.append((path, body))
            or {"session_id": sid, "generation": 2}
        ),
    )
    return GenerationActivities(deps, control, WORKER), BoundaryIn(inp, 1, 1), pod, calls


def test_fence_advances_only_aps_with_real_response_and_never_deletes_or_creates_pod(caplog):
    wrapped, arg, pod, calls = fixture("fence_only")
    with caplog.at_level("INFO"):
        wrapped.fence_monitor(arg, threading.Event())
    assert calls == [
        (
            "/v1/sessions/" + arg.inp.session_id + "/generation",
            {"generation": 2, "tenant": "tenant-test", "agent": "conformance-b"},
        )
    ]
    assert "fence_generation_2" in caplog.text


@pytest.mark.parametrize("case", ["blocked_deletion", "recovery_control"])
def test_real_uid_delete_gate_requires_actual_terminal_hold_or_404(monkeypatch, case, caplog):
    wrapped, arg, pod, calls = fixture(case)
    reads = 0

    def read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads <= 2:
            return pod
        if case == "recovery_control":
            raise ApiException(status=404)
        held = copy.deepcopy(pod)
        held.metadata.deletion_timestamp = "now"
        held.status.container_statuses = [NS(state=NS(terminated=NS(exit_code=137)))]
        return held

    wrapped.control.core.read_namespaced_pod = read
    monkeypatch.setattr(act.activity, "heartbeat", lambda *args: None)
    with caplog.at_level("INFO"):
        wrapped.delete_for_recovery(case, arg)
    assert calls[0][1]["body"].preconditions.uid == "old-uid"
    assert len(calls) == 1
    assert ("terminated_and_held" if case == "blocked_deletion" else "404") in caplog.text


@pytest.mark.parametrize("fault", ["wrong_agent", "wrong_session", "grant", "seed", "wrong_task"])
def test_worker_refuses_inputs_outside_exact_run(fault):
    wrapped, arg, pod, calls = fixture("fence_only")
    assert wrapped.case(arg.inp) == "fence_only"
    if fault == "wrong_agent":
        arg.inp.agent = "other"
    elif fault == "wrong_session":
        arg.inp.session_id = "00000000-0000-4000-8000-000000000099"
    elif fault == "grant":
        arg.inp.grant_handle = "gh_unexpected"
    elif fault == "seed":
        arg.inp.seed = "a" * 64
    else:
        arg.inp.task = {"tool": "other"}
    with pytest.raises(ValueError):
        wrapped.case(arg.inp)
    assert calls == []


def test_finalizer_must_hold_real_pod_not_a_simulated_failure(monkeypatch):
    wrapped, arg, pod, calls = fixture("blocked_deletion")
    reads = 0

    def read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads > 2:
            raise ApiException(status=404)
        return pod

    wrapped.control.core.read_namespaced_pod = read
    monkeypatch.setattr(act.activity, "heartbeat", lambda *args: None)
    with pytest.raises(ValueError, match="did not hold"):
        wrapped.delete_for_recovery("blocked_deletion", arg)


def test_preflight_hands_back_the_agents_boundary_limit(monkeypatch):
    wrapped, arg, _, _ = fixture("fence_only")
    monkeypatch.setattr(act, "preflight", lambda inp: 3)
    assert wrapped.preflight(arg.inp) == 3
