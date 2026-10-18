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


@pytest.mark.parametrize(
    "completed,attempt,settled", [(True, 1, True), (False, 1, False), (True, 2, False)]
)
async def test_unacknowledged_provision_or_retry_cannot_prove_cleanup(
    handle, completed, attempt, settled
):
    events = [
        HistoryEvent(
            event_id=5,
            activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
                activity_type=ActivityType(name="provision_session")
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
