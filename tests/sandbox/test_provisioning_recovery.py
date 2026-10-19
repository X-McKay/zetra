"""Real gateway provisioning with bounded faults at the native RPC boundary.

These tests delay dispatch or discard an acknowledgement in the client. They do
not pause an OpenShell server transaction or emulate a gateway process crash.
Every dispatched create runs against the configured OpenShell 0.1.4 gateway.
"""

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import grpc
import pytest
from openshell._proto import openshell_pb2
from temporalio.worker import Replayer

from tests.sandbox.test_sdk import sdk_runtime, sdk_worker
from zetra._runtime.activities import Activities
from zetra._runtime.artifacts import FileStore
from zetra._runtime.temporal import SessionWorkflow
from zetra.client import SessionClient
from zetra.manifest import load_release
from zetra.worker import create_worker

pytestmark = pytest.mark.cluster


class InjectedRpcError(grpc.RpcError):
    def __init__(self, code):
        self.status = code

    def code(self):
        return self.status


def copy_request(request):
    copied = openshell_pb2.CreateSandboxRequest()
    copied.CopyFrom(request)
    return copied


async def remove_exact(gateway, ref):
    """Test teardown only: delete the exact immutable instance we created."""
    try:
        current = await asyncio.to_thread(gateway.get, ref.name, workspace="default")
    except grpc.RpcError as exc:
        if exc.code() != grpc.StatusCode.NOT_FOUND:
            raise
    else:
        assert current.id == ref.id
        try:
            await asyncio.to_thread(gateway.stop, ref.name, workspace="default")
        except grpc.RpcError as exc:
            if exc.code() not in (
                grpc.StatusCode.FAILED_PRECONDITION,
                grpc.StatusCode.NOT_FOUND,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.UNAVAILABLE,
            ):
                raise
        try:
            await asyncio.to_thread(
                gateway.delete, ref.name, workspace="default", allow_missing=True
            )
        except grpc.RpcError as exc:
            if exc.code() not in (grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNAVAILABLE):
                raise
    await asyncio.to_thread(
        gateway.wait_deleted,
        ref.name,
        workspace="default",
        expected_sandbox_id=ref.id,
        timeout_seconds=60,
    )


async def test_lost_create_ack_fresh_worker_replays_native_request(tmp_path, monkeypatch):
    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        release = load_release(".state/reporter-release.json")
        native_create = gateway._stub.CreateSandbox
        requests, responses = [], []
        lost_ack = asyncio.Event()
        original_create = Activities.create_session

        def create(request, **kwargs):
            requests.append(copy_request(request))
            response = native_create(request, **kwargs)
            responses.append(response)
            if len(requests) == 1:
                raise InjectedRpcError(grpc.StatusCode.UNAVAILABLE)
            return response

        async def park_lost_ack(self, command):
            try:
                return await original_create(self, command)
            except InjectedRpcError:
                lost_ack.set()
                await asyncio.Event().wait()

        handle = None
        monkeypatch.setattr(gateway._stub, "CreateSandbox", create)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(Activities, "create_session", park_lost_ack)
                async with create_worker(
                    temporal,
                    sandbox_client=gateway,
                    key=key,
                    graceful_shutdown_timeout=timedelta(seconds=0),
                    **options,
                ):
                    handle = await client.submit(
                        release,
                        session="lost-create-" + uuid.uuid4().hex,
                        message="Complete your task.",
                    )
                    await asyncio.wait_for(lost_ack.wait(), timeout=180)
            assert responses and responses[0].sandbox.metadata.id
            assert requests[0].request_id, "create must carry a durable native request ID"
            original_id = responses[0].sandbox.metadata.id
            replacement_options = {**options, "artifacts": FileStore(tmp_path / "objects")}
            replacement = SessionClient(temporal, **replacement_options)
            async with sdk_worker(temporal, gateway, key, replacement_options):
                result = await asyncio.wait_for(replacement.wait(handle), timeout=300)
            assert result["status"] == "REMEDIATION_REQUIRED", result
            assert result["execution_status"] == "SUCCEEDED", result
            assert result["cleanup"] == "NOT_CONFIRMED", result
            assert len(requests) == 2
            assert requests[0].request_id == requests[1].request_id
            assert requests[0].SerializeToString(deterministic=True) == requests[
                1
            ].SerializeToString(deterministic=True)
            assert responses[1].sandbox.metadata.id == original_id
            history = await temporal.get_workflow_handle(
                handle.workflow_id,
                run_id=handle.run_id,
            ).fetch_history()
            scheduled = [
                event.activity_task_scheduled_event_attributes.activity_type.name
                for event in history.events
                if event.HasField("activity_task_scheduled_event_attributes")
            ]
            assert scheduled.count("execute_model") == 3
            assert scheduled.count("execute_tool") == 2
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
        finally:
            if handle is not None:
                workflow = temporal.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
                if (await workflow.describe()).status.name == "RUNNING":
                    await workflow.terminate(reason="provisioning recovery test teardown")
            for response in responses[:1]:
                await remove_exact(gateway, response.sandbox.metadata)


