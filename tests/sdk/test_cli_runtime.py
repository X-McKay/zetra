"""CLI process and export contracts, using caller-owned SDK connections."""

import json
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from temporalio.client import Client

from zetra import cli
from zetra.client import SessionClient
from zetra.session import SessionHandle


@pytest.fixture
def session_client(monkeypatch):
    handle = SessionHandle(
        namespace="default", workflow_id="one", run_id="exact", runtime_id="local"
    )
    monkeypatch.setattr(Client, "connect", AsyncMock(return_value=object()))
    monkeypatch.setattr(SessionClient, "submit", AsyncMock(return_value=handle))
    monkeypatch.setattr(
        SessionClient,
        "wait",
        AsyncMock(
            return_value={
                "status": "SUCCEEDED",
                "cleanup": "DELETED",
                "result": {
                    "final_text": "Done",
                    "session": {"private_session": "not console output"},
                },
            }
        ),
    )
    monkeypatch.setattr(
        SessionClient,
        "read_files",
        AsyncMock(return_value={"report.md": b"Report", "handle.json": b"agent file"}),
    )
    return handle


def run_args(output):
    return cli.parser().parse_args(
        ["run", "release.json", "--session", "one", "--input", "task", "--output", str(output)]
    )


async def test_run_publishes_verified_workspace_and_compact_summary(tmp_path, session_client):
    result = await cli.execute(run_args(tmp_path / "out"), lambda _: object())
    assert result["result"] == {
        "status": "SUCCEEDED",
        "cleanup": "DELETED",
        "cursor": None,
        "final_text": "Done",
    }
    assert json.loads((tmp_path / "out/handle.json").read_text()) == session_client.model_dump()
    assert "private_session" in (tmp_path / "out/result.json").read_text()
    assert (tmp_path / "out/workspace/report.md").read_bytes() == b"Report"
    assert (tmp_path / "out/workspace/handle.json").read_bytes() == b"agent file"


