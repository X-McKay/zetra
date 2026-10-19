import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio.api.common.v1 import ActivityType
from temporalio.api.history.v1 import (
    ActivityTaskCompletedEventAttributes,
    ActivityTaskScheduledEventAttributes,
    ActivityTaskStartedEventAttributes,
    HistoryEvent,
)

from zetra.session import SessionHandle
from zetra.worker import ActivityRouter, Context, execution_key, settled_provisioning


@pytest.fixture
def handle():
    return SessionHandle(namespace="default", workflow_id="session", run_id="run", runtime_id="lab")


def router():
    return ActivityRouter(
        None,
        task_queue="queue",
        runtime_id="lab",
        sandbox_client=None,
        artifacts=None,
        key=b"x" * 32,
    )


def test_capability_is_bound_to_execution_release_and_worker_key(handle):
    key = execution_key(b"x" * 32, handle, "release")
    assert key == execution_key(b"x" * 32, handle, "release")
    assert key != execution_key(b"y" * 32, handle, "release")
    assert key != execution_key(b"x" * 32, handle, "other")
    assert key != execution_key(b"x" * 32, handle.model_copy(update={"run_id": "next"}), "release")


async def test_concurrent_cache_misses_construct_one_adapter(handle):
    worker = router()
    context = Context({}, SimpleNamespace(lifecycle=SimpleNamespace(creations=set())))
    worker.build = AsyncMock(return_value=context)
    entered = []

    async def use():
        async with worker.context(handle) as actual:
            entered.append(actual)
            await asyncio.sleep(0)

    await asyncio.gather(*[use() for _ in range(10)])
    assert entered == [context] * 10
    assert worker.build.await_count == 1
    assert context.users == 0


async def test_cancelled_cache_initialization_retains_no_task(handle):
    worker = router()
    entered = asyncio.Event()

    async def build(_):
        entered.set()
        await asyncio.Event().wait()

    worker.build = build

    async def use():
        async with worker.context(handle):
            pytest.fail("canceled initializer entered context")

    task = asyncio.create_task(use())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not worker.cache and not worker.cache_lock.locked()


@pytest.mark.parametrize("name", ["provision_session", "rebind_session", "create_session"])
@pytest.mark.parametrize(
    "completed,attempt,settled", [(True, 1, True), (False, 1, False), (True, 2, False)]
)
async def test_unacknowledged_provision_or_retry_cannot_prove_cleanup(
    handle, name, completed, attempt, settled
):
    events = [
        HistoryEvent(
            event_id=5,
            activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
                activity_type=ActivityType(name=name)
            ),
        ),
        HistoryEvent(
            event_id=6,
            activity_task_started_event_attributes=ActivityTaskStartedEventAttributes(
                scheduled_event_id=5, attempt=attempt
            ),
        ),
    ]
    if completed:
        events.append(
            HistoryEvent(
                event_id=7,
                activity_task_completed_event_attributes=ActivityTaskCompletedEventAttributes(
                    scheduled_event_id=5
                ),
            )
        )
    temporal = Mock()
    temporal.get_workflow_handle.return_value.fetch_history = AsyncMock(
        return_value=SimpleNamespace(events=events)
    )
    assert await settled_provisioning(temporal, handle) is settled
    temporal.get_workflow_handle.assert_called_once_with(handle.workflow_id, run_id=handle.run_id)


@pytest.fixture
def release():
    from zetra.manifest import AgentSpec, Release

    return Release(
        agent=AgentSpec(name="test", owner="test", entrypoint="test.agent:create"),
        image="test@sha256:" + "1" * 64,
        source_sha256="2" * 64,
        policy_sha256="3" * 64,
        lock_sha256="4" * 64,
        sdk_revision="5" * 40,
    )


def approval_router(approved_releases):
    return ActivityRouter(
        None,
        task_queue="queue",
        runtime_id="lab",
        sandbox_client=None,
        artifacts=None,
        key=b"x" * 32,
        approved_releases=approved_releases,
    )


@pytest.mark.parametrize("approved", [[], ["different-release"]])
@pytest.mark.parametrize(
    "name",
    [
        "provision_session",
        "rebind_session",
        "create_session",
        "wait_session_ready",
        "advance_runner",
        "execute_model",
        "execute_tool",
    ],
)
async def test_unapproved_release_rejected_before_sandbox_access(handle, release, approved, name):
    from temporalio.exceptions import ApplicationError

    worker = approval_router(approved)
    start = {"release": release.model_dump()}
    activities = SimpleNamespace(lifecycle=SimpleNamespace(creations=set()))
    operation = AsyncMock(side_effect=AssertionError("sandbox was accessed"))
    setattr(activities, name, operation)
    worker.build = AsyncMock(return_value=Context(start, activities))
    worker.current_handle = lambda: handle
    command = start if name == "provision_session" else {"start": start}
    with pytest.raises(ApplicationError, match="release is not approved") as exc:
        await worker.invoke(name, command)
    assert exc.value.non_retryable
    operation.assert_not_awaited()


