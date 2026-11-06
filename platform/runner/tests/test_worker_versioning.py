"""Version promotion never bypasses routing CAS or poller safety checks."""

import hashlib
from datetime import UTC, datetime

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from temporalio.api.deployment.v1 import (
    RoutingConfig,
    WorkerDeploymentInfo,
    WorkerDeploymentVersion,
    WorkerDeploymentVersionInfo,
    WorkerDeploymentOptions,
)
from temporalio.api.workflowservice import v1 as api
from temporalio.api.taskqueue.v1 import PollerInfo

DEPLOYMENT = "session-runner-tenant-test-governed-v1"
QUEUE = "sessions-tenant-test-governed-v1"
IMAGE = "registry.almckay.io/platform/session-runner:test@sha256:" + "a" * 64
NOW = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)
TOKEN = b"test-routing-cas"
TOKEN_HASH = hashlib.sha256(TOKEN).hexdigest()


def timestamp(value=NOW):
    out = Timestamp()
    out.FromDatetime(value)
    return out


class TemporalService:
    """The real protobuf boundary; mutations preserve a CAS-controlled state."""

    def __init__(self):
        self.current = "old"
        self.token = TOKEN
        self.mutations = []
        self.ramp = ""
        self.fail_after_write = False
        self.missing_type = None
        self.poller_identity = "1@worker-new"
        self.poller_build = "new"
        self.poller_at = NOW
        self.version_queues = [(QUEUE, 1), (QUEUE, 2)]

    async def describe_worker_deployment(self, request, **kwargs):
        assert request.namespace == "agents-tenant-test"
        assert request.deployment_name == DEPLOYMENT
        return api.DescribeWorkerDeploymentResponse(
            conflict_token=self.token,
            worker_deployment_info=WorkerDeploymentInfo(
                name=DEPLOYMENT,
                routing_config=RoutingConfig(
                    current_deployment_version=WorkerDeploymentVersion(
                        deployment_name=DEPLOYMENT if self.current else "", build_id=self.current
                    ),
                    current_version=f"{DEPLOYMENT}.{self.current}"
                    if self.current
                    else "__unversioned__",
                    ramping_version=self.ramp,
                ),
            ),
        )

    async def describe_worker_deployment_version(self, request, **kwargs):
        assert request.namespace == "agents-tenant-test"
        assert request.deployment_version == WorkerDeploymentVersion(
            deployment_name=DEPLOYMENT, build_id="new"
        )
        return api.DescribeWorkerDeploymentVersionResponse(
            worker_deployment_version_info=WorkerDeploymentVersionInfo(
                version=DEPLOYMENT + ".new",
                deployment_version=WorkerDeploymentVersion(
                    deployment_name=DEPLOYMENT, build_id="new"
                ),
                task_queue_infos=[
                    {"name": name, "type": kind} for name, kind in self.version_queues
                ],
            )
        )

    async def describe_task_queue(self, request, **kwargs):
        assert request.namespace == "agents-tenant-test"
        if request.task_queue.name == "sessions-tenant-test":
            return api.DescribeTaskQueueResponse(
                versioning_info={"current_version": "__unversioned__"},
                pollers=[PollerInfo(identity="1@legacy-worker", last_access_time=timestamp())],
            )
        assert request.task_queue.name == QUEUE
        pollers = (
            []
            if request.task_queue_type == self.missing_type
            else [
                PollerInfo(
                    identity=self.poller_identity,
                    last_access_time=timestamp(self.poller_at),
                    deployment_options=WorkerDeploymentOptions(
                        deployment_name=DEPLOYMENT,
                        build_id=self.poller_build,
                        worker_versioning_mode=2,
                    ),
                )
            ]
        )
        return api.DescribeTaskQueueResponse(pollers=pollers)

    async def set_worker_deployment_current_version(self, request, **kwargs):
        assert request.namespace == "agents-tenant-test"
        assert request.deployment_name == DEPLOYMENT
        assert request.conflict_token == self.token
        assert request.allow_no_pollers is False
        assert request.ignore_missing_task_queues is False
        assert request.build_id == "new"
        self.mutations.append(request)
        self.current = request.build_id
        self.token = b"new-cas"
        if self.fail_after_write:
            raise TimeoutError("sensitive remote error")
        return api.SetWorkerDeploymentCurrentVersionResponse(conflict_token=self.token)


class HealthyInventory:
    async def legacy(self, role):
        assert role == "sessions"
        return {
            "deployment_uid": "legacy-uid",
            "pods": [{"name": "legacy-worker", "uid": "legacy-pod-uid"}],
        }

    async def candidate(self, deployment, build_id, image):
        assert (deployment, build_id, image) == (DEPLOYMENT, "new", IMAGE)
        return {
            "deployment_uid": "6bc25693-3cba-4ef5-83b5-0de389c36079",
            "build_id": build_id,
            "image": image,
            "node": "strix",
            "pods": [{"name": "worker-new", "uid": "41649c18-8588-41c1-89f1-019858847720"}],
        }


