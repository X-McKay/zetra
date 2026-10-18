from unittest.mock import AsyncMock

import pytest
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from zetra.client import SessionClient
from zetra.manifest import AgentSpec, Release


def release():
    return Release(
        agent=AgentSpec(name="test", owner="test", entrypoint="test:create"),
        image="test@sha256:" + "a" * 64,
        source_sha256="b" * 64,
        policy_sha256="c" * 64,
    )


async def test_duplicate_submit_compares_exact_original_start(tmp_path, monkeypatch):
    from zetra._runtime.artifacts import FileStore

    temporal = AsyncMock()
    temporal.namespace = "default"
    temporal.start_workflow.return_value.first_execution_run_id = "first"
    client = SessionClient(
        temporal, task_queue="queue", runtime_id="runtime", artifacts=FileStore(tmp_path)
    )
    handle = await client.submit(release(), session="one", message="task")
    expected = temporal.start_workflow.call_args.args[1]
    assert (
        temporal.start_workflow.call_args.kwargs["id_reuse_policy"]
        == WorkflowIDReusePolicy.REJECT_DUPLICATE
    )
    temporal.start_workflow.side_effect = WorkflowAlreadyStartedError(
        "one", "SessionWorkflow", run_id="first"
    )
    load = AsyncMock(return_value=expected)
    monkeypatch.setattr("zetra.client.load_execution", load)
    assert await client.submit(release(), session="one", message="task") == handle
    assert load.call_args.args[1].run_id == "first"
    with pytest.raises(ValueError, match="request"):
        await client.submit(release(), session="one", message="different")
    assert not list(tmp_path.rglob("session.json"))
    assert not list(tmp_path.rglob("result.json"))
