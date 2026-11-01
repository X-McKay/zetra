import asyncio
import json
import os
import signal
import sys
import uuid
from collections import Counter
from pathlib import Path

import pytest
from aiohttp import web
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from zetra._runtime.temporal import SessionWorkflow
from zetra.scaffold import scaffold


@pytest.fixture
async def environment():
    binary = os.environ.get("ZETRA_TEMPORAL_TEST_BINARY")
    if not binary:
        pytest.skip("explicit local Temporal CLI binary required")
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=binary) as env:
        yield env


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    scaffold(path, "temporal_reader")
    return path


async def test_real_local_temporal_boundaries_and_history(environment, project, tmp_path):
    from zetra.local_temporal import LocalTemporal

    runtime = LocalTemporal(project, state_dir=tmp_path / "state")
    queue = "local-" + uuid.uuid4().hex
    async with runtime.worker(environment.client, task_queue=queue):
        handle = await runtime.submit(environment.client, "Read guide", task_queue=queue)
        result = await runtime.result(environment.client, handle)
    assert result["status"] == "SUCCEEDED", result
    assert result["cleanup"] == "DELETED"
    assert result["result"] == {"final_text": "Completed."}
    assert result["tools"] == {"read_document": "workspace_read"}
    assert [event["kind"] for event in result["trajectory"]] == ["model", "tool", "model"]
    assert result["metrics"]["steps"] == 3
    assert result["mode"] == "temporal-fixture"
    native = environment.client.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
    history = await native.fetch_history()
    counts = Counter(
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    )
    assert counts["execute_model"] == 2
    assert counts["execute_tool"] == 1
    await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)


async def test_temporal_mcp_fixture_binding_survives_cold_boundaries(environment, tmp_path):
    from tests.sdk.test_bindings import fixtures, mcp_project
    from zetra.local_temporal import LocalTemporal

    project = mcp_project(tmp_path / "agent")
    runtime = LocalTemporal(project, state_dir=tmp_path / "state", mcp_fixtures=fixtures())
    queue = "mcp-" + uuid.uuid4().hex
    async with runtime.worker(environment.client, task_queue=queue):
        handle = await runtime.submit(environment.client, "Read guide", task_queue=queue)
        result = await runtime.result(environment.client, handle)
    assert result["status"] == "SUCCEEDED", result
    assert result["cleanup"] == "DELETED"
    assert result["trajectory"][1]["result"] == {"text": "Approved guide."}
    assert result["mcp_binding"]["mode"] == "fixture"
    changed = fixtures()
    changed["calls"][0]["result"]["text"] = "Changed."
    with pytest.raises(ValueError, match="identity"):
        LocalTemporal(project, state_dir=tmp_path / "state", mcp_fixtures=changed)


def test_local_temporal_state_rejects_changed_source_and_unowned_directory(project, tmp_path):
    from zetra.local_temporal import LocalTemporal

    state = tmp_path / "state"
    LocalTemporal(project, state_dir=state)
    prompt = project / "src/temporal_reader/agents/document_reader/instructions/base.md"
    prompt.write_text("Changed prompt")
    with pytest.raises(ValueError, match="identity"):
        LocalTemporal(project, state_dir=state)
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep").write_text("existing")
    with pytest.raises(ValueError, match="owned"):
        LocalTemporal(project, state_dir=other)
    assert (other / "keep").read_text() == "existing"


