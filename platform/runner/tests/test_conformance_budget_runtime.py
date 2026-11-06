import asyncio
import hashlib
import io
import json
import tarfile

import pytest
from temporalio.api.common.v1 import ActivityType, Payloads
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import (
    ActivityTaskCompletedEventAttributes,
    ActivityTaskScheduledEventAttributes,
    HistoryEvent,
)
from temporalio.converter import DataConverter

from runner.conformance import parse_args
from runner.conformance_budget import read_budget_state
from runner.conformance_budget_runtime import KubernetesGatewayWatch, recovery_from_history


def snapshot(root, usage, next_boundary):
    raw = json.dumps(
        {"usage": usage, "next": next_boundary, "budget": {"schema": 1, "case": "session"}}
    ).encode()
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        entry = tarfile.TarInfo("state/state.json")
        entry.size = len(raw)
        archive.addfile(entry, io.BytesIO(raw))
    digest = hashlib.sha256(data.getvalue()).hexdigest()
    folder = root / digest[:2]
    folder.mkdir(exist_ok=True)
    (folder / digest).write_bytes(data.getvalue())
    return digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", [None, "unconfirmed_delete", "create_before_fence", "wrong_checkpoint", "not_zero"]
)
async def test_trusted_history_proves_zero_checkpoint_and_delete_fence_create(tmp_path, mutation):
    before = snapshot(tmp_path, 800, 4)
    zero = snapshot(tmp_path, 800 if mutation == "not_zero" else 0, 5)
    converter = DataConverter.default
    events = []

    async def activity(name, generation, boundary=None, result=None, restore=None):
        start = len(events) + 1
        args = {"generation": generation, "boundary": boundary, "restore": restore}
        events.append(
            HistoryEvent(
                event_id=start,
                event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
                activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
                    activity_type=ActivityType(name=name),
                    input=Payloads(payloads=await converter.encode([args])),
                ),
            )
        )
        events.append(
            HistoryEvent(
                event_id=start + 1,
                event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED,
                activity_task_completed_event_attributes=ActivityTaskCompletedEventAttributes(
                    scheduled_event_id=start,
                    result=Payloads(payloads=await converter.encode([result])),
                ),
            )
        )

    await activity("snapshot_workspace", 1, 3, before)
    await activity("snapshot_workspace", 1, 4, zero)
    await activity(
        "delete_sandbox",
        1,
        result="NOT_CONFIRMED" if mutation == "unconfirmed_delete" else "DELETED",
    )
    if mutation != "create_before_fence":
        await activity("set_generation", 2)
    await activity(
        "create_sandbox", 2, result=None, restore=before if mutation == "wrong_checkpoint" else zero
    )
    if mutation == "create_before_fence":
        await activity("set_generation", 2)
    evidence = await recovery_from_history(events, converter, tmp_path)
    assert all(
        evidence[key]
        for key in ("confirmed_delete_before_fence_create", "restored_committed_zero_checkpoint")
    ) is (mutation is None)


def test_snapshot_digest_and_single_regular_state_member(tmp_path):
    digest = snapshot(tmp_path, 0, 5)
    assert read_budget_state(tmp_path, digest)["usage"] == 0
    (tmp_path / digest[:2] / digest).write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest"):
        read_budget_state(tmp_path, digest)


def test_budget_cli_requires_explicit_releases():
    with pytest.raises(SystemExit):
        parse_args(["--release", "main", "--tests", "T13"])
    assert parse_args(
        ["--release", "main", "--tests", "t13b", "--budget-session-release", "budget-release"]
    ).tests == ["T13b"]


@pytest.mark.asyncio
async def test_gateway_watch_requires_all_old_pods_gone_and_new_ready(monkeypatch):
    watcher = KubernetesGatewayWatch(None)
    before = {
        "pods": [{"uid": "old1", "ready": True}, {"uid": "old2", "ready": True}],
        "configuration": "a" * 64,
    }
    values = iter(
        [
            {
                "pods": [{"uid": "old1", "ready": True}, {"uid": "new1", "ready": True}],
                "configuration": "a" * 64,
            },
            {
                "pods": [{"uid": "new1", "ready": True}, {"uid": "new2", "ready": False}],
                "configuration": "a" * 64,
            },
            {
                "pods": [{"uid": "new1", "ready": True}, {"uid": "new2", "ready": True}],
                "configuration": "a" * 64,
            },
        ]
    )
    monkeypatch.setattr(watcher, "inventory", lambda: next(values))

    async def nop(_):
        pass

    monkeypatch.setattr("runner.conformance_budget_runtime.asyncio.sleep", nop)
    got = await watcher.replaced(
        before, agent="conformance-budget-day", session_id="00000000-0000-4000-8000-000000000001"
    )
    assert {p["uid"] for p in got["after"]} == {"new1", "new2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("first_error", [RuntimeError("failed cleanup"), asyncio.CancelledError()])
async def test_cleanup_attempts_both_sessions_before_propagating_first_error(first_error):
    from runner.conformance_budget_runtime import BudgetHarness

    harness = object.__new__(BudgetHarness)
    closed = []

    async def close(item):
        closed.append(item)
        if item == "control":
            raise first_error

    harness.close = close
    with pytest.raises(BaseExceptionGroup):
        await harness.close_all(["control", "primary"])
    assert closed == ["control", "primary"]


def test_daily_retry_cli_is_explicit_and_fixed():
    args = ["--release", "main", "--tests", "T13", "--budget-day-release", "retry-release"]
    assert parse_args(args).budget_day_agent == "conformance-budget-day"
    assert (
        parse_args(args + ["--budget-day-agent", "conformance-budget-day-retry"]).budget_day_agent
        == "conformance-budget-day-retry"
    )
    with pytest.raises(SystemExit):
        parse_args(args + ["--budget-day-agent", "arbitrary"])
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--release",
                "main",
                "--tests",
                "T4",
                "--budget-day-agent",
                "conformance-budget-day-retry",
            ]
        )