async def test_interruption_persists_handle_before_wait(tmp_path, session_client, monkeypatch):
    monkeypatch.setattr(SessionClient, "wait", AsyncMock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        await cli.execute(run_args(tmp_path / "out"), lambda _: object())
    assert json.loads((tmp_path / "out/handle.json").read_text()) == session_client.model_dump()
    assert not (tmp_path / "out/result.json").exists()


async def test_repeat_export_removes_stale_files_and_replaces_corrupt_receipt(
    tmp_path, session_client, monkeypatch
):
    args = run_args(tmp_path / "out")
    await cli.execute(args, lambda _: object())
    (tmp_path / "out/workspace/stale.txt").write_text("old")
    (tmp_path / "out/handle.json").write_text('{"partial":')
    monkeypatch.setattr(SessionClient, "read_files", AsyncMock(return_value={"new.txt": b"new"}))
    await cli.execute(args, lambda _: object())
    assert sorted(p.name for p in (tmp_path / "out/workspace").iterdir()) == ["new.txt"]
    assert json.loads((tmp_path / "out/handle.json").read_text()) == session_client.model_dump()


async def test_export_failure_does_not_publish_success_marker(
    tmp_path, session_client, monkeypatch
):
    args = run_args(tmp_path / "out")
    await cli.execute(args, lambda _: object())
    monkeypatch.setattr(
        SessionClient, "read_files", AsyncMock(side_effect=OSError("storage failed"))
    )
    with pytest.raises(OSError):
        await cli.execute(args, lambda _: object())
    assert not (tmp_path / "out/result.json").exists()
    assert not (tmp_path / "out/workspace").exists()


@pytest.mark.parametrize(
    "status,cleanup", [("OUTCOME_UNKNOWN", "DELETED"), ("SUCCEEDED", "NOT_CONFIRMED")]
)
async def test_unsuccessful_run_exports_no_workspace(
    tmp_path, session_client, monkeypatch, status, cleanup
):
    monkeypatch.setattr(
        SessionClient, "wait", AsyncMock(return_value={"status": status, "cleanup": cleanup})
    )
    args = run_args(tmp_path / "out")
    result = await cli.execute(args, lambda _: object())
    assert not (tmp_path / "out/workspace").exists()
    assert json.loads((tmp_path / "out/result.json").read_text())["cleanup"] == cleanup
    assert result["result"]["status"] != "SUCCEEDED"


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT"])
def test_worker_cli_drains_on_signal(signal_name):
    code = """import asyncio,os,signal
from zetra.cli import _run_worker
class Worker:
 def __init__(self):self.finished=asyncio.Event();self.drained=False
 async def run(self):
  asyncio.get_running_loop().call_later(.05,os.kill,os.getpid(),SIGNAL)
  await self.finished.wait()
  assert self.drained
 async def shutdown(self):
  await asyncio.sleep(.05);self.drained=True;self.finished.set()
asyncio.run(_run_worker(Worker()))
print('drained')
""".replace("SIGNAL", "signal." + signal_name)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "drained" in result.stdout


@pytest.mark.parametrize("target", ["output", "workspace"])
async def test_export_rejects_symlink_destinations(tmp_path, session_client, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    output = tmp_path / "output"
    if target == "output":
        output.symlink_to(outside, target_is_directory=True)
    else:
        output.mkdir()
        (output / "workspace").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        await cli.execute(run_args(output), lambda _: object())
    assert (outside / "keep").read_text() == "safe"
    assert not (outside / "handle.json").exists()


async def test_rejected_submission_leaves_existing_export_intact(
    tmp_path, session_client, monkeypatch
):
    args = run_args(tmp_path / "out")
    await cli.execute(args, lambda _: object())
    monkeypatch.setattr(
        SessionClient, "submit", AsyncMock(side_effect=ValueError("request mismatch"))
    )
    with pytest.raises(ValueError):
        await cli.execute(args, lambda _: object())
    assert (tmp_path / "out/workspace/report.md").read_bytes() == b"Report"
    assert json.loads((tmp_path / "out/result.json").read_text())["status"] == "SUCCEEDED"


async def test_worker_cli_forwards_release_approval_and_native_provider(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from zetra import worker as worker_module

    key = tmp_path / "key"
    key.write_bytes(b"x" * 32)

    class Gateway:
        closed = False

        def close(self):
            self.closed = True

    gateway = Gateway()
    monkeypatch.setattr(cli, "open_gateway", lambda: gateway)
    monkeypatch.setattr(Client, "connect", AsyncMock(return_value=object()))

    def factory(temporal, **options):
        assert options["approved_releases"] == {"release-one", "release-two"}
        assert options["providers"] == ["kubani-k8s-read"]
        assert options["key"] == b"x" * 32
        assert options["graceful_shutdown_timeout"] == timedelta(seconds=12)
        return object()

    monkeypatch.setattr(worker_module, "create_worker", factory)
    monkeypatch.setattr(cli, "_run_worker", AsyncMock())
    args = cli.parser().parse_args(
        [
            "worker",
            "--key-file",
            str(key),
            "--release",
            "one",
            "--release",
            "two",
            "--provider",
            "kubani-k8s-read",
            "--graceful-shutdown-seconds",
            "12",
        ]
    )
    assert await cli.execute(args, lambda name: SimpleNamespace(identity="release-" + name)) == {
        "status": "STOPPED"
    }
    assert gateway.closed


def test_cli_output_directory_publishes_summary_and_nonzero_failure(
    tmp_path, session_client, monkeypatch, capsys
):
    from zetra.manifest import AgentSpec, Release

    release_path = tmp_path / "release.json"
    release_path.write_text(
        Release(
            lock_sha256="a" * 64,
            sdk_revision="b" * 40,
            agent=AgentSpec(name="test", owner="test", entrypoint="test:create"),
            image="test@sha256:" + "a" * 64,
            source_sha256="b" * 64,
            policy_sha256="c" * 64,
        ).model_dump_json()
    )
    for state, cleanup, expected in [
        ("SUCCEEDED", "DELETED", 0),
        ("OUTCOME_UNKNOWN", "DELETED", 1),
        ("SUCCEEDED", "NOT_CONFIRMED", 1),
    ]:
        monkeypatch.setattr(
            SessionClient, "wait", AsyncMock(return_value={"status": state, "cleanup": cleanup})
        )
        assert (
            cli.main(
                [
                    "run",
                    str(release_path),
                    "--session",
                    "one",
                    "--input",
                    "task",
                    "--output",
                    str(tmp_path / "out"),
                ]
            )
            == expected
        )
        summary = json.loads(capsys.readouterr().out)
        assert summary["result"]["cleanup"] == cleanup
        assert (tmp_path / "out/result.json").is_file()


async def test_export_does_not_erase_unrelated_directory(tmp_path, session_client):
    output = tmp_path / "out"
    (output / "workspace").mkdir(parents=True)
    (output / "workspace/user-data.txt").write_text("keep")
    with pytest.raises(ValueError, match="export"):
        await cli.execute(run_args(output), lambda _: object())
    assert (output / "workspace/user-data.txt").read_text() == "keep"


@pytest.mark.parametrize("source", ["artifacts", "workspace"])
async def test_output_cannot_overlap_inputs_or_artifact_backing(tmp_path, session_client, source):
    output = tmp_path / "out"
    backing = output / "workspace"
    backing.mkdir(parents=True)
    (backing / "keep").write_text("safe")
    args = run_args(output)
    if source == "artifacts":
        args.artifacts = str(backing)
    else:
        args.workspace = str(backing)
    with pytest.raises(ValueError):
        await cli.execute(args, lambda _: object())
    assert (backing / "keep").read_text() == "safe"