async def test_tool_exception_is_unknown_and_never_repeated(environment, project, tmp_path):
    from zetra.local_temporal import LocalTemporal

    counter = tmp_path / "attempts.txt"
    tool = project / "src/temporal_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "from pathlib import Path\n"
        "async def read_document(document_id: str) -> str:\n"
        '    """Read guide."""\n'
        f"    with Path({str(counter)!r}).open('a') as f: f.write('attempt\\n')\n"
        "    raise RuntimeError('private-tool-secret')\n"
    )
    runtime = LocalTemporal(project, state_dir=tmp_path / "state")
    queue = "local-" + uuid.uuid4().hex
    async with runtime.worker(environment.client, task_queue=queue):
        handle = await runtime.submit(environment.client, "Read", task_queue=queue)
        result = await runtime.result(environment.client, handle)
    assert result["status"] == "OUTCOME_UNKNOWN", result
    assert result["cleanup"] == "DELETED"
    assert counter.read_text() == "attempt\n"
    assert result["metrics"]["steps"] == 1
    assert "private-tool-secret" not in json.dumps(result)


async def test_fresh_worker_reconstructs_accepted_prefix(environment, project, tmp_path):
    from zetra.local_temporal import LocalTemporal

    runtime = LocalTemporal(project, state_dir=tmp_path / "state")
    queue = "local-" + uuid.uuid4().hex
    async with runtime.worker(environment.client, task_queue=queue):
        handle = await runtime.submit(environment.client, "Read", task_queue=queue, pause_at=2)
        native = environment.client.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
        async with asyncio.timeout(20):
            while ((await native.query("progress"))["position"] or {}).get("cursor", 0) != 2:
                await asyncio.sleep(0.05)
    replacement = LocalTemporal(project, state_dir=tmp_path / "state")
    async with replacement.worker(environment.client, task_queue=queue):
        await native.signal("resume")
        result = await replacement.result(environment.client, handle)
    assert result["status"] == "SUCCEEDED", result
    history = await native.fetch_history()
    tools = [
        event
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
        and event.activity_task_scheduled_event_attributes.activity_type.name == "execute_tool"
    ]
    assert len(tools) == 1
    await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)


async def test_lost_candidate_completion_does_not_advance_or_repeat(
    environment, project, tmp_path, monkeypatch
):
    from zetra._runtime.local_host import HostSession
    from zetra.local_temporal import LocalTemporal

    original = HostSession.call
    attempts = []

    async def lose_response(self, action, command):
        response = await original(self, action, command)
        if action == "execute" and command["operation"]["kind"] == "tool":
            attempts.append(response)
            raise RuntimeError("candidate completion lost")
        return response

    monkeypatch.setattr(HostSession, "call", lose_response)
    runtime = LocalTemporal(project, state_dir=tmp_path / "state")
    queue = "loss-" + uuid.uuid4().hex
    async with runtime.worker(environment.client, task_queue=queue):
        handle = await runtime.submit(environment.client, "Read", task_queue=queue)
        result = await runtime.result(environment.client, handle)
    assert len(attempts) == 1
    assert attempts[0]["position"]["cursor"] == 2
    assert result["status"] == "OUTCOME_UNKNOWN"
    assert result["position"]["cursor"] == 1
    assert result["trajectory"][-1]["status"] == "outcome_unknown"
    assert result["cleanup"] == "DELETED"


async def test_native_cancellation_stops_child_and_confirms_cleanup(environment, project, tmp_path):
    from zetra.local_temporal import LocalTemporal

    ready = tmp_path / "child-pid"
    tool = project / "src/temporal_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "import asyncio, os\nfrom pathlib import Path\n"
        "async def read_document(document_id: str) -> str:\n"
        '    """Read guide."""\n'
        f"    Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "    await asyncio.sleep(60)\n    return 'late result'\n"
    )
    runtime = LocalTemporal(project, state_dir=tmp_path / "state")
    queue = "cancel-" + uuid.uuid4().hex
    async with runtime.worker(environment.client, task_queue=queue):
        handle = await runtime.submit(environment.client, "Read", task_queue=queue)
        async with asyncio.timeout(20):
            while not ready.exists():
                await asyncio.sleep(0.05)
        native = environment.client.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
        await native.cancel()
        async with asyncio.timeout(15):
            result = await runtime.result(environment.client, handle)
    assert result["status"] == "CANCELED", result
    assert result["cleanup"] == "DELETED"
    assert result["position"]["cursor"] == 1
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()), 0)


