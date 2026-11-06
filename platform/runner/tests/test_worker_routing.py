"""Historical inputs remain on legacy queues; governed workers are explicit."""

from dataclasses import asdict
from unittest.mock import Mock

import pytest
from temporalio.converter import DataConverter

from runner.types import InvalidInput, SessionInput
from runner import worker


def session(**extra):
    return SessionInput(
        "tenant-test",
        "conformance",
        "release",
        "e0ac048c-b480-4afe-a44d-f0307e6c8679",
        None,
        {},
        **extra,
    )


@pytest.mark.asyncio
async def test_historical_payload_defaults_to_legacy_queue():
    old = asdict(session())
    old.pop("worker_pool", None)
    payloads = await DataConverter.default.encode([old])
    inp = (await DataConverter.default.decode(payloads, [SessionInput]))[0]
    assert inp.worker_pool == "legacy"
    assert inp.task_queue == "sessions-tenant-test"
    assert worker.cleanup_queue("tenant-test") == "cleanup-tenant-test"


def test_governed_input_routes_only_to_separate_queue():
    inp = session(worker_pool="governed-v1")
    inp.validate()
    assert inp.task_queue == "sessions-tenant-test-governed-v1"
    assert worker.cleanup_queue("tenant-test", "governed-v1") == "cleanup-tenant-test-governed-v1"
    with pytest.raises(InvalidInput):
        session(worker_pool="unknown").validate()


@pytest.mark.parametrize(
    "pool,build,deployment",
    [
        ("legacy", "b1", "session-runner-tenant-test"),
        ("governed-v1", "", "session-runner-tenant-test-governed-v1"),
        ("governed-v1", "b1", "cleanup-reconciler-tenant-test-governed-v1"),
    ],
)
def test_worker_rejects_ambiguous_or_wrong_role_versioning(monkeypatch, pool, build, deployment):
    monkeypatch.setenv("WORKER_POOL", pool)
    monkeypatch.setenv("WORKER_BUILD_ID", build)
    monkeypatch.setenv("WORKER_DEPLOYMENT", deployment)
    with pytest.raises(ValueError):
        worker.session_worker(Mock(), "tenant-test", Mock())


def test_governed_worker_uses_exact_queue_and_pinned_version(monkeypatch):
    monkeypatch.setenv("WORKER_POOL", "governed-v1")
    monkeypatch.setenv("WORKER_BUILD_ID", "b1")
    monkeypatch.setenv("WORKER_DEPLOYMENT", "session-runner-tenant-test-governed-v1")
    constructor = Mock()
    monkeypatch.setattr(worker, "Worker", constructor)
    worker.session_worker(Mock(), "tenant-test", Mock())
    args = constructor.call_args.kwargs
    assert args["task_queue"] == "sessions-tenant-test-governed-v1"
    assert args["deployment_config"].use_worker_versioning is True
    assert args["deployment_config"].version.build_id == "b1"


@pytest.mark.asyncio
async def test_governed_worker_never_creates_legacy_schedule(monkeypatch):
    monkeypatch.setenv("WORKER_POOL", "governed-v1")
    client = Mock()
    await worker.ensure_reconciler_schedule(client, "tenant-test")
    client.create_schedule.assert_not_called()


@pytest.mark.asyncio
async def test_governed_submission_refuses_unpromoted_route_before_start():
    from unittest.mock import AsyncMock
    from runner.submit import submit
    from tests.test_worker_versioning import TemporalService

    service = TemporalService()
    service.current = ""
    client = Mock(namespace="agents-tenant-test", workflow_service=service)
    client.start_workflow = AsyncMock()
    with pytest.raises(ValueError, match="promoted"):
        await submit(client, session(worker_pool="governed-v1"))
    client.start_workflow.assert_not_called()


def test_existing_legacy_deployment_environment_remains_unversioned(monkeypatch):
    monkeypatch.delenv("WORKER_POOL", raising=False)
    monkeypatch.setenv("WORKER_BUILD_ID", "")
    monkeypatch.setenv("WORKER_DEPLOYMENT", "session-runner-tenant-test")
    assert worker._deployment("tenant-test", "sessions") is None


@pytest.mark.asyncio
async def test_governed_submission_preserves_t28_rpc_timeout_extension():
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from runner.submit import submit
    from tests.test_worker_versioning import TemporalService

    service = TemporalService()
    service.current = "new"
    client = Mock(namespace="agents-tenant-test", workflow_service=service)
    client.start_workflow = AsyncMock()
    timeout = timedelta(seconds=4)
    await submit(client, session(worker_pool="governed-v1"), rpc_timeout=timeout)
    assert client.start_workflow.call_args.kwargs["rpc_timeout"] == timeout
    assert (
        client.start_workflow.call_args.kwargs["task_queue"] == "sessions-tenant-test-governed-v1"
    )
