import asyncio
import os
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from openshell import SandboxClient, TlsConfig
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.worker import Replayer, Worker

# Fixture sandbox transport.
from tests.fixtures.sandbox import OpenShellSession
from zetra._runtime.activities import Activities
from zetra._runtime.artifacts import FileStore, Tape
from zetra._runtime.temporal import SessionWorkflow
from zetra._runtime.workspace import Workspace

pytestmark = pytest.mark.cluster


@pytest.mark.parametrize("replace", [False, True, "worker"])
async def test_temporal_owned_sandbox_and_cold_replacement(tmp_path, replace):
    if not os.environ.get("OPENSHELL_TLS_DIR") or not os.environ.get("TEMPORAL_ADDRESS"):
        pytest.skip("requires real OpenShell and Temporal in kind")
    session = "full-" + uuid.uuid4().hex
    store = FileStore(tmp_path / "objects")
    base = tmp_path / "base"
    base.mkdir()
    (base / "README.md").write_text("Synthetic input")
    if replace is True:
        (base / "large.txt").write_text("x" * 900000)
    tape = Tape(store, manifest="demo-v1")
    position = tape.empty(Workspace(base, store).capture())
    tls = Path(os.environ["OPENSHELL_TLS_DIR"])
    client = SandboxClient(
        os.environ.get("OPENSHELL_ADDRESS", "127.0.0.1:18080"),
        tls=TlsConfig(ca_path=tls / "ca.crt", cert_path=tls / "tls.crt", key_path=tls / "tls.key"),
    )
    key = secrets.token_bytes(32)
    remote = OpenShellSession(client, store, session=session, manifest="demo-v1", key=key)
    temporal = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    activities = Activities(remote, remote, tape)
    start = {"session": session, "message": "Inspect", "position": position.model_dump()}
    if replace:
        start["pause_at"] = 2
    try:
        async with Worker(
            temporal,
            task_queue=session,
            workflows=[SessionWorkflow],
            activities=activities.registered(),
        ):
            handle = await temporal.start_workflow(
                SessionWorkflow.run,
                start,
                id=session,
                task_queue=session,
                execution_timeout=timedelta(minutes=10),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            if replace:
                async with asyncio.timeout(180):
                    while True:
                        progress = await handle.query(SessionWorkflow.progress)
                        if progress["position"] and progress["position"]["cursor"] == 2:
                            break
                        await asyncio.sleep(0.5)
                old = remote.binding.copy()
                if replace is True:
                    assert (await remote.close({"binding": old}))["cleanup"] == "DELETED"
                if replace != "worker":
                    await handle.signal(SessionWorkflow.resume)
            if replace != "worker":
                result = await handle.result()
        if replace == "worker":
            # The sandbox remains parked while the application Worker stops.
            # Recreate adapter caches too; only stable credentials/artifacts survive.
            remote = OpenShellSession(client, store, session=session, manifest="demo-v1", key=key)
            activities = Activities(remote, remote, tape)
            assert remote.binding is None and not remote.uploaded
            async with Worker(
                temporal,
                task_queue=session,
                workflows=[SessionWorkflow],
                activities=activities.registered(),
            ):
                await handle.signal(SessionWorkflow.resume)
                result = await handle.result()
            assert remote.binding["generation"] == 1
            assert remote.binding["sandbox_id"] == old["sandbox_id"]
        assert result["status"] == "SUCCEEDED", result
        assert result["cleanup"] == "DELETED"
        assert result["position"]["cursor"] == 5
        restored = tmp_path / "restored"
        restored.mkdir()
        Workspace(restored, store).restore(
            __import__("zetra._runtime.artifacts", fromlist=["Ref"]).Ref.model_validate(
                result["position"]["workspace"]
            )
        )
        assert (restored / "report.md").read_text() == "Workspace inspected."
        if replace is True:
            assert (
                remote.binding["generation"] == 2
                and remote.binding["sandbox_id"] != old["sandbox_id"]
            )
        history = await handle.fetch_history()
        await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
        completed = [
            e for e in history.events if e.HasField("activity_task_completed_event_attributes")
        ]
        assert len(completed) == (14 if replace is True else 13)
        # The Workflow's committed position, not a sandbox cache, chose the five boundaries.
    finally:
        await remote.close({"binding": remote.binding})
        client.close()


@pytest.mark.parametrize("terminal", ["cancel", "terminate"])
async def test_terminal_cleanup_and_external_reconciliation(tmp_path, terminal):
    from temporalio.client import WorkflowFailureError

    if not os.environ.get("OPENSHELL_TLS_DIR") or not os.environ.get("TEMPORAL_ADDRESS"):
        pytest.skip("requires real OpenShell and Temporal in kind")
    session = "cleanup-" + uuid.uuid4().hex
    store = FileStore(tmp_path / "objects")
    base = tmp_path / "base"
    base.mkdir()
    tape = Tape(store, manifest="demo-v1")
    position = tape.empty(Workspace(base, store).capture())
    tls = Path(os.environ["OPENSHELL_TLS_DIR"])
    client = SandboxClient(
        os.environ.get("OPENSHELL_ADDRESS", "127.0.0.1:18080"),
        tls=TlsConfig(ca_path=tls / "ca.crt", cert_path=tls / "tls.crt", key_path=tls / "tls.key"),
    )
    remote = OpenShellSession(
        client, store, session=session, manifest="demo-v1", key=secrets.token_bytes(32)
    )
    temporal = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    activities = Activities(remote, remote, tape)
    try:
        async with Worker(
            temporal,
            task_queue=session,
            workflows=[SessionWorkflow],
            activities=activities.registered(),
        ):
            handle = await temporal.start_workflow(
                SessionWorkflow.run,
                {
                    "session": session,
                    "message": "Inspect",
                    "position": position.model_dump(),
                    "pause_at": 0,
                },
                id=session,
                task_queue=session,
                execution_timeout=timedelta(minutes=5),
            )
            async with asyncio.timeout(120):
                while True:
                    progress = await handle.query(SessionWorkflow.progress)
                    if progress["position"]:
                        break
                    await asyncio.sleep(0.5)
            # This adapter test inspects a pinned execution. Public reconciliation
            # is covered through the SDK's exact-run CleanupWorkflow tests.
            exact = temporal.get_workflow_handle(session, run_id=handle.first_execution_run_id)
            assert (await exact.describe()).status.name == "RUNNING"
            if terminal == "terminate":
                await handle.terminate("prototype cleanup injection")
            else:
                await handle.cancel()
            with pytest.raises(WorkflowFailureError):
                await handle.result()
            assert (await exact.describe()).status.name != "RUNNING"
            assert (await remote.close_all())["cleanup"] == "DELETED"
    finally:
        await remote.close({"binding": remote.binding})
        client.close()
