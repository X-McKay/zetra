import dataclasses

import pytest
from temporalio.testing import ActivityEnvironment

from runner import activities
from runner.conformance_lifecycle_control import Control
from runner.conformance_lifecycle_worker import LifecycleActivities
from runner.types import BoundaryIn, GenIn, SessionInput
from tests.test_conformance_lifecycle_control import Core


def setup(case="success"):
    c = Control(Core())
    cfg = c.config
    inp = SessionInput(
        cfg["tenant"],
        cfg["agent"],
        cfg["release"],
        cfg["sessions"][case],
        "opaque-grant",
        {"lifecycle_case": case},
        max_boundaries=2,
    )
    return c, inp, LifecycleActivities(c, "exact-worker-uid")


def test_release_invokes_real_boundary_once_and_preserves_result(monkeypatch):
    control, inp, wrapped = setup()
    control.released = lambda *args: True
    called = []
    sentinel = object()
    monkeypatch.setattr(activities, "run_boundary", lambda arg: called.append(arg) or sentinel)
    arg = BoundaryIn(inp, 1, 1)
    assert ActivityEnvironment().run(wrapped.run_boundary, arg) is sentinel
    assert called == [arg]


def test_gate_reports_safe_session_binding_before_real_call(monkeypatch):
    control, inp, wrapped = setup()
    released = [False, True]
    control.released = lambda *args: released.pop(0)
    monkeypatch.setattr(activities, "run_boundary", lambda arg: "real-return")
    env = ActivityEnvironment()
    heartbeats = []
    env.on_heartbeat = lambda *args: heartbeats.append(args[0])
    assert env.run(wrapped.run_boundary, BoundaryIn(inp, 1, 1)) == "real-return"
    assert heartbeats[0] == {
        "schema": "t24-gate-v1",
        "case": "success",
        "session_id": inp.session_id,
        "stage": "boundary1",
        "generation": 1,
        "worker_uid": "exact-worker-uid",
    }
    assert "opaque-grant" not in str(heartbeats)


def test_cleanup_retry_after_crash_runs_real_delete_without_a_new_gate(monkeypatch):
    control, inp, wrapped = setup("worker_kill")
    called = []
    monkeypatch.setattr(activities, "delete_sandbox", lambda arg: called.append(arg) or "DELETED")
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=2)
    arg = GenIn(inp, 1, None)
    assert env.run(wrapped.delete_sandbox, arg) == "DELETED"
    assert called == [arg]


@pytest.mark.parametrize(
    "field,value",
    [("agent", "other"), ("release", "other"), ("session_id", "other"), ("grant_handle", None)],
)
def test_wrapper_never_executes_unbound_session(monkeypatch, field, value):
    _, inp, wrapped = setup()
    called = []
    monkeypatch.setattr(activities, "preflight", lambda arg: called.append(arg))
    setattr(inp, field, value)
    with pytest.raises(ValueError):
        ActivityEnvironment().run(wrapped.preflight, inp)
    assert called == []
