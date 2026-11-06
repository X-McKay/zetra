from types import SimpleNamespace

import pytest

from runner.conformance_transport_worker import TransportActivities
from runner.types import SessionInput, BoundaryIn
from tests.test_transport_control import config


def inp(case="t4"):
    cfg = config()
    return SessionInput(
        "tenant-test",
        "conformance" if case == "t4" else "conformance-transport",
        cfg["t4_release" if case == "t4" else "transport_release"],
        cfg["sessions"][case],
        None,
        {"task": "normal"} if case == "t4" else {"transport_case": "tls"},
    )


def worker():
    return TransportActivities(
        SimpleNamespace(config=config(), read=lambda: None),
        None,
        "b2a651f5-fc2e-40a0-a8d0-bdd51aa4654d",
    )


def test_worker_accepts_only_two_bound_signed_identities():
    w = worker()
    assert w.case(inp()) == "t4" and w.case(inp("transport")) == "transport"
    original = inp()
    original.agent = "investigator"
    with pytest.raises(ValueError):
        w.case(original)
    original = inp()
    original.grant_handle = "opaque"
    with pytest.raises(ValueError):
        w.case(original)
    original = inp()
    original.seed = "a" * 64
    with pytest.raises(ValueError):
        w.case(original)


def test_first_t4_exec_is_gated_and_real_activity_remains_authoritative(monkeypatch):
    w = worker()
    calls = []
    monkeypatch.setattr(w, "gate", lambda b: calls.append("gate"))
    from runner import activities

    sentinel = SimpleNamespace(done=False)
    monkeypatch.setattr(activities, "run_boundary", lambda b: calls.append("real") or sentinel)
    arg = BoundaryIn(inp(), 1, 0)
    assert w.run_boundary(arg) is sentinel and calls == ["gate", "real"]
    calls.clear()
    arg.boundary = 1
    assert w.run_boundary(arg) is sentinel and calls == ["real"]
    arg.generation = 2
    with pytest.raises(ValueError):
        w.run_boundary(arg)


def test_final_real_t4_exec_holds_same_pod_until_capture_stops(monkeypatch):
    w = worker()
    calls = []
    from runner import activities

    result = SimpleNamespace(done=True)
    monkeypatch.setattr(activities, "run_boundary", lambda b: calls.append("real") or result)
    monkeypatch.setattr(
        w, "finish_capture", lambda b: calls.append("capture_stopped"), raising=False
    )
    assert w.run_boundary(BoundaryIn(inp(), 1, 2)) is result
    assert calls == ["real", "capture_stopped"]