async def test_retired_delayed_create_cannot_prove_deletion(tmp_path, monkeypatch):
    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        release = load_release(".state/reporter-release.json")
        native_create = gateway._stub.CreateSandbox
        release_dispatch = threading.Event()
        retired = asyncio.Event()
        original_create = Activities.create_session
        requests = []
        executor = ThreadPoolExecutor(max_workers=1)
        late = None

        def delayed(request, kwargs):
            assert release_dispatch.wait(60), "test did not release delayed dispatch"
            return native_create(request, **kwargs)

        def create(request, **kwargs):
            nonlocal late
            requests.append(copy_request(request))
            if len(requests) == 1:
                late = executor.submit(delayed, copy_request(request), kwargs)
                raise InjectedRpcError(grpc.StatusCode.UNAVAILABLE)
            return native_create(request, **kwargs)

        async def park_retired(self, command):
            try:
                return await original_create(self, command)
            except InjectedRpcError:
                retired.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(gateway._stub, "CreateSandbox", create)
        response = None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(Activities, "create_session", park_retired)
                async with create_worker(
                    temporal,
                    sandbox_client=gateway,
                    key=key,
                    graceful_shutdown_timeout=timedelta(seconds=0),
                    **options,
                ):
                    handle = await client.submit(
                        release,
                        session="late-create-" + uuid.uuid4().hex,
                        message="Complete your task.",
                    )
                    await asyncio.wait_for(retired.wait(), timeout=30)
                    await temporal.get_workflow_handle(
                        handle.workflow_id,
                        run_id=handle.run_id,
                    ).terminate(reason="qualify delayed create after terminal cleanup")
            replacement_options = {**options, "artifacts": FileStore(tmp_path / "objects")}
            replacement = SessionClient(temporal, **replacement_options)
            async with sdk_worker(temporal, gateway, key, replacement_options):
                result = await asyncio.wait_for(replacement.reconcile(handle), timeout=45)
            assert result["cleanup"] == "NOT_CONFIRMED", result
            assert requests[0].request_id, "retired create must retain its native request ID"
            # Cleanup ran while no request had reached the server. Dispatching
            # now produces a real late sandbox, proving absence was insufficient.
            release_dispatch.set()
            response = await asyncio.wait_for(asyncio.wrap_future(late), timeout=45)
            current = await asyncio.to_thread(
                gateway.get,
                response.sandbox.metadata.name,
                workspace="default",
            )
            assert current.id == response.sandbox.metadata.id
            await remove_exact(gateway, current)
            # Native idempotency records survive deletion. A replay must refuse
            # the retired original rather than creating a replacement instance.
            with pytest.raises(grpc.RpcError) as replay:
                await asyncio.to_thread(native_create, requests[0], timeout=20)
            assert replay.value.code() == grpc.StatusCode.FAILED_PRECONDITION
            assert replay.value.error_info.reason == "REQUEST_REPLAY_UNAVAILABLE"
            with pytest.raises(grpc.RpcError) as missing:
                await asyncio.to_thread(gateway.get, current.name, workspace="default")
            assert missing.value.code() == grpc.StatusCode.NOT_FOUND
        finally:
            release_dispatch.set()
            if late is not None and response is None:
                response = await asyncio.wait_for(asyncio.wrap_future(late), timeout=45)
            if response is not None:
                await remove_exact(gateway, response.sandbox.metadata)
            executor.shutdown(wait=False)


