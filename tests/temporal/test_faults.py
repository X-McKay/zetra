import asyncio
import json
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from tests.fixtures.runtime import FixtureRuntime
from zetra._runtime.activities import Activities
from zetra._runtime.temporal import SessionWorkflow

pytestmark = pytest.mark.cluster


@pytest.fixture
async def client():
    endpoint = os.environ.get("TEMPORAL_ADDRESS")
    if not endpoint:
        pytest.skip("explicit kind Temporal endpoint required")
    return await Client.connect(endpoint)


@pytest.mark.parametrize("window", ["before_dispatch", "after_effect", "after_candidate"])
async def test_unaccepted_execution_never_redispatched(client, tmp_path, window):
    ident = "fault-" + uuid.uuid4().hex
    runtime = FixtureRuntime(tmp_path, ident)
    runtime.fail_at = window
    transport = await runtime.start()
    activities = Activities(runtime, transport, runtime.tape)
    try:
        async with Worker(
            client,
            task_queue=ident,
            workflows=[SessionWorkflow],
            activities=activities.registered(),
        ):
            result = await client.execute_workflow(
                SessionWorkflow.run,
                {"session": ident},
                id=ident,
                task_queue=ident,
                execution_timeout=timedelta(minutes=2),
            )
        assert result["status"] == "OUTCOME_UNKNOWN"
        assert result["position"]["cursor"] == 0 and result["cleanup"] == "DELETED"
        assert len(runtime.case.effects) == (0 if window == "before_dispatch" else 1)
        assert len(runtime.candidates) == (1 if window == "after_candidate" else 0)
    finally:
        await runtime.stop()


async def test_sigkill_worker_after_accepted_completion(client, tmp_path):
    ident = "kill-" + uuid.uuid4().hex
    runtime = FixtureRuntime(tmp_path, ident)
    transport = await runtime.start()
    config = {
        "endpoint": os.environ["TEMPORAL_ADDRESS"],
        "url": transport.url,
        "queue": ident,
        "session": ident,
        "objects": str(tmp_path / "objects"),
        "binding": await runtime.provision({}),
    }
    workers = []

    async def start_worker():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.fixtures.worker_child",
            cwd=Path(__file__).resolve().parents[2],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        workers.append(process)
        process.stdin.write(json.dumps(config).encode() + b"\n")
        await process.stdin.drain()
        async with asyncio.timeout(20):
            assert await process.stdout.readline() == b"READY\n"
        return process

    try:
        first = await start_worker()
        handle = await client.start_workflow(
            SessionWorkflow.run,
            {"session": ident, "pause_at": 2},
            id=ident,
            task_queue=ident,
            execution_timeout=timedelta(minutes=2),
        )
        async with asyncio.timeout(45):
            while True:
                progress = await handle.query(SessionWorkflow.progress)
                if progress["position"] and progress["position"]["cursor"] == 2:
                    break
                await asyncio.sleep(0.2)
        first.kill()
        await first.wait()
        assert first.returncode != 0
        await start_worker()
        await handle.signal(SessionWorkflow.resume)
        result = await handle.result()
        assert result["status"] == "SUCCEEDED" and result["cleanup"] == "DELETED"
        assert runtime.case.effects == [
            "fixture",
            "inspect_repository",
            "fixture",
            "write_report",
            "fixture",
        ]
        await Replayer(workflows=[SessionWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        for process in workers:
            if process.returncode is None:
                process.kill()
            await process.wait()
        await runtime.stop()


async def test_silent_remote_operation_keeps_heartbeating(client, tmp_path):
    ident = "heartbeat-" + uuid.uuid4().hex
    runtime = FixtureRuntime(tmp_path, ident)
    runtime.delay_next = 12
    transport = await runtime.start()
    activities = Activities(runtime, transport, runtime.tape)
    try:
        async with Worker(
            client,
            task_queue=ident,
            workflows=[SessionWorkflow],
            activities=activities.registered(),
        ):
            result = await client.execute_workflow(
                SessionWorkflow.run,
                {"session": ident},
                id=ident,
                task_queue=ident,
                execution_timeout=timedelta(minutes=2),
            )
        assert result["status"] == "SUCCEEDED" and len(runtime.case.effects) == 5
    finally:
        await runtime.stop()


async def test_cancellation_stops_remote_work_and_removes_workspace(client, tmp_path):
    from temporalio.client import WorkflowFailureError

    ident = "cancel-" + uuid.uuid4().hex
    runtime = FixtureRuntime(tmp_path, ident)
    runtime.delay_next = 120
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
                execution_timeout=timedelta(minutes=2),
            )
            async with asyncio.timeout(30):
                await runtime.started.wait()
            await handle.cancel()
            with pytest.raises(WorkflowFailureError):
                await handle.result()
        assert not runtime.workspace_root.exists()
        assert all(item[2].done() for item in runtime.dispatcher.tasks.values())
    finally:
        await runtime.stop()


async def test_missing_heartbeat_times_out_without_effect_retry(client, tmp_path):
    from temporalio.api.enums.v1 import TimeoutType

    ident = "timeout-" + uuid.uuid4().hex
    runtime = FixtureRuntime(tmp_path, ident)
    runtime.delay_next = 120
    transport = await runtime.start()
    activities = Activities(runtime, transport, runtime.tape)

    async def without_heartbeat(operation):
        return await operation

    activities.heartbeating = without_heartbeat
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
                execution_timeout=timedelta(minutes=2),
            )
            result = await handle.result()
        assert result["status"] == "OUTCOME_UNKNOWN" and result["cleanup"] == "DELETED"
        history = await handle.fetch_history()
        timeouts = [
            e.activity_task_timed_out_event_attributes
            for e in history.events
            if e.HasField("activity_task_timed_out_event_attributes")
        ]
        assert any(
            e.failure.timeout_failure_info.timeout_type == TimeoutType.TIMEOUT_TYPE_HEARTBEAT
            for e in timeouts
        )
        assert len(runtime.dispatcher.tasks) == 1
    finally:
        await runtime.stop()
