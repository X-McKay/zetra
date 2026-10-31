"""Real trusted-host children, independent of any Temporal or OpenShell service."""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from zetra._runtime.artifacts import FileStore, Position, Tape
from zetra._runtime.records import canonical_bytes
from zetra.manifest import load_agent, source_digest
from zetra.scaffold import scaffold


@pytest.fixture
def host(tmp_path):
    assert importlib.util.find_spec("zetra._runtime.local_host") is not None
    from zetra._runtime.local_host import HostSession

    project = tmp_path / "project"
    scaffold(project, "local_reader")
    settings = {
        "project_root": str(project),
        "source_sha256": source_digest(project),
        "agent": load_agent(project).model_dump(),
        "model_config": None,
    }
    (tmp_path / "objects").mkdir(mode=0o700)
    store = FileStore(tmp_path / "objects")
    initial = Position(
        workspace=store.put(canonical_bytes({"version": 1, "files": [], "directories": []}))
    )
    session = HostSession(
        tmp_path / "execution",
        store.root,
        settings=settings,
        session="exact-execution",
        message="Read the guide",
    )
    return session, project, initial


async def operation(host, position, binding=None):
    if binding is None:
        binding = await host.provision({"position": position.model_dump()})
    command = {"binding": binding, "position": position.model_dump(), "pending": None}
    advanced = await host.call("advance", command)
    return command, advanced


async def execute(host, command, op):
    return await host.call(
        "execute", {**command, "operation": op, "attempt_id": op["operation_id"] + ":attempt-1"}
    )


async def test_cold_children_use_real_core_and_shared_accepted_tape(host):
    session, _, position = host
    kinds = []
    binding = None
    while True:
        command, advanced = await operation(session, position, binding)
        binding = command["binding"]
        if advanced["status"] == "done":
            assert advanced["result"] == {"final_text": "Completed."}
            break
        kinds.append(advanced["operation"]["kind"])
        receipt = await execute(session, command, advanced["operation"])
        position = Position.model_validate(receipt["position"])
    assert kinds == ["model", "tool", "model"]
    records = Tape(FileStore(session.objects_root), manifest=session.manifest).records(position)
    assert records[1]["operation"]["executor"] == "read_document"
    assert json.loads((session.root / "inventory.json").read_text()) == {
        "read_document": "workspace_read"
    }
    assert await session.close({}) == {"cleanup": "DELETED"}


async def test_attempt_marker_prevents_redispatch_even_without_acceptance(host):
    session, _, position = host
    command, advanced = await operation(session, position)
    receipt = await execute(session, command, advanced["operation"])
    assert receipt["position"]["cursor"] == 1
    with pytest.raises(RuntimeError):
        await execute(session, command, advanced["operation"])
    assert position.cursor == 0


async def test_replacement_fences_queued_old_generation_and_is_idempotent(host):
    session, _, position = host
    first = await session.provision({"position": position.model_dump()})
    replacement = {"binding": first, "position": position.model_dump(), "start": {}}
    second = await session.replace(replacement)
    assert second["generation"] == 2
    assert await session.replace(replacement) == second
    with pytest.raises((RuntimeError, ValueError)):
        await operation(session, position, first)
    assert (await operation(session, position, second))[1]["status"] == "operation"


async def test_boolean_generation_cannot_match_integer_binding(host):
    session, _, position = host
    binding = await session.provision({"position": position.model_dump()})
    with pytest.raises((RuntimeError, ValueError)):
        await operation(session, position, {**binding, "generation": True})


async def test_terminal_cleanup_fences_late_start_before_project_import(host, tmp_path):
    session, project, position = host
    marker = tmp_path / "imported"
    factory = project / "src/local_reader/agents/document_reader/factory.py"
    assert factory.exists()
    factory.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n" + factory.read_text()
    )
    session.settings["source_sha256"] = source_digest(project)
    from zetra.manifest import digest

    session.manifest = digest(session.settings)
    assert await session.close({}) == {"cleanup": "DELETED"}
    with pytest.raises((RuntimeError, ValueError)):
        await session.provision({"position": position.model_dump()})
    with pytest.raises((RuntimeError, ValueError)):
        await operation(
            session,
            position,
            {"session": session.session, "generation": 1, "position": position.model_dump()},
        )
    assert not marker.exists()