@pytest.mark.parametrize("recovery", ["failure", "replacement_failure", "replacement_cancel"])
async def test_recorded_create_readiness_failure_confirms_native_deletion(
    tmp_path, monkeypatch, recovery
):
    """Readiness retries and cancellation cannot retire or repeat a settled create."""
    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        release = load_release(".state/reporter-release.json")
        native_create = gateway._stub.CreateSandbox
        original_ready = Activities.wait_session_ready
        responses = []
        readiness = asyncio.Event()
        attempts = []
        handle = None

        def create(request, **kwargs):
            response = native_create(request, **kwargs)
            responses.append(response)
            return response

        def fail_supervisor_readiness(*args, **kwargs):
            raise ValueError("injected native supervisor readiness failure")

        async def wait_ready(self, command):
            # Temporal cannot dispatch this Activity until creation's immutable
            # binding has been accepted into Workflow history.
            attempts.append(command)
            readiness.set()
            if recovery != "failure" and len(attempts) == 1:
                await asyncio.Event().wait()
            return await original_ready(self, command)

        monkeypatch.setattr(gateway._stub, "CreateSandbox", create)
        monkeypatch.setattr(gateway, "wait_ready", fail_supervisor_readiness)
        monkeypatch.setattr(Activities, "wait_session_ready", wait_ready)
        try:
            async with create_worker(
                temporal,
                sandbox_client=gateway,
                key=key,
                graceful_shutdown_timeout=timedelta(seconds=0),
                **options,
            ):
                handle = await client.submit(
                    release,
                    session="ready-failure-" + uuid.uuid4().hex,
                    message="Complete your task.",
                )
                await asyncio.wait_for(readiness.wait(), timeout=180)
                workflow = temporal.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
                history = await workflow.fetch_history()
                completed_ids = {
                    event.activity_task_completed_event_attributes.scheduled_event_id
                    for event in history.events
                    if event.HasField("activity_task_completed_event_attributes")
                }
                assert any(
                    event.event_id in completed_ids
                    and event.activity_task_scheduled_event_attributes.activity_type.name
                    == "create_session"
                    for event in history.events
                    if event.HasField("activity_task_scheduled_event_attributes")
                ), "creation binding must be recorded before readiness"
                if recovery == "failure":
                    result = await asyncio.wait_for(client.wait(handle), timeout=180)
            if recovery != "failure":
                # Reopen artifacts and instantiate a new router/lifecycle after
                # losing the original worker during the readiness Activity.
                replacement_options = {**options, "artifacts": FileStore(tmp_path / "objects")}
                replacement = SessionClient(temporal, **replacement_options)
                if recovery == "replacement_cancel":
                    await replacement.cancel(handle)
                async with sdk_worker(temporal, gateway, key, replacement_options):
                    result = await asyncio.wait_for(replacement.wait(handle), timeout=240)
            assert result["cleanup"] == "DELETED", result
            if recovery != "replacement_cancel":
                assert result["status"] == "FAILED", result
            assert len(responses) == 1, "readiness retry must never allocate a sandbox"
            if recovery == "replacement_failure":
                assert len(attempts) == 2
                assert attempts[0] == attempts[1], "readiness retries must retain the binding"
            ref = responses[0].sandbox.metadata
            with pytest.raises(grpc.RpcError) as missing:
                await asyncio.to_thread(gateway.get, ref.name, workspace="default")
            assert missing.value.code() == grpc.StatusCode.NOT_FOUND
            await asyncio.to_thread(
                gateway.wait_deleted,
                ref.name,
                workspace="default",
                expected_sandbox_id=ref.id,
                timeout_seconds=60,
            )
            history = await workflow.fetch_history()
            scheduled = [
                event.activity_task_scheduled_event_attributes.activity_type.name
                for event in history.events
                if event.HasField("activity_task_scheduled_event_attributes")
            ]
            assert scheduled.count("create_session") == 1
            assert "execute_model" not in scheduled
            assert "execute_tool" not in scheduled
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
        finally:
            if handle is not None:
                workflow = temporal.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
                if (await workflow.describe()).status.name == "RUNNING":
                    await workflow.terminate(reason="readiness recovery test teardown")
            for response in responses:
                await remove_exact(gateway, response.sandbox.metadata)
