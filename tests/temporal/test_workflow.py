import asyncio
import os
import uuid
from collections import Counter
from datetime import timedelta

import pytest
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.worker import Replayer, Worker

from tests.fixtures.runtime import FixtureRuntime
from zetra._runtime.activities import Activities
from zetra._runtime.temporal import SessionWorkflow

pytestmark = pytest.mark.cluster


@pytest.fixture
async def client():
    endpoint = os.environ.get("TEMPORAL_ADDRESS")
    if not endpoint:
        pytest.skip("explicit TEMPORAL_ADDRESS required; no deployment claim from skips")
    return await Client.connect(endpoint, namespace="default")


@pytest.mark.parametrize("lost_completion", [False, True])
async def test_real_history_and_completion_loss(client, tmp_path, lost_completion):
    ident = "probe-" + uuid.uuid4().hex
    runtime = FixtureRuntime(tmp_path, ident)
    runtime.fail_after_candidate = lost_completion
    transport = await runtime.start()
    activities = Activities(runtime, transport, runtime.tape)
    try:
        async with Worker(
            client,
            task_queue=ident,
            workflows=[SessionWorkflow],
            activities=activities.registered(),
        ):
            handle = await client.start_workflow(
                SessionWorkflow.run,
                {"session": ident},
                id=ident,
                task_queue=ident,
                execution_timeout=timedelta(minutes=3),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            result = await handle.result()
        history = await handle.fetch_history()
        await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
        counts = Counter(
            e.activity_task_scheduled_event_attributes.activity_type.name
            for e in history.events
            if e.HasField("activity_task_scheduled_event_attributes")
        )
        assert result["cleanup"] == "DELETED"
        if lost_completion:
            assert result["status"] == "OUTCOME_UNKNOWN"
            assert result["position"]["cursor"] == 0
            assert len(runtime.candidates) == 1
            assert runtime.case.effects == ["fixture"]
            assert counts["execute_model"] == 1
        else:
            assert result["status"] == "SUCCEEDED"
            assert result["result"]["final_text"] == "Report ready."
            assert counts["advance_runner"] == 6
            assert counts["execute_model"] == 3 and counts["execute_tool"] == 2
            assert runtime.case.effects == [
                "fixture",
                "inspect_repository",
                "fixture",
                "write_report",
                "fixture",
            ]
    finally:
        await runtime.stop()


async def test_worker_restart_reuses_accepted_results(client, tmp_path):
    ident = "restart-" + uuid.uuid4().hex
    runtime = FixtureRuntime(tmp_path, ident)
    transport = await runtime.start()
    activities = Activities(runtime, transport, runtime.tape)
    try:
        async with Worker(
            client,
            task_queue=ident,
            workflows=[SessionWorkflow],
            activities=activities.registered(),
        ):
            handle = await client.start_workflow(
                SessionWorkflow.run,
                {"session": ident, "pause_at": 2},
                id=ident,
                task_queue=ident,
                execution_timeout=timedelta(minutes=3),
            )
            async with asyncio.timeout(60):
                while True:
                    progress = await handle.query(SessionWorkflow.progress)
                    if progress["position"] and progress["position"]["cursor"] == 2:
                        break
                    await asyncio.sleep(0.2)
        assert runtime.case.effects == ["fixture", "inspect_repository"]
        # A fresh worker replays from real server history; no process-local position is copied.
        async with Worker(
            client,
            task_queue=ident,
            workflows=[SessionWorkflow],
            activities=activities.registered(),
        ):
            await handle.signal(SessionWorkflow.resume)
            result = await handle.result()
        assert result["status"] == "SUCCEEDED"
        assert runtime.case.effects == [
            "fixture",
            "inspect_repository",
            "fixture",
            "write_report",
            "fixture",
        ]
        await Replayer(workflows=[SessionWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        await runtime.stop()
