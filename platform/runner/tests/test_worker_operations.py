"""Current discovery follows routing CAS; drain reports never authorize retirement."""

from datetime import timedelta

import pytest
from temporalio.api.workflowservice import v1 as api

from runner.worker_operations import WorkerOperations
from runner.worker_versioning import GuardError, VersioningOperator
from tests.test_worker_versioning import (
    DEPLOYMENT,
    IMAGE,
    NOW,
    TemporalService,
    HealthyInventory,
    timestamp,
)


@pytest.mark.asyncio
async def test_current_discovery_requires_exact_routed_build_image_and_poller_ownership():
    service = TemporalService()
    service.current = "new"
    ops = WorkerOperations(
        VersioningOperator(service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW)
    )
    result = await ops.current("sessions", "governed-v1", IMAGE)
    assert result["build_id"] == "new"
    assert result["workers"]["pods"][0]["name"] == "worker-new"
    assert result["pool"] == "governed-v1"


@pytest.mark.asyncio
async def test_current_discovery_rejects_route_change_during_inventory():
    service = TemporalService()
    service.current = "new"

    class RacingInventory(HealthyInventory):
        async def candidate(self, *args):
            value = await super().candidate(*args)
            service.token = b"raced"
            return value

    ops = WorkerOperations(
        VersioningOperator(service, RacingInventory(), tenant="tenant-test", now=lambda: NOW)
    )
    with pytest.raises(GuardError, match="changed"):
        await ops.current("sessions", "governed-v1", IMAGE)


class DrainingService(TemporalService):
    async def describe_namespace(self, request, **kwargs):
        return api.DescribeNamespaceResponse(
            namespace_info={"name": "agents-tenant-test"},
            config={"workflow_execution_retention_ttl": {"seconds": 7 * 86400}},
        )

    async def describe_worker_deployment_version(self, request, **kwargs):
        result = await super().describe_worker_deployment_version(request, **kwargs)
        result.worker_deployment_version_info.drainage_info.status = self.drain_status
        result.worker_deployment_version_info.drainage_info.last_changed_time.CopyFrom(
            timestamp(NOW - timedelta(hours=1))
        )
        result.worker_deployment_version_info.drainage_info.last_checked_time.CopyFrom(timestamp())
        return result

    async def list_workflow_executions(self, request, **kwargs):
        assert request.query == f'TemporalWorkerDeploymentVersion = "{DEPLOYMENT}:new"'
        return api.ListWorkflowExecutionsResponse()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0, 1, 2])
async def test_empty_visibility_never_substitutes_for_server_drain_or_retention(status):
    service = DrainingService()
    service.drain_status = status
    ops = WorkerOperations(
        VersioningOperator(service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW)
    )
    result = await ops.drain(DEPLOYMENT, "new")
    assert result["checks"]["server_drained"] is (status == 2)
    assert result["retirement_allowed"] is False
    assert result["checks"]["seven_day_inactive_observation"] is False


@pytest.mark.asyncio
async def test_current_build_can_never_be_reported_drained_for_retirement():
    service = DrainingService()
    service.current = "new"
    service.drain_status = 2
    ops = WorkerOperations(
        VersioningOperator(service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW)
    )
    result = await ops.drain(DEPLOYMENT, "new")
    assert result["checks"]["not_current"] is False
    assert result["retirement_allowed"] is False


@pytest.mark.asyncio
async def test_visibility_bound_cannot_be_mistaken_for_complete_drain():
    class Infinite(DrainingService):
        async def list_workflow_executions(self, request, **kwargs):
            return api.ListWorkflowExecutionsResponse(next_page_token=b"more")

    service = Infinite()
    service.drain_status = 2
    ops = WorkerOperations(
        VersioningOperator(service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW)
    )
    with pytest.raises(GuardError, match="bounded"):
        await ops.drain(DEPLOYMENT, "new")


