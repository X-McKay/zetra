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
    got = await watcher.replaced(before)
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
