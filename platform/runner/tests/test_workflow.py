from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio.exceptions import ApplicationError

from runner import workflow as wf
from tests.harness import (
    FakeActivities,
    Hang,
    out,
    run_inproc,
    run_temporal,
    session_input,
)


@pytest.fixture(params=["inproc", "temporal"])
def go(request, monkeypatch):
    async def runner(fake, inp, cancel=False, unsandboxed=False):
        if request.param == "inproc":
            return await run_inproc(monkeypatch, fake, inp, cancel=cancel)
        return await run_temporal(fake, inp, cancel=cancel, unsandboxed=unsandboxed)

    runner.backend = request.param
    return runner


def fail_once(error_type: str, at: tuple[int, int]):
    """A plan that raises error_type once, at (generation, boundary)."""

    def plan(generation, boundary):
        if (generation, boundary) == at:
            if error_type == "Timeout":
                raise Hang()
            raise ApplicationError(error_type, type=error_type, non_retryable=True)
        return None

    return plan


def assert_fenced_before_create(fake: FakeActivities) -> None:
    """set_generation(g) precedes create_sandbox(g), and g-1 is DELETED before g is fenced."""
    gens = [e[1] for e in fake.log if e[0] == "create_sandbox"]
    for g in gens:
        assert fake.log.index(("set_generation", g)) < fake.log.index(("create_sandbox", g))
        if g > 1:
            assert fake.log.index(("delete_sandbox", g - 1)) < fake.log.index(("set_generation", g))


async def test_t18_happy_path(go):
    fake = FakeActivities(done_at=2)
    r = await go(fake, session_input(grant="gh_test"))
    assert r.outcome == "COMPLETED"
    assert r.result.cleanup == {"stop": "OK", "delete": "DELETED", "grant": "OK"}
    assert 2 <= r.result.boundaries <= 6 and r.result.boundaries == 3
    assert r.result.generation == 1 and r.result.snapshot == "snap-g1-b2"
    assert r.status["state"] == "CLEANED"
    assert fake.names()[:4] == ["preflight", "set_generation", "create_sandbox", "wait_ready"]
    assert fake.restores == {1: None}
    assert_fenced_before_create(fake)


async def test_agent_mode_session_skips_grant_deletion(go):
    fake = FakeActivities(done_at=0)
    r = await go(fake, session_input(grant=None))
    assert r.result.cleanup == {"stop": "OK", "delete": "DELETED", "grant": "OK"}
    assert "delete_grant" not in fake.names()


@pytest.mark.parametrize("error_type", ["SandboxLost", "ExecFailed", "OutcomeUnknown", "Timeout"])
async def test_t23_uncommitted_exec_reruns_on_new_generation(go, error_type):
    fake = FakeActivities(done_at=3, boundary_plan=fail_once(error_type, (1, 2)))
    r = await go(fake, session_input())
    assert r.outcome == "COMPLETED" and r.result.generation == 2
    # Boundaries 0 and 1 were committed on generation 1 and never run again; boundary 2
    # ran once without commit and is re-run on generation 2, restored from boundary 1.
    assert fake.executed == [(1, 0), (1, 1), (1, 2), (2, 2), (2, 3)]
    assert fake.restores == {1: None, 2: "snap-g1-b1"}
    assert r.result.boundaries == 4
    assert_fenced_before_create(fake)


async def test_t23_snapshot_failure_reruns_the_boundary(go):
    def snap(generation, boundary):
        if (generation, boundary) == (1, 1):
            raise ApplicationError("upload failed", type="SnapshotFailed")

    fake = FakeActivities(done_at=2, snapshot_plan=snap)
    r = await go(fake, session_input())
    assert r.outcome == "COMPLETED"
    assert fake.executed == [(1, 0), (1, 1), (2, 1), (2, 2)]
    # The snapshot activity was retried three times before the boundary was abandoned.
    assert fake.log.count(("snapshot_workspace", 1, 1)) == 3
    assert fake.restores[2] == "snap-g1-b0"


async def test_t23_failure_before_any_commit_restores_nothing(go):
    fake = FakeActivities(done_at=1, boundary_plan=fail_once("SandboxLost", (1, 0)))
    r = await go(fake, session_input())
    assert r.outcome == "COMPLETED" and fake.restores == {1: None, 2: None}


async def test_t23_committed_boundaries_never_run_twice(go):
    def plan(generation, boundary):
        if boundary == 4 and generation < 3:
            raise ApplicationError("lost", type="ExecFailed", non_retryable=True)
        return None

    fake = FakeActivities(done_at=5, boundary_plan=plan)
    r = await go(fake, session_input())
    assert r.outcome == "COMPLETED" and r.result.generation == 3
    committed = [b for g, b in fake.executed if b != 4]
    assert committed == sorted(set(committed)) == [0, 1, 2, 3, 5]


async def test_max_three_generations(go):
    def plan(generation, boundary):
        raise ApplicationError("unknown", type="OutcomeUnknown", non_retryable=True)

    fake = FakeActivities(boundary_plan=plan)
    r = await go(fake, session_input())
    assert r.outcome == "FAILED" and r.result.generation == wf.MAX_GENERATIONS
    assert fake.executed == [(1, 0), (2, 0), (3, 0)]
    assert r.result.cleanup["delete"] == "DELETED"
    assert ("delete_sandbox", 3) in fake.log