async def test_changed_source_is_rejected_before_import(host, tmp_path):
    session, project, position = host
    marker = tmp_path / "imported"
    factory = project / "src/local_reader/agents/document_reader/factory.py"
    assert factory.exists()
    factory.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n" + factory.read_text()
    )
    with pytest.raises((RuntimeError, ValueError)):
        await operation(session, position)
    assert not marker.exists()


async def test_audit_guard_blocks_tool_subprocess_and_hides_exception_text(host, tmp_path):
    session, project, position = host
    marker = tmp_path / "escaped"
    tool = project / "src/local_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "import subprocess, sys\n"
        "async def read_document(document_id: str) -> str:\n"
        '    """Read a document."""\n'
        f"    subprocess.run([sys.executable, '-c', {('open(' + repr(str(marker)) + ', "w").write("private-secret")')!r}], check=True)\n"
        "    return 'done'\n"
    )
    session.settings["source_sha256"] = source_digest(project)
    # Settings are deliberately pinned again before any lifecycle state exists.
    from zetra.manifest import digest

    session.manifest = digest(session.settings)
    command, advanced = await operation(session, position)
    receipt = await execute(session, command, advanced["operation"])
    position = Position.model_validate(receipt["position"])
    command, advanced = await operation(session, position, command["binding"])
    assert advanced["operation"]["kind"] == "tool"
    with pytest.raises(RuntimeError) as caught:
        await execute(session, command, advanced["operation"])
    assert "private-secret" not in str(caught.value)
    assert not marker.exists()


async def test_cancellation_waits_for_child_stop_before_cleanup(host, tmp_path):
    session, project, position = host
    entered = tmp_path / "entered"
    tool = project / "src/local_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "import asyncio\nfrom pathlib import Path\n"
        "async def read_document(document_id: str) -> str:\n"
        '    """Read a document."""\n'
        f"    Path({str(entered)!r}).touch()\n"
        "    await asyncio.sleep(120)\n"
        "    return 'done'\n"
    )
    session.settings["source_sha256"] = source_digest(project)
    from zetra.manifest import digest

    session.manifest = digest(session.settings)
    command, advanced = await operation(session, position)
    receipt = await execute(session, command, advanced["operation"])
    position = Position.model_validate(receipt["position"])
    command, advanced = await operation(session, position, command["binding"])
    task = asyncio.create_task(execute(session, command, advanced["operation"]))
    async with asyncio.timeout(20):
        while not entered.exists():
            await asyncio.sleep(0.05)
    closing = asyncio.create_task(session.close({}))
    await asyncio.sleep(0.1)
    assert not closing.done(), "cleanup must wait for the active writer's lease"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.wait_for(closing, 5) == {"cleanup": "DELETED"}
    assert not (session.root / "work").exists()


async def test_cleanup_failure_retains_terminal_fence_and_lease_inode(host, monkeypatch):
    session, _, position = host
    await session.provision({"position": position.model_dump()})
    inode = (session.root / "lease").stat().st_ino
    work = session.root / "work"
    work.mkdir()
    (work / "abandoned").write_text("unconfirmed")

    def cannot_remove(path):
        raise OSError("filesystem failure")

    monkeypatch.setattr("zetra._runtime.local_host.shutil.rmtree", cannot_remove)
    assert await session.close({}) == {"cleanup": "NOT_CONFIRMED"}
    assert (session.root / "closed").is_file()
    assert (session.root / "lease").stat().st_ino == inode
    with pytest.raises(ValueError):
        await session.provision({"position": position.model_dump()})


async def test_source_mutation_by_effect_prevents_receipt_acceptance(host, tmp_path):
    session, project, position = host
    effect = tmp_path / "effect"
    tool = project / "src/local_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "from pathlib import Path\n"
        "async def read_document(document_id: str) -> str:\n"
        '    """Read a document."""\n'
        f"    Path({str(effect)!r}).write_text('effect happened')\n"
        "    Path(__file__).write_text('# changed inputs')\n"
        "    return 'done'\n"
    )
    session.settings["source_sha256"] = source_digest(project)
    from zetra.manifest import digest

    session.manifest = digest(session.settings)
    command, advanced = await operation(session, position)
    receipt = await execute(session, command, advanced["operation"])
    position = Position.model_validate(receipt["position"])
    command, advanced = await operation(session, position, command["binding"])
    with pytest.raises(RuntimeError):
        await execute(session, command, advanced["operation"])
    assert effect.read_text() == "effect happened"
    assert position.cursor == 1


