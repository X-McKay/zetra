"""Real OpenShell cleanup retries through Temporal, with bounded gateway list faults."""

import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import grpc
import pytest

from tests.sandbox.test_sdk import sdk_runtime, sdk_worker
from zetra._runtime.artifacts import FileStore
from zetra.client import SessionClient, _cleanup_id
from zetra.manifest import load_release
from zetra.session import execution_session

pytestmark = pytest.mark.cluster


class CleanupRpcError(grpc.RpcError):
    def __init__(self, status):
        self.status = status

    def code(self):
        return self.status


class CleanupFault:
    """Only close_all lists resources; provisioning and effects use real RPCs."""

    def __init__(self, native, status, failures):
        self.native = native
        self.status = status
        self.failures = failures
        self.calls = 0
        self.refs = []

    def __call__(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise CleanupRpcError(self.status)
        refs = self.native(**kwargs).all()
        self.refs.extend(refs)
        return SimpleNamespace(all=lambda: refs)


async def history_for(temporal, handle):
    return await temporal.get_workflow_handle(
        handle.workflow_id, run_id=handle.run_id
    ).fetch_history()


def assert_effects_once(history):
    schedules = [
        event.activity_task_scheduled_event_attributes
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    for name, expected in (("execute_model", 3), ("execute_tool", 2)):
        effects = [item for item in schedules if item.activity_type.name == name]
        assert len(effects) == expected
        assert all(item.retry_policy.maximum_attempts == 1 for item in effects)


def assert_cleanup_attempt(history, name, attempt):
    schedules = [
        event
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
        and event.activity_task_scheduled_event_attributes.activity_type.name == name
    ]
    assert len(schedules) == 1
    schedule = schedules[0]
    attrs = schedule.activity_task_scheduled_event_attributes
    assert attrs.schedule_to_close_timeout.seconds == 600
    assert attrs.retry_policy.maximum_attempts == 0
    assert attrs.retry_policy.maximum_interval.seconds == 30
    starts = [
        event.activity_task_started_event_attributes
        for event in history.events
        if event.HasField("activity_task_started_event_attributes")
        and event.activity_task_started_event_attributes.scheduled_event_id == schedule.event_id
    ]
    assert starts[-1].attempt == attempt
    assert any(
        event.HasField("activity_task_completed_event_attributes")
        and event.activity_task_completed_event_attributes.scheduled_event_id == schedule.event_id
        for event in history.events
    )


async def confirm_deleted(gateway, refs, handle):
    owner = hashlib.sha256(execution_session(handle).encode()).hexdigest()[:32]
    refs = [ref for ref in refs if ref.labels.get("bridge-session") == owner]
    assert refs, "qualification must observe an actual native sandbox"
    for name, sandbox_id in {(ref.name, ref.id) for ref in refs}:
        await asyncio.to_thread(
            gateway.wait_deleted,
            name,
            workspace="default",
            expected_sandbox_id=sandbox_id,
            timeout_seconds=60,
        )


async def teardown(temporal, client, gateway, key, options, handle):
    if handle is None:
        return
    execution = temporal.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
    if (await execution.describe()).status.name == "RUNNING":
        await execution.terminate(reason="cleanup recovery qualification teardown")
    async with sdk_worker(temporal, gateway, key, options):
        result = await asyncio.wait_for(client.reconcile(handle), timeout=300)
    assert result["cleanup"] == "DELETED", result


async def test_normal_close_recovers_after_four_transient_gateway_failures(tmp_path, monkeypatch):
    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        native_list = gateway.list
        fault = CleanupFault(native_list, grpc.StatusCode.UNAVAILABLE, 4)
        handle = None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(gateway, "list", fault)
                async with sdk_worker(temporal, gateway, key, options):
                    handle = await client.submit(
                        load_release(".state/reporter-release.json"),
                        session="cleanup-retry-" + uuid.uuid4().hex,
                        message="Complete your task.",
                    )
                    result = await asyncio.wait_for(client.wait(handle), timeout=360)
            assert result["status"] == "SUCCEEDED", result
            assert result["cleanup"] == "DELETED", result
            assert result["position"]["cursor"] == 5
            assert fault.calls == 5
            history = await history_for(temporal, handle)
            assert_cleanup_attempt(history, "close_session", 5)
            assert_effects_once(history)
            await confirm_deleted(gateway, fault.refs, handle)
        finally:
            await teardown(temporal, client, gateway, key, options, handle)


async def test_permanent_close_then_fresh_worker_reconciles_four_transient_failures(
    tmp_path, monkeypatch
):
    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        native_list = gateway.list
        denied = CleanupFault(native_list, grpc.StatusCode.PERMISSION_DENIED, 100)
        handle = None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(gateway, "list", denied)
                async with sdk_worker(temporal, gateway, key, options):
                    handle = await client.submit(
                        load_release(".state/reporter-release.json"),
                        session="cleanup-reconcile-" + uuid.uuid4().hex,
                        message="Complete your task.",
                    )
                    result = await asyncio.wait_for(client.wait(handle), timeout=300)
            assert result["status"] == "REMEDIATION_REQUIRED", result
            assert result["execution_status"] == "SUCCEEDED", result
            assert result["cleanup"] == "REMEDIATION_REQUIRED", result
            assert denied.calls == 1
            execution = temporal.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
            original_result = await execution.result(follow_runs=False)
            assert original_result["status"] == "SUCCEEDED"
            before = await history_for(temporal, handle)
            assert_effects_once(before)
            replacement_options = {**options, "artifacts": FileStore(tmp_path / "objects")}
            replacement = SessionClient(temporal, **replacement_options)
            transient = CleanupFault(native_list, grpc.StatusCode.UNAVAILABLE, 4)
            with monkeypatch.context() as patch:
                patch.setattr(gateway, "list", transient)
                async with sdk_worker(
                    temporal, gateway, key, replacement_options, approved_releases=set()
                ):
                    recovered = await asyncio.wait_for(replacement.reconcile(handle), timeout=300)
            assert recovered["status"] == "SUCCEEDED", recovered
            assert recovered["cleanup"] == "DELETED", recovered
            assert recovered["result"] == original_result["result"]
            assert recovered["position"] == original_result["position"]
            # Successful cleanup is retained even after worker shutdown/revocation.
            repeated = await asyncio.wait_for(
                asyncio.gather(*(replacement.reconcile(handle) for _ in range(4))), timeout=10
            )
            assert all(item == recovered for item in repeated)
            assert (await replacement.read_files(handle))["summary.md"]
            assert transient.calls == 5
            cleanup = temporal.get_workflow_handle(_cleanup_id(handle))
            description = await cleanup.describe()
            cleanup_history = await temporal.get_workflow_handle(
                _cleanup_id(handle), run_id=description.run_id
            ).fetch_history()
            assert_cleanup_attempt(cleanup_history, "reconcile_session", 5)
            after = await history_for(temporal, handle)
            assert after.to_json() == before.to_json()
            assert_effects_once(after)
            await confirm_deleted(gateway, transient.refs, handle)
        finally:
            await teardown(temporal, client, gateway, key, options, handle)
