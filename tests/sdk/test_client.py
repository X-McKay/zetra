from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from zetra.client import SessionClient, normalized_result
from zetra.session import SessionHandle


def handle():
    return SessionHandle(
        namespace="default", workflow_id="one", run_id="run-1", runtime_id="runtime"
    )


class Temporal:
    namespace = "default"

    def __init__(self):
        self.execution = SimpleNamespace(
            result=AsyncMock(return_value={"status": "SUCCEEDED", "cleanup": "DELETED"}),
            cancel=AsyncMock(),
            describe=AsyncMock(
                return_value=SimpleNamespace(status=SimpleNamespace(name="RUNNING"))
            ),
            query=AsyncMock(return_value={"cursor": 1}),
        )
        self.start_workflow = AsyncMock(
            return_value=SimpleNamespace(first_execution_run_id="run-1")
        )
        self.calls = []

    def get_workflow_handle(self, workflow_id, **kwargs):
        self.calls.append((workflow_id, kwargs))
        return self.execution


@pytest.fixture
def client(tmp_path, monkeypatch):
    from zetra._runtime.artifacts import FileStore

    temporal = Temporal()
    monkeypatch.setattr(
        "zetra.client.load_execution",
        AsyncMock(return_value={"session": "one", "task_queue": "queue"}),
    )
    return SessionClient(
        temporal, task_queue="queue", runtime_id="runtime", artifacts=FileStore(tmp_path)
    )


def test_success_requires_confirmed_cleanup():
    assert (
        normalized_result({"status": "SUCCEEDED", "cleanup": "FAILED"})["status"]
        == "REMEDIATION_REQUIRED"
    )


async def test_wait_and_cancel_are_pinned_to_run(client):
    assert (await client.wait(handle()))["status"] == "SUCCEEDED"
    client.temporal.execution.result.assert_awaited_once_with(follow_runs=False)
    await client.cancel(handle())
    assert client.temporal.calls == [("one", {"run_id": "run-1"}), ("one", {"run_id": "run-1"})]


async def test_handle_runtime_mismatch_rejected_before_rpc(client):
    with pytest.raises(ValueError, match="runtime"):
        await client.wait(handle().model_copy(update={"runtime_id": "another"}))
    assert not client.temporal.calls


async def test_reconcile_only_closed_execution_and_preserves_uncertainty(client):
    with pytest.raises(ValueError, match="running"):
        await client.reconcile(handle())
    client.temporal.execution.describe.return_value.status.name = "TERMINATED"
    cleanup = SimpleNamespace(
        result=AsyncMock(return_value={"cleanup": "DELETED"}), first_execution_run_id="cleanup-run"
    )
    client.temporal.start_workflow.return_value = cleanup
    result = await client.reconcile(handle())
    assert result == {
        "status": "TERMINATED",
        "cleanup": "DELETED",
        "effect_status": "OUTCOME_UNKNOWN",
    }
    call = client.temporal.start_workflow.call_args
    assert call.args == ("CleanupWorkflow", handle().model_dump())
    assert call.kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE
    cleanup.result.assert_awaited_once_with(follow_runs=False)


async def test_duplicate_cleanup_uses_exception_run_id(client, monkeypatch):
    verify = AsyncMock()
    monkeypatch.setattr("zetra.client._verify_cleanup", verify)
    client.temporal.execution.describe.return_value.status.name = "TERMINATED"
    client.temporal.execution.result.return_value = {"cleanup": "DELETED"}
    client.temporal.start_workflow.side_effect = WorkflowAlreadyStartedError(
        "cleanup", "CleanupWorkflow", run_id="closed-cleanup"
    )
    await client.reconcile(handle())
    assert client.temporal.calls[-1][1] == {"run_id": "closed-cleanup"}

    assert verify.call_args.args[2:] == ("closed-cleanup", handle(), "queue")


@pytest.mark.parametrize(
    "field,value",
    [
        ("workflow_type", SimpleNamespace(name="OtherWorkflow")),
        ("task_queue", SimpleNamespace(name="another-queue")),
        ("original_execution_run_id", "other-run"),
        ("first_execution_run_id", "other-run"),
        ("continued_execution_run_id", "previous-run"),
        ("attempt", 2),
        ("cron_schedule", "* * * * *"),
    ],
)
async def test_cleanup_rejects_other_workflow_and_lineage(field, value):
    from zetra.client import _verify_cleanup

    attrs = SimpleNamespace(
        workflow_type=SimpleNamespace(name="CleanupWorkflow"),
        task_queue=SimpleNamespace(name="queue"),
        original_execution_run_id="cleanup-run",
        first_execution_run_id="cleanup-run",
        continued_execution_run_id="",
        attempt=1,
        cron_schedule="",
        input=SimpleNamespace(payloads=[]),
    )
    setattr(attrs, field, value)
    event = SimpleNamespace(
        HasField=lambda name: True, workflow_execution_started_event_attributes=attrs
    )
    execution = SimpleNamespace(
        fetch_history=AsyncMock(return_value=SimpleNamespace(events=[event]))
    )
    with pytest.raises(ValueError, match="identity or lineage"):
        await _verify_cleanup(object(), execution, "cleanup-run", handle(), "queue")


