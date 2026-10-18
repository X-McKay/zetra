"""Public SDK qualification uses independently built agent images and ordinary Workers."""

import asyncio
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from openshell import SandboxClient, TlsConfig
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from zetra._runtime.activities import Activities
from zetra._runtime.artifacts import FileStore
from zetra._runtime.temporal import SessionWorkflow
from zetra.client import SessionClient
from zetra.manifest import load_release
from zetra.worker import create_worker

pytestmark = pytest.mark.cluster


@asynccontextmanager
async def sdk_runtime(tmp_path):
    if not os.environ.get("TEMPORAL_ADDRESS"):
        pytest.skip("requires real Temporal/OpenShell and built SDK images")
    temporal = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    tls = Path(os.environ["OPENSHELL_TLS_DIR"])
    gateway = SandboxClient(
        os.environ["OPENSHELL_ADDRESS"],
        tls=TlsConfig(ca_path=tls / "ca.crt", cert_path=tls / "tls.crt", key_path=tls / "tls.key"),
    )
    options = {
        "task_queue": "sdk-" + uuid.uuid4().hex,
        "runtime_id": "sdk-runtime-" + uuid.uuid4().hex,
        "artifacts": FileStore(tmp_path / "objects"),
    }
    key = secrets.token_bytes(32)
    client = SessionClient(temporal, **options)
    try:
        yield temporal, client, gateway, key, options
    finally:
        gateway.close()


def sdk_worker(temporal, gateway, key, options):
    worker = create_worker(temporal, sandbox_client=gateway, key=key, **options)
    assert isinstance(worker, Worker)
    return worker


@pytest.mark.parametrize("name,cursor", [("reporter", 5), ("reader", 3), ("generated", 5)])
async def test_public_sdk_release_session_and_repeated_submission(tmp_path, name, cursor):
    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        release_path = Path(".state") / f"{name}-release.json"
        assert release_path.is_file(), "build SDK example images before qualification"
        release = load_release(release_path)
        session = "sdk-" + uuid.uuid4().hex
        workspace = tmp_path / "initial"
        workspace.mkdir()
        message = "Complete your task."
        async with sdk_worker(temporal, gateway, key, options):
            handle = await client.submit(
                release, session=session, message=message, workspace=workspace
            )
            result = await client.wait(handle)
            assert result["status"] == "SUCCEEDED", result
            assert result["cleanup"] == "DELETED"
            assert result["position"]["cursor"] == cursor
            repeated = await client.submit(
                release, session=session, message=message, workspace=workspace
            )
            assert repeated == handle
            assert await client.wait(repeated) == result
            assert (await client.status(handle))["result"] == result
            files = await client.read_files(handle)
            if name != "reader":
                assert files["summary.md"]
            with pytest.raises(ValueError):
                await client.submit(release, session=session, message="Changed input")
            other = SessionClient(temporal, **{**options, "runtime_id": "different-runtime"})
            with pytest.raises(ValueError):
                await other.submit(release, session=session, message=message, workspace=workspace)
        history = await temporal.get_workflow_handle(
            handle.workflow_id, run_id=handle.run_id
        ).fetch_history()
        assert key.hex() not in history.to_json()
        assert not list(tmp_path.rglob("session.json"))
        await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)


async def test_public_sdk_in_flight_worker_replacement(tmp_path, monkeypatch):
    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        release = load_release(".state/reporter-release.json")
        parked = asyncio.Event()
        never = asyncio.Event()
        original_advance = Activities.advance_runner

        async def park_safe_advance(self, command):
            if command["position"]["cursor"] == 2:
                parked.set()
                await never.wait()
            return await original_advance(self, command)

        # Intercept a safe Activity before transport dispatch. No effect is in flight
        # when Worker shutdown cancels this attempt and a fresh Worker retries it.
        with monkeypatch.context() as patch:
            patch.setattr(Activities, "advance_runner", park_safe_advance)
            async with create_worker(
                temporal,
                sandbox_client=gateway,
                key=key,
                graceful_shutdown_timeout=timedelta(seconds=0),
                **options,
            ):
                handle = await client.submit(
                    release,
                    session="sdk-replace-" + uuid.uuid4().hex,
                    message="Complete your task.",
                )
                try:
                    await asyncio.wait_for(parked.wait(), timeout=180)
                except BaseException:
                    await client.cancel(handle)
                    raise
        # Reopen the durable store and construct a new router, adapters and client.
        replacement_options = {**options, "artifacts": FileStore(tmp_path / "objects")}
        replacement_client = SessionClient(temporal, **replacement_options)
        async with sdk_worker(temporal, gateway, key, replacement_options):
            result = await asyncio.wait_for(replacement_client.wait(handle), timeout=240)
        assert result["status"] == "SUCCEEDED", result
        assert result["cleanup"] == "DELETED"
        assert result["position"]["cursor"] == 5
        history = await temporal.get_workflow_handle(
            handle.workflow_id, run_id=handle.run_id
        ).fetch_history()
        scheduled = [
            event.activity_task_scheduled_event_attributes.activity_type.name
            for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
        ]
        assert scheduled.count("execute_model") == 3
        assert scheduled.count("execute_tool") == 2
        await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)


