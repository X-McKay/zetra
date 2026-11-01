import asyncio

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from runner.types import CoreSessionResult
from runner.workflow import EXECUTION_TIMEOUT, SessionWorkflow
from tests.harness import _environment, session_input
from tests.test_core_workflow import CoreActivities


@pytest.mark.parametrize(
    "mode", ["completed", "replacement", "unknown", "snapshot_failed", "wrong_workflow"]
)
async def test_full_core_history_and_replay(mode):
    def prepare(generation, cursor):
        if mode == "replacement" and generation == 1 and cursor == 1:
            raise ApplicationError(
                "synthetic lost prepare", type="CorePrepareLost", non_retryable=True
            )

    def execute(generation, cursor):
        if mode == "unknown":
            raise ApplicationError(
                "synthetic lost completion", type="SyntheticLoss", non_retryable=True
            )

    def checkpoint(generation, cursor, count):
        if mode == "snapshot_failed":
            raise ApplicationError("synthetic store loss", type="CoreSnapshotFailed")

    fake = CoreActivities(prepare_plan=prepare, execute_plan=execute, checkpoint_plan=checkpoint)
    inp = session_input()
    env = await _environment()
    async with env:
        async with Worker(
            env.client,
            task_queue=inp.task_queue,
            workflows=[SessionWorkflow],
            activities=fake.all(),
        ):
            handle = await env.client.start_workflow(
                SessionWorkflow.run,
                inp,
                id=inp.workflow_id + ("-wrong" if mode == "wrong_workflow" else ""),
                task_queue=inp.task_queue,
                execution_timeout=EXECUTION_TIMEOUT,
            )
            typed = env.client.get_workflow_handle(handle.id, result_type=CoreSessionResult)
            result = await asyncio.wait_for(typed.result(), 30)
            history = await handle.fetch_history()
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
            expected = {
                "unknown": "OUTCOME_UNKNOWN",
                "snapshot_failed": "OUTCOME_UNKNOWN",
                "wrong_workflow": "REJECTED",
            }.get(mode, "COMPLETED")
            assert result.outcome == expected
            assert result.runtime_profile == "zetra-core-v1"
            if mode == "wrong_workflow":
                assert fake.log == [("preflight",)]
                assert result.generation == 0
            elif mode in {"unknown", "snapshot_failed"}:
                assert fake.core_executes == [(1, 0)]
                assert result.boundaries == 0
                assert result.position["cursor"] == 0
            else:
                assert result.boundaries == 3 and result.position["cursor"] == 3
                assert result.final_text == "Finished."
                assert len(fake.core_executes) == 3
                assert result.generation == (2 if mode == "replacement" else 1)
            assert not fake.executed
            assert all(event[0] != "create_sandbox" for event in fake.log)


async def test_native_core_cancellation_closes_with_cleanup_and_replays():
    from temporalio.client import WorkflowFailureError

    fake = CoreActivities(block_at=0)
    inp = session_input()
    env = await _environment()
    async with env:
        async with Worker(
            env.client,
            task_queue=inp.task_queue,
            workflows=[SessionWorkflow],
            activities=fake.all(),
        ):
            handle = await env.client.start_workflow(
                SessionWorkflow.run,
                inp,
                id=inp.workflow_id,
                task_queue=inp.task_queue,
                execution_timeout=EXECUTION_TIMEOUT,
            )
            await asyncio.wait_for(fake.blocked.wait(), 10)
            await handle.cancel()
            with pytest.raises(WorkflowFailureError):
                await handle.result()
            status = await handle.query(SessionWorkflow.status)
            assert status["cleanup"]["delete"] == "DELETED"
            assert status["boundaries"] == 0 and fake.core_executes == [(1, 0)]
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(
                await handle.fetch_history()
            )