async def test_t21_replacement_waits_for_confirmed_deletion(go):
    fake = FakeActivities(
        boundary_plan=fail_once("ExecFailed", (1, 1)), delete_plan=lambda g: "NOT_CONFIRMED"
    )
    r = await go(fake, session_input())
    assert r.outcome == "NOT_CONFIRMED"
    assert ("set_generation", 2) not in fake.log and ("create_sandbox", 2) not in fake.log
    assert r.result.generation == 1 and r.result.cleanup["delete"] == "NOT_CONFIRMED"
    assert r.status["state"] == "NOT_CONFIRMED"


async def test_agent_error_is_terminal_without_replacement(go):
    def plan(generation, boundary):
        if boundary == 1:
            raise ApplicationError("agent reported an error", type="AgentError", non_retryable=True)
        return None

    fake = FakeActivities(boundary_plan=plan)
    r = await go(fake, session_input())
    assert r.outcome == "FAILED" and r.result.generation == 1 and r.result.boundaries == 1
    assert r.result.cleanup == {"stop": "OK", "delete": "DELETED", "grant": "OK"}


async def test_t9a_preflight_refusal_creates_nothing(go):
    fake = FakeActivities(
        preflight_error=ApplicationError("release not ready", type="Forbidden", non_retryable=True)
    )
    r = await go(fake, session_input())
    assert r.outcome == "FAILED" and r.result.generation == 0
    assert "create_sandbox" not in fake.names() and "set_generation" not in fake.names()
    assert "delete_sandbox" not in fake.names()
    assert r.result.cleanup == {"stop": "OK", "delete": "DELETED", "grant": "OK"}


async def test_boundary_limit_fails(go):
    fake = FakeActivities(done_at=99)
    r = await go(fake, session_input(max_boundaries=2))
    assert r.outcome == "FAILED" and r.result.boundaries == 2


async def test_t24_every_cleanup_step_runs_when_others_fail(go):
    fake = FakeActivities(
        done_at=0,
        stop_error=ApplicationError("APS refused", type="Forbidden", non_retryable=True),
        grant_error=ApplicationError("APS refused", type="Forbidden", non_retryable=True),
        delete_plan=lambda g: "NOT_CONFIRMED",
    )
    r = await go(fake, session_input())
    assert r.outcome == "COMPLETED"
    assert r.result.cleanup == {
        "stop": "FAILED:Forbidden",
        "delete": "NOT_CONFIRMED",
        "grant": "FAILED:Forbidden",
    }
    assert {"stop_session", "delete_sandbox", "delete_grant"} <= set(fake.names())
    assert r.status["state"] == "NOT_CONFIRMED"


async def test_t24_delete_still_runs_when_stop_fails(go):
    fake = FakeActivities(
        done_at=0, stop_error=ApplicationError("APS refused", type="Forbidden", non_retryable=True)
    )
    r = await go(fake, session_input())
    assert r.result.cleanup == {"stop": "FAILED:Forbidden", "delete": "DELETED", "grant": "OK"}


async def test_cancellation_still_cleans_up(go):
    fake = FakeActivities(block_at=1)
    r = await go(fake, session_input(), cancel=True)
    assert r.outcome == "CANCELLED"
    assert r.status["cleanup"] == {"stop": "OK", "delete": "DELETED", "grant": "OK"}
    assert r.status["state"] == "CLEANED"
    assert ("delete_sandbox", 1) in fake.log


async def test_business_deadline_times_out_with_cleanup(go, monkeypatch):
    monkeypatch.setattr(wf, "BUSINESS_DEADLINE", timedelta(seconds=1))
    fake = FakeActivities(block_at=1)
    r = await go(fake, session_input(), unsandboxed=True)
    assert r.outcome == "TIMED_OUT"
    assert r.result.cleanup == {"stop": "OK", "delete": "DELETED", "grant": "OK"}
    assert r.result.boundaries == 1


def test_replaceable_classification():
    from temporalio.exceptions import ActivityError, CancelledError

    def err(cause):
        e = ActivityError(
            "x",
            scheduled_event_id=0,
            started_event_id=0,
            identity="",
            activity_type="run_boundary",
            activity_id="1",
            retry_state=None,
        )
        e.__cause__ = cause
        return e

    for t in ("SandboxLost", "ExecFailed", "OutcomeUnknown", "SnapshotFailed"):
        assert wf._replaceable(err(ApplicationError("x", type=t)))
    for t in ("AgentError", "BudgetExceeded", "Forbidden", "IdentityConflict"):
        assert not wf._replaceable(err(ApplicationError("x", type=t)))
    assert not wf._replaceable(err(CancelledError("cancelled")))


def test_out_helper_is_valid_protocol():
    from dataclasses import asdict

    from runner.types import BoundaryOut

    assert BoundaryOut.parse_strict(asdict(out(1))) == out(1)
