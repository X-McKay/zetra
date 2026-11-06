"""Future Schedule actions move by raw protobuf CAS without decoding payloads."""

from unittest.mock import AsyncMock

import pytest
from temporalio.api.workflowservice import v1 as api

from runner.worker_schedule import CleanupScheduleOperator
from runner.worker_versioning import GuardError


def description():
    return api.DescribeScheduleResponse(
        conflict_token=b"\x00\x00\x00\x00\x00\x00\x00\x01",
        schedule={
            "spec": {"interval": [{"interval": {"seconds": 300}}]},
            "policies": {"overlap_policy": 1},
            "state": {"notes": "retained", "paused": True},
            "action": {
                "start_workflow": {
                    "workflow_id": "cleanup-reconciler:tenant-test",
                    "workflow_type": {"name": "CleanupReconcilerWorkflow"},
                    "task_queue": {"name": "cleanup-tenant-test"},
                    "input": {
                        "payloads": [
                            {
                                "metadata": {"encoding": b"binary/encrypted"},
                                "data": b"opaque-ciphertext",
                            }
                        ]
                    },
                    "workflow_execution_timeout": {"seconds": 900},
                }
            },
        },
        memo={"fields": {"opaque": {"data": b"preserved"}}},
    )


class Service:
    def __init__(self):
        self.value = description()
        self.updates = []

    async def describe_schedule(self, request, **kwargs):
        assert request.schedule_id == "cleanup-reconciler-tenant-test"
        assert request.namespace == "agents-tenant-test"
        return self.value

    async def update_schedule(self, request, **kwargs):
        assert request.conflict_token == self.value.conflict_token
        self.updates.append(request)
        self.value.schedule.CopyFrom(request.schedule)
        self.value.conflict_token = b"\x00\x00\x00\x00\x00\x00\x00\x02"
        return api.UpdateScheduleResponse()


class Versions:
    tenant = "tenant-test"

    def record(self, value):
        pass

    prepare = AsyncMock(return_value={"current": "new"})
    legacy = AsyncMock(return_value={"workers": {"pods": [{"name": "legacy"}]}})


@pytest.mark.asyncio
async def test_schedule_cas_preserves_every_field_except_future_queue():
    service = Service()
    before = description()
    operator = CleanupScheduleOperator(service, Versions())
    plan = await operator.inspect()
    result = await operator.route("governed-v1", build_id="new", image="pinned", **plan)
    assert result["status"] == "confirmed"
    request = service.updates[0]
    assert request.memo == before.memo
    changed = api.DescribeScheduleResponse()
    changed.CopyFrom(before)
    changed.schedule.action.start_workflow.task_queue.name = "cleanup-tenant-test-governed-v1"
    assert request.schedule == changed.schedule
    assert b"opaque-ciphertext" not in str(result).encode()
    assert request.conflict_token == b"\x00\x00\x00\x00\x00\x00\x00\x01"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["token", "queue", "interval", "policy", "payload"])
async def test_schedule_race_or_wrong_shape_never_mutates(changed):
    service = Service()
    operator = CleanupScheduleOperator(service, Versions())
    plan = await operator.inspect()
    if changed == "token":
        service.value.conflict_token = b"raced"
    elif changed == "queue":
        service.value.schedule.action.start_workflow.task_queue.name = "foreign"
    elif changed == "interval":
        service.value.schedule.spec.interval[0].interval.seconds = 60
    elif changed == "policy":
        service.value.schedule.policies.overlap_policy = 3
    else:
        service.value.schedule.action.start_workflow.input.payloads[0].data = b"changed"
    with pytest.raises(GuardError):
        await operator.route("governed-v1", build_id="new", image="pinned", **plan)
    assert not service.updates


@pytest.mark.asyncio
async def test_schedule_write_timeout_is_uncertain_never_retried():
    service = Service()
    original = service.update_schedule

    async def timeout(request, **kwargs):
        await original(request, **kwargs)
        raise TimeoutError("must not leak")

    service.update_schedule = timeout
    operator = CleanupScheduleOperator(service, Versions())
    result = await operator.route(
        "governed-v1", build_id="new", image="pinned", **await operator.inspect()
    )
    assert result["status"] == "uncertain"
    assert result["observed_queue"] == "cleanup-tenant-test-governed-v1"
    assert len(service.updates) == 1
    assert "must not leak" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [b"", b"short", bytes(8), b"longer-than-eight"])
async def test_schedule_token_cannot_disable_server_conflict_check(token):
    service = Service()
    service.value.conflict_token = token
    operator = CleanupScheduleOperator(service, Versions())
    with pytest.raises(GuardError, match="token"):
        await operator.inspect()
    assert not service.updates


@pytest.mark.asyncio
async def test_acknowledged_but_ignored_schedule_update_is_not_confirmed():
    service = Service()

    async def ignored(request, **kwargs):
        service.updates.append(request)
        return api.UpdateScheduleResponse()

    service.update_schedule = ignored
    operator = CleanupScheduleOperator(service, Versions())
    result = await operator.route(
        "governed-v1", build_id="new", image="pinned", **await operator.inspect()
    )
    assert result["status"] == "uncertain"
    assert result["observed_queue"] == "cleanup-tenant-test"
    assert len(service.updates) == 1
