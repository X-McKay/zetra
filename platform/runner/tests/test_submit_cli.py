import json
from types import SimpleNamespace

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from runner.submit import main, submit
from tests.test_operator import RUN, Client, inp, receipt


def arguments():
    return [
        "--tenant",
        "tenant-a",
        "--agent",
        "reader",
        "--release",
        "reader-r1",
        "--session-id",
        inp().session_id,
        "--task",
        '{"request":"read"}',
    ]


async def test_duplicate_without_exact_run_fails():
    client = Client()

    async def start(*a, **kw):
        raise WorkflowAlreadyStartedError(inp().workflow_id, "SessionWorkflow")

    client.start_workflow = start
    with pytest.raises(ValueError):
        await submit(client, inp())


async def test_duplicate_pinned_and_compares_full_input():
    client = Client()

    async def start(*a, **kw):
        raise WorkflowAlreadyStartedError(inp().workflow_id, "SessionWorkflow", run_id=RUN)

    client.start_workflow = start
    assert await submit(client, inp()) is client
    with pytest.raises(ValueError):
        await submit(client, inp(task="changed"))


def test_default_output_and_receipt_before_inspection_failure(monkeypatch, capsys, tmp_path):
    import runner.worker

    client = Client()

    async def connect(tenant):
        return client

    async def start(*a, **kw):
        return SimpleNamespace(id=inp().workflow_id, first_execution_run_id=RUN)

    async def fail(**kw):
        raise RuntimeError("secret")

    client.start_workflow = start
    client.describe = fail
    monkeypatch.setattr(runner.worker, "connect", connect)
    destination = tmp_path / "receipt"
    assert main(arguments() + ["--receipt", str(destination)]) == 0
    output = capsys.readouterr()
    assert output.out.strip() == inp().workflow_id
    assert json.loads(output.err)["run_id"] == RUN
    assert destination.stat().st_mode & 0o777 == 0o600
    assert main(arguments() + ["--wait"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.err)["run_id"] == RUN and "secret" not in output.out


@pytest.mark.parametrize("flag", ["--receipt", "--report"])
def test_dangling_destination_rejected_before_connect(flag, tmp_path, monkeypatch):
    import runner.worker

    async def forbidden(*a):
        pytest.fail("must reject before connecting")

    monkeypatch.setattr(runner.worker, "connect", forbidden)
    dest = tmp_path / "link"
    dest.symlink_to(tmp_path / "missing")
    extra = ["--wait", "--artifact-root", str(tmp_path)] if flag == "--report" else []
    assert main(arguments() + extra + [flag, str(dest)]) == 2


def test_resume_never_submits(monkeypatch, tmp_path, capsys):
    import runner.worker
    from dataclasses import asdict

    client = Client()

    async def connect(tenant):
        return client

    monkeypatch.setattr(runner.worker, "connect", connect)
    path = tmp_path / "receipt"
    path.write_text(json.dumps(asdict(receipt())))
    assert main(["--resume", str(path), "--wait"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["cleanup"] == "DELETED"


@pytest.mark.parametrize(
    "extra",
    [
        ["--wait-timeout", "nan"],
        ["--rpc-timeout", "0"],
        ["--report", "out"],
        ["--resume", "missing", "--wait", "--tenant", "tenant-a"],
    ],
)
def test_invalid_options(extra):
    assert main(arguments() + extra) == 2


def test_connection_is_bounded_by_rpc_timeout(monkeypatch, capsys):
    import asyncio
    import runner.worker

    async def connect(tenant):
        await asyncio.sleep(0.1)
        pytest.fail("connection exceeded rpc limit")

    monkeypatch.setattr(runner.worker, "connect", connect)
    assert main(arguments() + ["--wait", "--rpc-timeout", ".001"]) == 124
    assert "NOT_CONFIRMED" in capsys.readouterr().out


def test_ambiguous_start_not_retried(monkeypatch, capsys):
    import runner.worker

    client = Client()
    calls = []

    async def connect(tenant):
        return client

    async def start(*args, **kw):
        calls.append(1)
        raise TimeoutError

    client.start_workflow = start
    monkeypatch.setattr(runner.worker, "connect", connect)
    assert main(arguments()) == 124
    assert calls == [1]


def test_missing_accepted_run_is_failure(monkeypatch, capsys):
    import runner.worker

    client = Client()

    async def connect(tenant):
        return client

    async def start(*args, **kw):
        return SimpleNamespace(first_execution_run_id=None, run_id=None)

    client.start_workflow = start
    monkeypatch.setattr(runner.worker, "connect", connect)
    assert main(arguments()) == 1


def test_report_export_is_exclusive_private_and_exact(monkeypatch, tmp_path, capsys):
    import hashlib
    import tarfile
    import runner.worker
    from tests.test_operator import archive

    raw = archive([("workspace/report.md", b"# exact report", tarfile.REGTYPE)])
    key = hashlib.sha256(raw).hexdigest()
    artifact = tmp_path / key[:2] / key
    artifact.parent.mkdir()
    artifact.write_bytes(raw)
    client = Client()

    async def connect(tenant):
        return client

    async def start(*args, **kw):
        return SimpleNamespace(id=inp().workflow_id, first_execution_run_id=RUN)

    async def result(**kw):
        from runner.types import SessionResult
        from tests.test_operator import CLEAN

        return SessionResult("COMPLETED", CLEAN, snapshot=key)

    client.start_workflow, client.result = start, result
    monkeypatch.setattr(runner.worker, "connect", connect)
    output = tmp_path / "report.md"
    assert (
        main(arguments() + ["--wait", "--report", str(output), "--artifact-root", str(tmp_path)])
        == 0
    )
    assert output.read_bytes() == b"# exact report"
    assert output.stat().st_mode & 0o777 == 0o600
    assert key not in capsys.readouterr().out


def test_namespace_override_rejected_before_start(monkeypatch):
    import runner.worker

    client = Client()
    client.namespace = "wrong"

    async def connect(tenant):
        return client

    async def start(*args, **kw):
        pytest.fail("namespace mismatch must not submit")

    client.start_workflow = start
    monkeypatch.setattr(runner.worker, "connect", connect)
    assert main(arguments()) == 1


def test_blocked_report_read_does_not_join_executor_or_write_late(monkeypatch, tmp_path):
    import time
    import runner.operator
    import runner.worker

    client = Client()

    async def connect(tenant):
        return client

    async def start(*args, **kw):
        return SimpleNamespace(id=inp().workflow_id, first_execution_run_id=RUN)

    def blocked(*args):
        time.sleep(0.2)
        return "late private report"

    client.start_workflow = start
    monkeypatch.setattr(runner.worker, "connect", connect)
    monkeypatch.setattr(runner.operator, "read_report", blocked)
    output = tmp_path / "report"
    started = time.monotonic()
    code = main(
        arguments()
        + [
            "--wait",
            "--wait-timeout",
            ".02",
            "--report",
            str(output),
            "--artifact-root",
            str(tmp_path),
        ]
    )
    elapsed = time.monotonic() - started
    assert code == 124 and elapsed < 0.15
    time.sleep(0.25)
    assert not output.exists()