@pytest.mark.asyncio
async def test_gateway_event_is_timestamped_and_late_ready_inventory_cannot_pass(
    monkeypatch, capsys
):
    from types import SimpleNamespace
    from runner import conformance_budget_runtime as runtime

    local = [0.0]
    monkeypatch.setattr(
        runtime,
        "time",
        SimpleNamespace(monotonic=lambda: local[0], time_ns=lambda: 1234567890123456789),
    )
    watcher = KubernetesGatewayWatch(None)
    before = {
        "pods": [{"uid": "old1", "ready": True}, {"uid": "old2", "ready": True}],
        "configuration": "a" * 64,
    }

    def inventory():
        local[0] = 101.0
        return {
            "pods": [{"uid": "new1", "ready": True}, {"uid": "new2", "ready": True}],
            "configuration": "a" * 64,
        }

    monkeypatch.setattr(watcher, "inventory", inventory)
    with pytest.raises(TimeoutError):
        await watcher.replaced(
            before,
            agent="conformance-budget-day-retry",
            session_id="00000000-0000-4000-8000-000000000001",
        )
    event = json.loads(capsys.readouterr().err)
    assert type(event["emitted_ns"]) is int and event["emitted_ns"] == 1234567890123456789
    assert event["test"] == "T13" and event["agent"] == "conformance-budget-day-retry"
    assert event["deadline_seconds"] == 100
    assert watcher.last_evidence["completed"] is False
    assert watcher.last_evidence["elapsed_seconds"] == 101.0
    assert watcher.last_evidence["started_at"]
    assert watcher.last_evidence["after"] == inventory()["pods"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_budget_timeout_retains_partial_ledger_phase_and_cleanup(monkeypatch, cleanup_error):
    from types import SimpleNamespace
    from runner.conformance_budget_runtime import BudgetHarness
    from runner.types import SessionInput

    retry = "conformance-budget-day-retry"
    inp = SessionInput(
        "tenant-test",
        retry,
        "signed-retry",
        "00000000-0000-4000-8000-000000000001",
        None,
        {"budget_case": "day"},
    )
    running = (inp, None, None, None)
    closed = []
    watcher = SimpleNamespace(inventory=lambda: {"pods": []}, last_evidence=None)
    harness = BudgetHarness(
        None,
        SimpleNamespace(tenant="tenant-test"),
        None,
        None,
        None,
        watcher,
        "signed-retry",
        None,
        "/unused",
        day_agent=retry,
    )

    async def validate(case):
        assert case == "day"

    async def start(case, role):
        harness.observations.append(
            {
                "session_id": inp.session_id,
                "agent": retry,
                "tenant": "tenant-test",
                "day": {"spent": 1800, "inflight": 0},
            }
        )
        return running

    async def wait(*args):
        return None

    async def replaced(before, **kwargs):
        watcher.last_evidence = {
            "completed": False,
            "emitted_ns": 123,
            "deadline_seconds": 100,
            "old_uids_absent": False,
        }
        raise TimeoutError("Bearer private-do-not-report")

    async def close(active):
        closed.extend(active)
        # Post-failure cleanup must not rewrite already collected evidence.
        harness.observations[0]["day"]["spent"] = 0
        watcher.last_evidence["completed"] = True
        if cleanup_error:
            raise ExceptionGroup("Bearer private-cleanup", [RuntimeError("private-cleanup")])

    monkeypatch.setattr(harness, "validate", validate)
    monkeypatch.setattr(harness, "start", start)
    monkeypatch.setattr(harness, "wait_decision", wait)
    monkeypatch.setattr(watcher, "replaced", replaced, raising=False)
    monkeypatch.setattr(harness, "close_all", close)
    report = await harness.run("day")
    assert report["result"] == "error" and report["error"] == "TimeoutError"
    assert report["phase"] == "gateway_replacement"
    assert report["evidence"]["observations"][0]["day"]["spent"] == 1800
    assert report["evidence"]["sessions"][0]["session_id"] == inp.session_id
    assert report["evidence"]["gateway_restart"]["completed"] is False
    assert closed == [running]
    assert "private" not in json.dumps(report)
    assert ("cleanup_error" in report) is cleanup_error