async def test_persistent_server_restart_keeps_accepted_tool_result(project, tmp_path):
    from zetra.local_temporal import LocalTemporal

    binary = os.environ.get("ZETRA_TEMPORAL_TEST_BINARY")
    if not binary:
        pytest.skip("explicit local Temporal CLI binary required")
    count = tmp_path / "calls"
    tool = project / "src/temporal_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "from pathlib import Path\nasync def read_document(document_id: str) -> str:\n"
        '    """Read guide."""\n'
        f"    with Path({str(count)!r}).open('a') as stream: stream.write('call\\n')\n"
        "    return 'Approved guide.'\n"
    )
    database = str(tmp_path / "history.sqlite")
    queue = "restart-" + uuid.uuid4().hex
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_database_filename=database
    ) as env:
        runtime = LocalTemporal(project, state_dir=tmp_path / "state")
        async with runtime.worker(env.client, task_queue=queue):
            handle = await runtime.submit(env.client, "Read", task_queue=queue, pause_at=2)
            native = env.client.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
            async with asyncio.timeout(20):
                while ((await native.query("progress"))["position"] or {}).get("cursor", 0) != 2:
                    await asyncio.sleep(0.05)
    assert count.read_text() == "call\n"

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_database_filename=database
    ) as env:
        restored = LocalTemporal(project, state_dir=tmp_path / "state")
        async with restored.worker(env.client, task_queue=queue):
            native = env.client.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
            await native.signal("resume")
            result = await restored.result(env.client, handle)
        await Replayer(workflows=[SessionWorkflow]).replay_workflow(await native.fetch_history())
    assert result["status"] == "SUCCEEDED", result
    assert count.read_text() == "call\n"


