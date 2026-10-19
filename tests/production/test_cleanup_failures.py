"""Cleanup retries must preserve uncertainty and stop explicit permanent denials."""

import grpc
import pytest
from openshell.sandbox import SandboxError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from zetra._runtime.activities import Activities


class GatewayFailure(grpc.RpcError):
    def __init__(self, status):
        self.status = status

    def code(self):
        return self.status


class Lifecycle:
    def __init__(self, outcome):
        self.outcome = outcome

    async def close(self, command):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    "code",
    [
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.UNIMPLEMENTED,
    ],
)
async def test_permanent_cleanup_rejection_stops_temporal_retries(code):
    activities = Activities(Lifecycle(GatewayFailure(code)), None, None)
    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(activities.close_session, {})
    assert failure.value.non_retryable
    assert code.name in str(failure.value)


@pytest.mark.parametrize(
    "failure",
    [
        GatewayFailure(grpc.StatusCode.UNAVAILABLE),
        GatewayFailure(grpc.StatusCode.DEADLINE_EXCEEDED),
        GatewayFailure(grpc.StatusCode.RESOURCE_EXHAUSTED),
        GatewayFailure(grpc.StatusCode.ABORTED),
        SandboxError("sandbox was not deleted within timeout"),
    ],
)
async def test_transient_cleanup_and_unconfirmed_deletion_remain_failures(failure):
    activities = Activities(Lifecycle(failure), None, None)
    with pytest.raises(type(failure)) as caught:
        await ActivityEnvironment().run(activities.close_session, {})
    assert caught.value is failure


async def test_identity_conflict_is_terminal_without_claiming_deletion():
    activities = Activities(Lifecycle({"cleanup": "REMEDIATION_REQUIRED"}), None, None)
    assert await ActivityEnvironment().run(activities.close_session, {}) == {
        "cleanup": "REMEDIATION_REQUIRED"
    }


async def test_cleanup_workflow_only_completes_when_deletion_is_confirmed(monkeypatch):
    from unittest.mock import AsyncMock

    from zetra._runtime import temporal

    outcome = {"cleanup": "NOT_CONFIRMED", "reason": "unsettled provisioning"}
    execute = AsyncMock(return_value=outcome)
    monkeypatch.setattr(temporal.workflow, "execute_activity", execute)
    with pytest.raises(ApplicationError) as failure:
        await temporal.CleanupWorkflow().run({})
    assert failure.value.non_retryable
    assert failure.value.type == "CleanupNotConfirmed"
    assert failure.value.details == (outcome,)
    execute.return_value = {"cleanup": "DELETED"}
    assert await temporal.CleanupWorkflow().run({}) == {"cleanup": "DELETED"}