@pytest.mark.asyncio
async def test_visibility_run_must_correlate_with_actual_queue_and_version():
    class WrongVersion(DrainingService):
        async def list_workflow_executions(self, request, **kwargs):
            return api.ListWorkflowExecutionsResponse(
                executions=[{"execution": {"workflow_id": "test", "run_id": "run"}}]
            )

        async def describe_workflow_execution(self, request, **kwargs):
            return api.DescribeWorkflowExecutionResponse(
                workflow_execution_info={
                    "task_queue": "foreign",
                    "status": 2,
                    "close_time": timestamp(),
                    "versioning_info": {
                        "deployment_version": {"deployment_name": DEPLOYMENT, "build_id": "other"}
                    },
                }
            )

    service = WrongVersion()
    service.drain_status = 2
    result = await WorkerOperations(
        VersioningOperator(service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW)
    ).drain(DEPLOYMENT, "new")
    assert result["checks"]["visible_runs_correlated"] is False
    assert result["retirement_allowed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["open", "unspecified", "queue", "version", "run", "namespace"])
async def test_replay_refuses_unbound_or_open_history_before_fetching_payloads(broken):
    from types import SimpleNamespace as N
    from unittest.mock import Mock

    class ReplayService(TemporalService):
        async def describe_workflow_execution(self, request, **kwargs):
            info = {
                "execution": {"workflow_id": "workflow", "run_id": "run"},
                "task_queue": "sessions-tenant-test-governed-v1",
                "status": 2,
                "close_time": timestamp(),
                "versioning_info": {
                    "deployment_version": {"deployment_name": DEPLOYMENT, "build_id": "new"}
                },
            }
            if broken == "open":
                info["status"] = 1
            if broken == "unspecified":
                info["status"] = 0
            if broken == "queue":
                info["task_queue"] = "foreign"
            if broken == "version":
                info["versioning_info"]["deployment_version"]["build_id"] = "other"
            if broken == "run":
                info["execution"]["run_id"] = "other"
            return api.DescribeWorkflowExecutionResponse(workflow_execution_info=info)

    client = N(
        namespace="foreign" if broken == "namespace" else "agents-tenant-test",
        get_workflow_handle=Mock(),
    )
    ops = WorkerOperations(
        VersioningOperator(
            ReplayService(), HealthyInventory(), tenant="tenant-test", now=lambda: NOW
        )
    )
    with pytest.raises(GuardError):
        await ops.replay(client, DEPLOYMENT, "new", "workflow", "run")
    client.get_workflow_handle.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken", [None, "activity_build", "activity_identity", "workflow_version"]
)
async def test_history_receipt_correlates_real_started_events_without_payload_output(broken):
    from types import SimpleNamespace as N
    from unittest.mock import AsyncMock, Mock
    from temporalio.api.history.v1 import HistoryEvent

    class Service(TemporalService):
        async def describe_workflow_execution(self, request, **kwargs):
            return api.DescribeWorkflowExecutionResponse(
                workflow_execution_info={
                    "execution": {"workflow_id": "workflow", "run_id": "run"},
                    "task_queue": "sessions-tenant-test-governed-v1",
                    "status": 2,
                    "close_time": timestamp(),
                    "versioning_info": {
                        "deployment_version": {"deployment_name": DEPLOYMENT, "build_id": "new"}
                    },
                }
            )

    events = [
        HistoryEvent(
            event_id=1,
            workflow_task_completed_event_attributes={
                "identity": "1@worker-new",
                "deployment_version": {
                    "deployment_name": DEPLOYMENT,
                    "build_id": "other" if broken == "workflow_version" else "new",
                },
            },
        ),
        HistoryEvent(
            event_id=2,
            activity_task_started_event_attributes={
                "identity": "1@foreign" if broken == "activity_identity" else "1@worker-new",
                "worker_version": {
                    "build_id": "other" if broken == "activity_build" else "new",
                    "use_versioning": False,
                },
            },
        ),
    ]
    history = N(events=events, to_json=lambda: "private-history")
    client = N(
        namespace="agents-tenant-test",
        get_workflow_handle=Mock(return_value=N(fetch_history=AsyncMock(return_value=history))),
    )
    ops = WorkerOperations(
        VersioningOperator(Service(), HealthyInventory(), tenant="tenant-test", now=lambda: NOW)
    )
    result = await ops.history_receipt(client, DEPLOYMENT, "new", IMAGE, "workflow", "run")
    assert result["result"] == ("pass" if broken is None else "fail")
    assert "private-history" not in str(result)
