"""Temporal enforces permanent cleanup proof across racing/replaced clients."""

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from zetra._runtime.temporal import CleanupWorkflow
from zetra.client import SessionClient, _cleanup_id
from zetra.session import SessionHandle

pytestmark = pytest.mark.cluster


class Cleanup:
    def __init__(self):
        self.calls = 0
        self.confirmed = False

    @activity.defn(name="reconcile_session")
    async def reconcile(self, target: dict) -> dict:
        self.calls += 1
        return {"cleanup": "DELETED" if self.confirmed else "NOT_CONFIRMED"}


async def test_failed_cleanup_can_retry_but_success_cannot_be_replaced():
    endpoint = os.environ.get("TEMPORAL_ADDRESS")
    if not endpoint:
        pytest.skip("requires real Temporal")
    temporal = await Client.connect(endpoint)
    ident = "cleanup-proof-" + uuid.uuid4().hex
    handle = SessionHandle(namespace="default", workflow_id=ident, run_id="exact", runtime_id=ident)

    def client():
        value = SessionClient(temporal, task_queue=ident, runtime_id=ident, artifacts=None)
        # Only the terminal source execution is a fixture; all cleanup RPCs,
        # identity validation, history, retries and conflict arbitration are real.
        value._execution = AsyncMock(
            return_value=SimpleNamespace(
                describe=AsyncMock(
                    return_value=SimpleNamespace(status=SimpleNamespace(name="COMPLETED"))
                ),
                result=AsyncMock(return_value={"status": "SUCCEEDED", "cleanup": "NOT_CONFIRMED"}),
            )
        )
        return value

    cleanup = Cleanup()
    async with Worker(
        temporal, task_queue=ident, workflows=[CleanupWorkflow], activities=[cleanup.reconcile]
    ):
        assert (await client().reconcile(handle))["cleanup"] == "NOT_CONFIRMED"
        failed = await temporal.get_workflow_handle(_cleanup_id(handle)).describe()
        assert failed.status.name == "FAILED"
        cleanup.confirmed = True
        results = await asyncio.gather(*(client().reconcile(handle) for _ in range(8)))
        assert all(r["cleanup"] == "DELETED" and r["status"] == "SUCCEEDED" for r in results)
        success = await temporal.get_workflow_handle(_cleanup_id(handle)).describe()
        assert success.status.name == "COMPLETED" and success.run_id != failed.run_id
    # No polling worker and a newly constructed client: server retains proof.
    cleanup.confirmed = False
    async with asyncio.timeout(10):
        assert (await client().reconcile(handle))["cleanup"] == "DELETED"
        assert (await client().wait(handle))["cleanup"] == "DELETED"
        assert (await client().status(handle))["result"]["cleanup"] == "DELETED"
    assert cleanup.calls == 2
    assert (
        await temporal.get_workflow_handle(_cleanup_id(handle)).describe()
    ).run_id == success.run_id
    history = await temporal.get_workflow_handle(
        _cleanup_id(handle), run_id=success.run_id
    ).fetch_history()
    await Replayer(workflows=[CleanupWorkflow]).replay_workflow(history)