async def test_cli_temporal_mode_runs_without_cluster(project, tmp_path):
    binary = os.environ.get("ZETRA_TEMPORAL_TEST_BINARY")
    if not binary:
        pytest.skip("explicit local Temporal CLI binary required")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "zetra",
        "dev",
        str(project),
        "--input",
        "Read guide",
        "--temporal",
        "--temporal-binary",
        binary,
        "--temporal-state",
        str(tmp_path / "cli-state"),
        "--output",
        str(tmp_path / "export"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
    assert process.returncode == 0, stderr.decode()
    report = json.loads(stdout)
    assert report["status"] == "SUCCEEDED"
    assert report["mode"] == "temporal-fixture"
    assert report["cleanup"] == "DELETED"
    assert (tmp_path / "cli-state/last-handle.json").is_file()
    assert json.loads((tmp_path / "export/report.json").read_text()) == report


def test_temporal_cli_options_cannot_be_silently_ignored(project, capsys):
    from zetra.cli import main

    assert main(["dev", str(project), "--input", "Read", "--temporal-state", "/tmp/unused"]) == 1
    assert "Temporal mode" in capsys.readouterr().err


@pytest.mark.parametrize(
    "address",
    [
        "example.com:7233",
        "user@localhost:7233",
        "localhost",
        "localhost:7233/path",
        "localhost:7233?key=secret",
    ],
)
def test_cli_temporal_connection_is_explicit_loopback_only(address):
    from zetra.local_temporal import loopback_address

    with pytest.raises(ValueError):
        loopback_address(address)


async def test_temporal_real_http_model_and_credentials_stay_out_of_history(
    environment, project, tmp_path, monkeypatch
):
    from zetra.local_temporal import LocalTemporal

    calls = []
    monkeypatch.setenv("TEMPORAL_MODEL_TEST_KEY", "private-provider-credential")

    async def handle(request):
        body = await request.json()
        calls.append(body)
        assert request.headers["Authorization"] == "Bearer private-provider-credential"
        message = (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read-1",
                        "type": "function",
                        "function": {
                            "name": "read_document",
                            "arguments": '{"document_id":"guide"}',
                        },
                    }
                ],
            }
            if len(calls) == 1
            else {"role": "assistant", "content": "Read the approved guide."}
        )
        return web.json_response(
            {
                "id": "completion",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(calls) == 1 else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        runtime = LocalTemporal(
            project,
            state_dir=tmp_path / "state",
            model_config={
                "base_url": f"http://127.0.0.1:{port}/v1",
                "model": "test-model",
                "api_key_env": "TEMPORAL_MODEL_TEST_KEY",
            },
        )
        queue = "model-" + uuid.uuid4().hex
        async with runtime.worker(environment.client, task_queue=queue):
            execution = await runtime.submit(environment.client, "Read", task_queue=queue)
            result = await runtime.result(environment.client, execution)
        assert result["status"] == "SUCCEEDED", result
        assert result["mode"] == "temporal-local"
        assert result["metrics"]["input_tokens"] == 20
        assert result["metrics"]["output_tokens"] == 10
        assert any(
            m["role"] == "tool" and "verify metadata" in m["content"] for m in calls[-1]["messages"]
        )
        history = await environment.client.get_workflow_handle(
            execution.workflow_id, run_id=execution.run_id
        ).fetch_history()
        assert b"private-provider-credential" not in b"".join(
            e.SerializeToString() for e in history.events
        )
        assert "private-provider-credential" not in json.dumps(result)
    finally:
        await server.cleanup()


async def test_worker_sigkill_preserves_unknown_tool_and_stops_direct_runner(
    environment, project, tmp_path
):
    from zetra.local_temporal import LocalTemporal

    ready, runner, calls = tmp_path / "ready", tmp_path / "runner", tmp_path / "calls"
    tool = project / "src/temporal_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "import asyncio, os\nfrom pathlib import Path\n"
        "async def read_document(document_id: str) -> str:\n"
        '    """Read guide."""\n'
        f"    with Path({str(calls)!r}).open('a') as f: f.write('attempt\\n')\n"
        f"    Path({str(runner)!r}).write_text(str(os.getpid()))\n"
        "    await asyncio.sleep(60)\n    return 'late result'\n"
    )
    state = tmp_path / "state"
    runtime = LocalTemporal(project, state_dir=state)
    queue = "kill-" + uuid.uuid4().hex
    address = environment.client.service_client.config.target_host
    script = f"""import asyncio
from pathlib import Path
from temporalio.client import Client
from zetra.local_temporal import LocalTemporal
async def main():
    runtime = LocalTemporal({str(project)!r}, state_dir={str(state)!r})
    client = await Client.connect({address!r})
    async with runtime.worker(client, task_queue={queue!r}):
        Path({str(ready)!r}).write_text('ready')
        await asyncio.Event().wait()
asyncio.run(main())
"""
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(20):
            while not ready.exists():
                assert worker.returncode is None
                await asyncio.sleep(0.05)
        execution = await runtime.submit(environment.client, "Read", task_queue=queue)
        async with asyncio.timeout(20):
            while not runner.exists():
                await asyncio.sleep(0.05)
        worker.kill()
        await worker.wait()
        replacement = LocalTemporal(project, state_dir=state)
        async with replacement.worker(environment.client, task_queue=queue):
            async with asyncio.timeout(30):
                result = await replacement.result(environment.client, execution)
        assert result["status"] == "OUTCOME_UNKNOWN", result
        assert result["position"]["cursor"] == 1
        assert result["cleanup"] == "DELETED"
        assert calls.read_text() == "attempt\n"
        # A reparented dead child can briefly be a zombie; it is no longer a writer.
        proc = Path(f"/proc/{int(runner.read_text())}/stat")
        assert not proc.exists() or proc.read_text().split(") ", 1)[1].startswith("Z ")
    finally:
        if worker.returncode is None:
            worker.kill()
            await worker.wait()


async def test_source_mutation_cannot_downgrade_unknown_effect_to_failed(
    environment, project, tmp_path
):
    from zetra.local_temporal import LocalTemporal

    effect = tmp_path / "effect"
    tool = project / "src/temporal_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "from pathlib import Path\nasync def read_document(document_id: str) -> str:\n"
        '    """Read guide."""\n'
        f"    Path({str(effect)!r}).write_text('performed')\n"
        "    source = Path(__file__)\n    source.write_text(source.read_text() + '\\n# changed')\n"
        "    return 'done'\n"
    )
    runtime = LocalTemporal(project, state_dir=tmp_path / "state")
    queue = "mutation-" + uuid.uuid4().hex
    async with runtime.worker(environment.client, task_queue=queue):
        execution = await runtime.submit(environment.client, "Read", task_queue=queue)
        report = await runtime.result(environment.client, execution)
    assert effect.read_text() == "performed"
    assert report["status"] == "OUTCOME_UNKNOWN", report
    assert report["error_type"] == "SourceChanged"
    assert report["position"]["cursor"] == 1
    assert report["cleanup"] == "DELETED"


