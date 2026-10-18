"""Real Temporal history must reject foreign runtimes and reset runs before transport."""

import asyncio
import hashlib
import os
import uuid
from unittest.mock import Mock

import pytest
from temporalio import activity
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import Client
from temporalio.worker import Worker

from zetra._runtime.artifacts import FileStore, Position
from zetra._runtime.openshell import policy
from zetra._runtime.records import canonical_bytes
from zetra._runtime.temporal import SessionWorkflow
from zetra.manifest import AgentSpec, Release
from zetra.session import SessionHandle, SessionRequest, load_execution
from zetra.worker import create_worker

pytestmark = pytest.mark.cluster


class CompletedFixture:
    """Complete the real Workflow without invoking any sandbox transport."""

    @activity.defn(name="provision_session")
    async def provision(self, start: dict) -> dict:
        return {"position": start["position"]}

    @activity.defn(name="advance_runner")
    async def advance(self, command: dict) -> dict:
        return {"status": "done", "result": {"final_text": "identity fixture"}}

    @activity.defn(name="close_session")
    async def close(self, command: dict) -> dict:
        return {"cleanup": "DELETED"}


async def temporal_client():
    if not os.environ.get("TEMPORAL_ADDRESS"):
        pytest.skip("requires real Temporal via TEMPORAL_ADDRESS; no OpenShell needed")
    return await Client.connect(os.environ["TEMPORAL_ADDRESS"])


def request_for(store, *, queue, runtime_id="lab"):
    snapshot = store.put(canonical_bytes({"version": 1, "files": [], "directories": []}))
    return SessionRequest(
        session="identity-" + uuid.uuid4().hex,
        message="Validate execution identity.",
        position=Position(workspace=snapshot),
        release=Release(
            lock_sha256="a" * 64,
            sdk_revision="b" * 40,
            agent=AgentSpec(name="identity", owner="test", entrypoint="identity.agent:create"),
            image="identity@sha256:" + "1" * 64,
            source_sha256="2" * 64,
            policy_sha256=hashlib.sha256(
                policy().SerializeToString(deterministic=True)
            ).hexdigest(),
        ),
        runtime_id=runtime_id,
        task_queue=queue,
    )


async def assert_rejected_by_worker(temporal, request, execution, store, *, reason):
    gateway = Mock()
    async with create_worker(
        temporal,
        task_queue=request.task_queue,
        runtime_id="lab",
        sandbox_client=gateway,
        artifacts=store,
        key=b"x" * 32,
    ):
        result = await asyncio.wait_for(execution.result(follow_runs=False), timeout=60)
    assert result["status"] == "FAILED", result
    assert result["cleanup"] == "REMEDIATION_REQUIRED", result
    assert gateway.mock_calls == []
    history = await execution.fetch_history()
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    failures = [
        event.activity_task_failed_event_attributes.failure.message
        for event in history.events
        if event.HasField("activity_task_failed_event_attributes")
    ]
    assert any(reason in message for message in failures), failures
    assert "execute_model" not in scheduled
    assert "execute_tool" not in scheduled


async def test_real_reset_history_is_rejected_before_sandbox_access(tmp_path):
    temporal = await temporal_client()
    store = FileStore(tmp_path / "objects")
    request = request_for(store, queue="identity-" + uuid.uuid4().hex)
    fixture = CompletedFixture()
    async with Worker(
        temporal,
        task_queue=request.task_queue,
        workflows=[SessionWorkflow],
        activities=[fixture.provision, fixture.advance, fixture.close],
    ):
        original = await temporal.start_workflow(
            SessionWorkflow.run,
            request.start(),
            id=request.session,
            task_queue=request.task_queue,
        )
        assert (await asyncio.wait_for(original.result(), timeout=60))["status"] == "SUCCEEDED"
    history = await original.fetch_history()
    reset_point = next(
        event.event_id
        for event in history.events
        if event.HasField("workflow_task_completed_event_attributes")
    )
    response = await temporal.workflow_service.reset_workflow_execution(
        ResetWorkflowExecutionRequest(
            namespace=temporal.namespace,
            workflow_execution=WorkflowExecution(
                workflow_id=request.session, run_id=original.first_execution_run_id
            ),
            workflow_task_finish_event_id=reset_point,
            request_id=str(uuid.uuid4()),
            reason="Reject a real reset lineage before transport access",
        )
    )
    assert response.run_id != original.first_execution_run_id
    reset_handle = SessionHandle(
        namespace=temporal.namespace,
        workflow_id=request.session,
        run_id=response.run_id,
        runtime_id=request.runtime_id,
    )
    with pytest.raises(ValueError, match="lineage"):
        await load_execution(temporal, reset_handle)
    execution = temporal.get_workflow_handle(request.session, run_id=response.run_id)
    await assert_rejected_by_worker(temporal, request, execution, store, reason="lineage")


async def test_real_foreign_runtime_request_is_rejected_before_sandbox_access(tmp_path):
    temporal = await temporal_client()
    store = FileStore(tmp_path / "objects")
    request = request_for(store, queue="identity-" + uuid.uuid4().hex, runtime_id="foreign-runtime")
    execution = await temporal.start_workflow(
        SessionWorkflow.run,
        request.start(),
        id=request.session,
        task_queue=request.task_queue,
    )
    await assert_rejected_by_worker(
        temporal, request, execution, store, reason="Workflow start identity mismatch"
    )