@pytest.mark.asyncio
async def test_distinct_initial_promotion_preserves_legacy_workers_and_queue():
    from runner.worker_versioning import VersioningOperator

    service = TemporalService()
    service.current = ""
    operator = VersioningOperator(
        service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW
    )
    result = await operator.promote(
        DEPLOYMENT,
        "new",
        IMAGE,
        expected_current="__unversioned__",
        expected_conflict_sha256=TOKEN_HASH,
    )
    assert result["status"] == "confirmed"
    assert result["legacy"]["workers"]["pods"][0]["name"] == "legacy-worker"
    assert len(service.mutations) == 1


@pytest.mark.asyncio
async def test_legacy_deployment_never_accepts_promotion():
    from runner.worker_versioning import GuardError, VersioningOperator

    service = TemporalService()
    operator = VersioningOperator(
        service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW
    )
    with pytest.raises(GuardError, match="allowlist"):
        await operator.promote(
            "session-runner-tenant-test",
            "new",
            IMAGE,
            expected_current="__unversioned__",
            expected_conflict_sha256=TOKEN_HASH,
        )
    assert service.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["current", "token", "ramp"])
async def test_changed_routing_state_stops_before_mutation(changed):
    from runner.worker_versioning import GuardError, VersioningOperator

    service = TemporalService()
    if changed == "current":
        service.current = "other"
    elif changed == "token":
        service.token = b"concurrent-change"
    else:
        service.ramp = DEPLOYMENT + ".other"
    operator = VersioningOperator(
        service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW
    )
    with pytest.raises(GuardError):
        await operator.promote(
            DEPLOYMENT,
            "new",
            IMAGE,
            expected_current="old",
            expected_conflict_sha256=TOKEN_HASH,
        )
    assert service.mutations == []


@pytest.mark.asyncio
async def test_real_promotion_uses_fresh_cas_and_both_server_guards():
    from runner.worker_versioning import VersioningOperator

    service = TemporalService()
    receipts = []
    operator = VersioningOperator(
        service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW, record=receipts.append
    )
    result = await operator.promote(
        DEPLOYMENT, "new", IMAGE, expected_current="old", expected_conflict_sha256=TOKEN_HASH
    )
    assert result["status"] == "confirmed" and result["current"] == "new"
    assert service.current == "new" and len(service.mutations) == 1
    assert receipts[0]["status"] == "mutation_pending"
    assert receipts[-1] == result
    assert TOKEN.decode() not in str(receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        "activity",
        "workflow",
        "stale",
        "future",
        "wrong_pod",
        "wrong_build",
        "missing_queue",
        "extra_queue",
    ],
)
async def test_promotion_requires_complete_exact_fresh_real_pollers(broken):
    from datetime import timedelta
    from runner.worker_versioning import GuardError, VersioningOperator

    service = TemporalService()
    if broken in {"activity", "workflow"}:
        service.missing_type = 2 if broken == "activity" else 1
    elif broken == "stale":
        service.poller_at = NOW - timedelta(seconds=121)
    elif broken == "future":
        service.poller_at = NOW + timedelta(seconds=30)
    elif broken == "wrong_pod":
        service.poller_identity = "1@unrelated-pod"
    elif broken == "wrong_build":
        service.poller_build = "old"
    elif broken == "missing_queue":
        service.version_queues = [(QUEUE, 1)]
    else:
        service.version_queues.append(("unexpected", 1))
    operator = VersioningOperator(
        service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW
    )
    with pytest.raises(GuardError):
        await operator.promote(
            DEPLOYMENT, "new", IMAGE, expected_current="old", expected_conflict_sha256=TOKEN_HASH
        )
    assert service.mutations == []


@pytest.mark.asyncio
async def test_timeout_after_mutation_is_reconciled_but_never_retried_or_reported_confirmed():
    from runner.worker_versioning import VersioningOperator

    service = TemporalService()
    service.fail_after_write = True
    receipts = []
    operator = VersioningOperator(
        service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW, record=receipts.append
    )
    result = await operator.promote(
        DEPLOYMENT, "new", IMAGE, expected_current="old", expected_conflict_sha256=TOKEN_HASH
    )
    assert service.current == "new" and len(service.mutations) == 1
    assert result["status"] == "uncertain" and result["observed_current"] == "new"
    assert "sensitive" not in str(receipts) and TOKEN.decode() not in str(receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["BAD", "bad.build", "bad:build", "bad_build", "x" * 41])
@pytest.mark.parametrize("representation", ["structured", "legacy_string"])
async def test_routing_rejects_malformed_current_build_ids(bad, representation):
    from runner.worker_versioning import GuardError, VersioningOperator

    service = TemporalService()
    service.current = bad
    if representation == "legacy_string":
        original = service.describe_worker_deployment

        async def legacy_string(request, **kwargs):
            value = await original(request, **kwargs)
            value.worker_deployment_info.routing_config.ClearField("current_deployment_version")
            return value

        service.describe_worker_deployment = legacy_string
    operator = VersioningOperator(
        service, HealthyInventory(), tenant="tenant-test", now=lambda: NOW
    )
    with pytest.raises(GuardError, match="build"):
        await operator.routing(DEPLOYMENT)