def _process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except FileNotFoundError:
        return None


async def test_cli_sigterm_cancels_workflow_and_stops_owned_server(project, tmp_path):
    binary = os.environ.get("ZETRA_TEMPORAL_TEST_BINARY")
    if not binary:
        pytest.skip("explicit local Temporal CLI binary required")
    ready = tmp_path / "ready"
    state = tmp_path / "state"
    tool = project / "src/temporal_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "import asyncio\nfrom pathlib import Path\nasync def read_document(document_id: str) -> str:\n"
        '    """Read guide."""\n'
        f"    Path({str(ready)!r}).write_text('ready')\n"
        "    await asyncio.sleep(60)\n    return 'late'\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "zetra",
        "dev",
        str(project),
        "--input",
        "Read",
        "--temporal",
        "--temporal-binary",
        binary,
        "--temporal-state",
        str(state),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    children = {}
    try:
        async with asyncio.timeout(25):
            while not ready.exists():
                assert process.returncode is None
                await asyncio.sleep(0.05)
        for path in Path(f"/proc/{process.pid}/task").glob("*/children"):
            for child in path.read_text().split():
                pid = int(child)
                if identity := _process_identity(pid):
                    children[pid] = identity
        assert children
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.wait(), 20)
        assert process.returncode == 130
        report = json.loads((state / "last-report.json").read_text())
        assert report["status"] == "CANCELED"
        assert report["cleanup"] == "DELETED"
        assert all(_process_identity(pid) != identity for pid, identity in children.items())
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        # Clean only this test's still-identical children on the expected RED run.
        for pid, identity in children.items():
            if _process_identity(pid) == identity:
                os.kill(pid, signal.SIGTERM)


async def test_startup_cancellation_resolves_and_stops_owned_server(project, tmp_path, monkeypatch):
    from zetra.local_temporal import dev_temporal

    binary = os.environ.get("ZETRA_TEMPORAL_TEST_BINARY")
    if not binary:
        pytest.skip("explicit local Temporal CLI binary required")
    original = WorkflowEnvironment.start_local
    created, release = asyncio.Event(), asyncio.Event()
    environments = []

    async def delayed_start(**kwargs):
        environment = await original(**kwargs)
        environments.append(environment)
        created.set()
        await release.wait()
        return environment

    monkeypatch.setattr(WorkflowEnvironment, "start_local", delayed_start)
    task = asyncio.create_task(
        dev_temporal(project, "Read", binary=binary, state_dir=tmp_path / "state")
    )
    try:
        await asyncio.wait_for(created.wait(), 15)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)
        address = environments[0].client.service_client.config.target_host
        host, port = address.rsplit(":", 1)
        with pytest.raises(OSError):
            await asyncio.wait_for(asyncio.open_connection(host, int(port)), 1)
        assert not (tmp_path / "state/last-handle.json").exists()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for environment in environments:
            await environment.shutdown()