async def test_cleanup_rejects_another_execution_input():
    from zetra.client import _verify_cleanup

    attrs = SimpleNamespace(
        workflow_type=SimpleNamespace(name="CleanupWorkflow"),
        task_queue=SimpleNamespace(name="queue"),
        original_execution_run_id="cleanup-run",
        first_execution_run_id="cleanup-run",
        continued_execution_run_id="",
        attempt=1,
        cron_schedule="",
        input=SimpleNamespace(payloads=[]),
    )
    event = SimpleNamespace(
        HasField=lambda name: True, workflow_execution_started_event_attributes=attrs
    )
    execution = SimpleNamespace(
        fetch_history=AsyncMock(return_value=SimpleNamespace(events=[event]))
    )
    temporal = SimpleNamespace(
        data_converter=SimpleNamespace(decode=AsyncMock(return_value=[{"run_id": "another"}]))
    )
    with pytest.raises(ValueError, match="another execution"):
        await _verify_cleanup(temporal, execution, "cleanup-run", handle(), "queue")
    temporal.data_converter.decode.return_value = [handle().model_dump()]
    await _verify_cleanup(temporal, execution, "cleanup-run", handle(), "queue")


async def test_task_queue_mismatch_rejected_before_execution_rpc(client, monkeypatch):
    monkeypatch.setattr(
        "zetra.client.load_execution", AsyncMock(return_value={"task_queue": "other"})
    )
    with pytest.raises(ValueError, match="task queue"):
        await client.reconcile(handle())
    assert not client.temporal.calls


@pytest.mark.parametrize("status", ["SUCCEEDED", "OUTCOME_UNKNOWN"])
async def test_completed_cleanup_can_be_retried_without_changing_execution_outcome(client, status):
    client.temporal.execution.describe.return_value.status.name = "COMPLETED"
    original = {"status": status, "cleanup": "NOT_CONFIRMED", "position": {"cursor": 3}}
    client.temporal.execution.result.return_value = original
    client.temporal.start_workflow.return_value = SimpleNamespace(
        result=AsyncMock(return_value={"cleanup": "DELETED", "status": "SUCCEEDED"})
    )
    result = await client.reconcile(handle())
    assert result == {**original, "cleanup": "DELETED"}
    assert original["cleanup"] == "NOT_CONFIRMED"


async def test_unconfirmed_cleanup_workflow_can_be_attempted_again(client):
    client.temporal.execution.describe.return_value.status.name = "TERMINATED"
    cleanup = SimpleNamespace(
        result=AsyncMock(side_effect=[{"cleanup": "NOT_CONFIRMED"}, {"cleanup": "DELETED"}])
    )
    client.temporal.start_workflow.return_value = cleanup
    assert (await client.reconcile(handle()))["status"] == "REMEDIATION_REQUIRED"
    assert (await client.reconcile(handle()))["cleanup"] == "DELETED"
    assert client.temporal.start_workflow.await_count == 2
    for call in client.temporal.start_workflow.call_args_list:
        assert call.kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE
        assert call.kwargs["execution_timeout"].total_seconds() == 900


async def test_status_reads_verified_cleanup_without_starting_workflow(client, monkeypatch):
    client.temporal.execution.describe.return_value.status.name = "TERMINATED"
    monkeypatch.setattr(client, "_cleanup_result", AsyncMock(return_value={"cleanup": "DELETED"}))
    result = await client.status(handle())
    assert result["result"] == {
        "status": "TERMINATED",
        "cleanup": "DELETED",
        "effect_status": "OUTCOME_UNKNOWN",
    }
    client.temporal.start_workflow.assert_not_awaited()


async def test_cleanup_status_resolves_latest_then_reads_exact_verified_run(client, monkeypatch):
    client.temporal.execution.describe.return_value = SimpleNamespace(
        status=SimpleNamespace(name="COMPLETED"), run_id="cleanup-run"
    )
    client.temporal.execution.result.return_value = {"cleanup": "DELETED"}
    verify = AsyncMock()
    monkeypatch.setattr("zetra.client._verify_cleanup", verify)
    assert await client._cleanup_result(handle()) == {"cleanup": "DELETED"}
    assert client.temporal.calls[-2][1] == {}
    assert client.temporal.calls[-1][1] == {"run_id": "cleanup-run"}
    assert verify.call_args.args[2:] == ("cleanup-run", handle(), "queue")
    client.temporal.start_workflow.assert_not_awaited()


async def test_wait_observes_completed_cleanup_recovery_without_mutating(client, monkeypatch):
    client.temporal.execution.result.return_value = {
        "status": "SUCCEEDED",
        "cleanup": "NOT_CONFIRMED",
    }
    monkeypatch.setattr(client, "_cleanup_result", AsyncMock(return_value={"cleanup": "DELETED"}))
    assert await client.wait(handle()) == {"status": "SUCCEEDED", "cleanup": "DELETED"}
    client.temporal.start_workflow.assert_not_awaited()