@pytest.mark.parametrize("approval", [None, "matching"])
async def test_approved_release_can_execute_from_cached_context(handle, release, approval):
    worker = approval_router(None if approval is None else [release.identity])
    operation = AsyncMock(return_value={"accepted": True})
    worker.build = AsyncMock(
        return_value=Context(
            {"release": release.model_dump()},
            SimpleNamespace(lifecycle=SimpleNamespace(creations=set()), execute_tool=operation),
        )
    )
    worker.current_handle = lambda: handle
    assert await worker.execute_tool({}) == {"accepted": True}
    worker.approved_releases = frozenset()
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError, match="release is not approved"):
        await worker.execute_tool({})
    assert operation.await_count == 1
    assert worker.build.await_count == 1


async def test_revoked_release_still_allows_cleanup(handle, release, monkeypatch):
    worker = approval_router([])
    start = {"release": release.model_dump()}
    operation = AsyncMock(return_value={"cleanup": "DELETED"})
    worker.build = AsyncMock(
        return_value=Context(
            start,
            SimpleNamespace(
                lifecycle=SimpleNamespace(creations=set()),
                close_session=operation,
            ),
        )
    )
    worker.current_handle = lambda: handle
    monkeypatch.setattr("zetra.worker.settled_provisioning", AsyncMock(return_value=True))
    assert await worker.close_session({"start": start}) == {"cleanup": "DELETED"}
    worker.temporal = Mock()
    worker.temporal.get_workflow_handle.return_value.describe = AsyncMock(
        return_value=SimpleNamespace(status=SimpleNamespace(name="COMPLETED"))
    )
    assert await worker.reconcile_session(handle.model_dump()) == {"cleanup": "DELETED"}
    assert operation.await_count == 2


@pytest.mark.parametrize("invalid", ["release", b"release", 123, [None], [1], [""]])
def test_invalid_release_approvals_fail_at_construction(invalid):
    with pytest.raises(ValueError, match="approved_releases"):
        approval_router(invalid)


def test_release_approvals_are_snapshotted():
    approvals = ["release"]
    worker = approval_router(approvals)
    approvals.clear()
    assert worker.approved_releases == frozenset({"release"})


def test_create_worker_consumes_release_approval_and_keeps_native_options(monkeypatch):
    from zetra.worker import create_worker

    native = Mock()
    monkeypatch.setattr("zetra.worker.Worker", native)
    worker = create_worker(
        None,
        task_queue="queue",
        runtime_id="lab",
        sandbox_client=None,
        artifacts=None,
        key=b"x" * 32,
        approved_releases=["approved"],
        max_concurrent_activities=3,
    )
    assert worker is native.return_value
    options = native.call_args.kwargs
    assert options["max_concurrent_activities"] == 3
    assert "approved_releases" not in options
    assert options["activities"][0].__self__.approved_releases == frozenset({"approved"})


async def test_readiness_retry_does_not_reopen_create_admission(handle):
    events = [
        HistoryEvent(
            event_id=5,
            activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
                activity_type=ActivityType(name="create_session")
            ),
        ),
        HistoryEvent(
            event_id=6,
            activity_task_started_event_attributes=ActivityTaskStartedEventAttributes(
                scheduled_event_id=5, attempt=1
            ),
        ),
        HistoryEvent(
            event_id=7,
            activity_task_completed_event_attributes=ActivityTaskCompletedEventAttributes(
                scheduled_event_id=5
            ),
        ),
        HistoryEvent(
            event_id=10,
            activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
                activity_type=ActivityType(name="wait_session_ready")
            ),
        ),
        HistoryEvent(
            event_id=11,
            activity_task_started_event_attributes=ActivityTaskStartedEventAttributes(
                scheduled_event_id=10, attempt=3
            ),
        ),
    ]
    temporal = Mock()
    temporal.get_workflow_handle.return_value.fetch_history = AsyncMock(
        return_value=SimpleNamespace(events=events)
    )
    assert await settled_provisioning(temporal, handle)