async def test_public_sdk_live_model(tmp_path):
    if os.environ.get("BRIDGE_LIVE_VLLM") != "1":
        pytest.skip("requires opt-in live vLLM")
    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        release = load_release(".state/live-release.json")
        async with sdk_worker(temporal, gateway, key, options):
            result = await client.run(
                release,
                session="sdk-live-" + uuid.uuid4().hex,
                message="Inspect the workspace, write a summary and confirm completion.",
            )
        assert result["status"] == "SUCCEEDED", result
        assert result["cleanup"] == "DELETED"
        assert result["position"]["cursor"] == 5
        assert result["result"]["final_text"]


@pytest.mark.parametrize("terminal", ["cancel", "terminate"])
async def test_public_sdk_terminal_cleanup(tmp_path, terminal):
    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        release = load_release(".state/blocked-release.json")
        session = "sdk-terminal-" + uuid.uuid4().hex
        async with sdk_worker(temporal, gateway, key, options):
            reference = await client.submit(release, session=session, message="Complete task")
            handle = temporal.get_workflow_handle(reference.workflow_id, run_id=reference.run_id)
            running = asyncio.create_task(client.wait(reference))
            try:
                async with asyncio.timeout(120):
                    while True:
                        if running.done():
                            pytest.fail(f"session ended before terminal injection: {await running}")
                        try:
                            progress = await handle.query("progress")
                        except Exception:  # noqa: BLE001 - Workflow may not yet exist
                            progress = None
                        if progress and (progress.get("pending") or {}).get("kind") == "tool":
                            break
                        await asyncio.sleep(0.05)
                if terminal == "cancel":
                    await client.cancel(reference)
                else:
                    await handle.terminate(reason="SDK terminal cleanup qualification")
                result = await asyncio.wait_for(running, timeout=180)
                assert result["status"] in ("CANCELED", "CANCELLED", "TERMINATED"), result
                assert result["cleanup"] == "DELETED"
                assert result["effect_status"] == "OUTCOME_UNKNOWN"
                assert (await client.status(reference))["result"] == result
                assert await client.run(release, session=session, message="Complete task") == result
            finally:
                if not running.done():
                    await handle.cancel()
                    await asyncio.wait_for(running, timeout=180)


async def test_generated_agent_sandbox_evaluation(tmp_path):
    from zetra.evaluation import evaluate

    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        release = load_release(".state/generated-release.json")
        async with sdk_worker(temporal, gateway, key, options):
            evidence = await evaluate(
                ".state/generated-agent", mode="sandbox", release=release, client=client
            )
        assert evidence["status"] == "PASSED", evidence
        assert evidence["release"] == release.identity
        assert all(case["cleanup"] == "DELETED" for case in evidence["cases"])


async def test_shared_worker_keeps_concurrent_session_workspaces_separate(tmp_path):
    async with sdk_runtime(tmp_path) as (temporal, client, gateway, key, options):
        release = load_release(".state/reporter-release.json")

        async def run(owner):
            workspace = tmp_path / owner
            workspace.mkdir()
            (workspace / f"{owner}.txt").write_text(owner)
            handle = await client.submit(
                release,
                session=f"sdk-{owner}-" + uuid.uuid4().hex,
                message="Complete your task.",
                workspace=workspace,
            )
            result = await client.wait(handle)
            assert result["status"] == "SUCCEEDED", result
            assert result["cleanup"] == "DELETED"
            return handle, await client.read_files(handle)

        async with sdk_worker(temporal, gateway, key, options):
            left, right = await asyncio.gather(run("left"), run("right"))
        assert left[0].run_id != right[0].run_id
        assert left[1]["left.txt"] == b"left" and "right.txt" not in left[1]
        assert right[1]["right.txt"] == b"right" and "left.txt" not in right[1]
