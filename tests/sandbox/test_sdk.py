"""Public SDK qualification uses independently built agent images."""

import json
import os
import uuid
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Replayer

from zetra._runtime.temporal import SessionWorkflow
from zetra.client import SessionClient
from zetra.manifest import load_release

pytestmark = pytest.mark.cluster


@pytest.mark.parametrize("name,cursor", [("reporter", 5), ("reader", 3), ("generated", 5)])
async def test_public_sdk_release_session_and_repeated_submission(
    tmp_path, name, cursor, monkeypatch
):
    if not os.environ.get("TEMPORAL_ADDRESS"):
        pytest.skip("requires real Temporal/OpenShell and built SDK images")
    release_path = Path(".state") / f"{name}-release.json"
    assert release_path.is_file(), "build both SDK example images before qualification"
    release = load_release(release_path)
    client = SessionClient(state=tmp_path, address=os.environ["TEMPORAL_ADDRESS"])
    session = "sdk-" + uuid.uuid4().hex
    result = await client.run(release, session=session, message="Complete your task.")
    assert result["status"] == "SUCCEEDED", result
    assert result["cleanup"] == "DELETED"
    assert result["position"]["cursor"] == cursor
    assert await client.run(release, session=session, message="Complete your task.") == result
    assert (await client.status(session))["result"] == result
    files = await client.read_files(session)
    if name != "reader":
        assert files["summary.md"]
    record = json.loads((tmp_path / session / "session.json").read_text())
    assert (tmp_path / session / "session.json").stat().st_mode & 0o077 == 0
    temporal = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    history = await temporal.get_workflow_handle(session).fetch_history()
    assert record["key"] not in history.to_json()
    await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
    with monkeypatch.context() as changed:
        changed.setenv("OPENSHELL_ADDRESS", "127.0.0.1:1")
        with pytest.raises(ValueError, match="gateway"):
            await client.run(release, session=session, message="Complete your task.")
    # A second state directory cannot adopt the Workflow, even with the same release/input.
    other = SessionClient(state=tmp_path / "other", address=os.environ["TEMPORAL_ADDRESS"])
    with pytest.raises(ValueError, match="owner"):
        await other.run(release, session=session, message="Complete your task.")


async def test_public_sdk_live_model(tmp_path):
    if os.environ.get("BRIDGE_LIVE_VLLM") != "1":
        pytest.skip("requires opt-in live vLLM")
    release = load_release(".state/live-release.json")
    client = SessionClient(state=tmp_path, address=os.environ["TEMPORAL_ADDRESS"])
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
    import asyncio

    if not os.environ.get("TEMPORAL_ADDRESS"):
        pytest.skip("requires real Temporal/OpenShell and built SDK images")
    release = load_release(".state/blocked-release.json")
    client = SessionClient(state=tmp_path, address=os.environ["TEMPORAL_ADDRESS"])
    temporal = await Client.connect(os.environ["TEMPORAL_ADDRESS"])
    session = "sdk-terminal-" + uuid.uuid4().hex
    running = asyncio.create_task(client.run(release, session=session, message="Complete task"))
    handle = temporal.get_workflow_handle(session)
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
            await client.cancel(session)
        else:
            await handle.terminate(reason="SDK terminal cleanup qualification")
        result = await asyncio.wait_for(running, timeout=180)
        assert result["status"] in ("CANCELED", "CANCELLED", "TERMINATED"), result
        assert result["cleanup"] == "DELETED"
        assert result["effect_status"] == "OUTCOME_UNKNOWN"
        assert (await client.status(session))["result"] == result
        resumed = await client.run(release, session=session, message="Complete task")
        assert resumed == result
    finally:
        if not running.done():
            await handle.cancel()
            await asyncio.wait_for(running, timeout=180)


async def test_generated_agent_sandbox_evaluation(tmp_path):
    from zetra.evaluation import evaluate

    if not os.environ.get("TEMPORAL_ADDRESS"):
        pytest.skip("requires real Temporal/OpenShell and built SDK images")
    release = load_release(".state/generated-release.json")
    client = SessionClient(state=tmp_path, address=os.environ["TEMPORAL_ADDRESS"])
    evidence = await evaluate(
        ".state/generated-agent", mode="sandbox", release=release, client=client
    )
    assert evidence["status"] == "PASSED", evidence
    assert evidence["release"] == release.identity
    assert all(case["cleanup"] == "DELETED" for case in evidence["cases"])
