"""A completed Activity must not swallow a workflow cancellation request."""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import Replayer, Worker

from tests.fixtures.runtime import FixtureRuntime
from zetra._runtime.activities import Activities
from zetra._runtime.temporal import SessionWorkflow

pytestmark = pytest.mark.cluster


@pytest.mark.parametrize(
    "boundary,effects",
    [
        ("create_session", []),
        ("wait_session_ready", []),
        ("advance_runner", []),
        ("execute_model", ["fixture"]),
        ("close_session", ["fixture", "inspect_repository", "fixture", "write_report", "fixture"]),
    ],
)
async def test_successful_activity_after_cancel_stops_next_boundary(tmp_path, boundary, effects):
    endpoint = os.environ.get("TEMPORAL_ADDRESS")
    if not endpoint:
        pytest.skip("explicit Temporal service required")
    client = await Client.connect(
        endpoint, namespace=os.environ.get("TEMPORAL_NAMESPACE", "default")
    )
    ident = "cancel-completion-" + uuid.uuid4().hex
    runtime = FixtureRuntime(tmp_path, ident)
    transport = await runtime.start()
    entered, release = asyncio.Event(), asyncio.Event()

    class CompletingActivities(Activities):
        async def gate(self, name):
            if name == boundary:
                entered.set()
                # Complete before receiving a heartbeat cancellation notice.
                await release.wait()

        @activity.defn
        async def create_session(self, command: dict) -> dict:
            await self.gate("create_session")
            return await super().create_session(command)

        @activity.defn
        async def wait_session_ready(self, command: dict) -> dict:
            await self.gate("wait_session_ready")
            return await super().wait_session_ready(command)

        @activity.defn
        async def advance_runner(self, command: dict) -> dict:
            await self.gate("advance_runner")
            return await super().advance_runner(command)

        @activity.defn
        async def execute_model(self, command: dict) -> dict:
            await self.gate("execute_model")
            return await super().execute_model(command)

        @activity.defn
        async def close_session(self, command: dict) -> dict:
            await self.gate("close_session")
            return await super().close_session(command)

    activities = CompletingActivities(runtime, transport, runtime.tape)
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
            await asyncio.wait_for(entered.wait(), 30)
            await handle.cancel()
            async with asyncio.timeout(10):
                while True:
                    history = await handle.fetch_history()
                    if any(
                        e.HasField("activity_task_cancel_requested_event_attributes")
                        for e in history.events
                    ):
                        break
                    await asyncio.sleep(0.05)
            release.set()
            with pytest.raises(WorkflowFailureError):
                await handle.result()
            assert (await handle.describe()).status.name == "CANCELED"
        assert runtime.case.effects == effects
        history = await handle.fetch_history()
        cancel = next(
            e.event_id
            for e in history.events
            if e.HasField("workflow_execution_cancel_requested_event_attributes")
        )
        assert all(
            e.activity_task_scheduled_event_attributes.activity_type.name == "close_session"
            for e in history.events
            if e.event_id > cancel and e.HasField("activity_task_scheduled_event_attributes")
        )
        assert not (runtime.root / "workspace").exists()
        await Replayer(workflows=[SessionWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        release.set()
        await runtime.stop()