async def test_changed_runtime_inventory_is_rejected_with_unchanged_source(host, monkeypatch):
    session, project, position = host
    factory = project / "src/local_reader/agents/document_reader/factory.py"
    factory.write_text(
        factory.read_text()
        + (
            "\nimport os\n"
            "if os.environ.get('ZETRA_TEST_OTHER_INVENTORY'):\n"
            "    read_document.__name__ = 'other_tool'\n"
        )
    )
    session.settings["source_sha256"] = source_digest(project)
    from zetra.manifest import digest

    session.manifest = digest(session.settings)
    command, _ = await operation(session, position)
    monkeypatch.setenv("ZETRA_TEST_OTHER_INVENTORY", "1")
    with pytest.raises(RuntimeError):
        await operation(session, position, command["binding"])


async def test_source_mutation_at_interpreter_exit_prevents_receipt_acceptance(host):
    session, project, position = host
    tool = project / "src/local_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "import atexit\nfrom pathlib import Path\n"
        "async def read_document(document_id: str) -> str:\n"
        '    """Read a document."""\n'
        "    atexit.register(lambda: Path(__file__).write_text('# exit mutation'))\n"
        "    return 'done'\n"
    )
    session.settings["source_sha256"] = source_digest(project)
    from zetra.manifest import digest

    session.manifest = digest(session.settings)
    command, advanced = await operation(session, position)
    receipt = await execute(session, command, advanced["operation"])
    position = Position.model_validate(receipt["position"])
    command, advanced = await operation(session, position, command["binding"])
    with pytest.raises((RuntimeError, ValueError)):
        await execute(session, command, advanced["operation"])
    assert tool.read_text() == "# exit mutation"


async def test_worker_death_kills_child_and_releases_admission_lease(host, tmp_path):
    session, project, position = host
    entered = tmp_path / "runner-pid"
    tool = project / "src/local_reader/agents/document_reader/tools/document_read.py"
    tool.write_text(
        "import asyncio, os\nfrom pathlib import Path\n"
        "async def read_document(document_id: str) -> str:\n"
        '    """Read a document."""\n'
        f"    Path({str(entered)!r}).write_text(str(os.getpid()))\n"
        "    await asyncio.sleep(120)\n"
        "    return 'done'\n"
    )
    session.settings["source_sha256"] = source_digest(project)
    from zetra.manifest import digest

    session.manifest = digest(session.settings)
    command, advanced = await operation(session, position)
    receipt = await execute(session, command, advanced["operation"])
    position = Position.model_validate(receipt["position"])
    command, advanced = await operation(session, position, command["binding"])
    request = {
        "root": str(session.root),
        "objects_root": str(session.objects_root),
        "settings": session.settings,
        "session": session.session,
        "message": session.message,
        "command": {**command, "operation": advanced["operation"], "attempt_id": "death-attempt"},
    }
    script = (
        "import asyncio, json, sys\n"
        "from zetra._runtime.local_host import HostSession\n"
        "request = json.load(sys.stdin)\n"
        "command = request.pop('command')\n"
        "host = HostSession(**request)\n"
        "asyncio.run(host.call('execute', command))\n"
    )
    parent = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        parent.stdin.write(json.dumps(request).encode())
        await parent.stdin.drain()
        parent.stdin.close()
        async with asyncio.timeout(20):
            while not entered.exists():
                assert parent.returncode is None
                await asyncio.sleep(0.05)
        runner_pid = int(entered.read_text())
        parent.kill()
        await parent.wait()
        async with asyncio.timeout(5):
            while True:
                try:
                    # A reparented zombie has exited and cannot execute code.
                    state = Path(f"/proc/{runner_pid}/stat").read_text().split(") ", 1)[1][0]
                    if state == "Z":
                        break
                    os.kill(runner_pid, 0)
                except FileNotFoundError:
                    break
                await asyncio.sleep(0.05)
        assert await asyncio.wait_for(session.close({}), 5) == {"cleanup": "DELETED"}
    finally:
        if parent.returncode is None:
            parent.kill()
        await parent.wait()
