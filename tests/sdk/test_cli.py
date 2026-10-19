import json
from unittest.mock import AsyncMock

from zetra import cli


def test_cli_has_separate_worker_and_explicit_handles():
    args = cli.parser().parse_args(["worker", "--key-file", "private.key"])
    assert args.command == "worker"
    args = cli.parser().parse_args(["status", "handle.json"])
    assert args.handle == "handle.json"


def test_terminal_execution_is_nonzero_even_after_confirmed_deletion(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "execute",
        AsyncMock(
            return_value={
                "handle": {"run_id": "exact"},
                "result": {"status": "TERMINATED", "cleanup": "DELETED"},
            }
        ),
    )
    assert cli.main(["run", "release.json", "--session", "one", "--input", "task"]) == 1
    assert json.loads(capsys.readouterr().out)["result"]["status"] == "TERMINATED"


def test_fixture_eval_does_not_require_connection(monkeypatch):
    from zetra import evaluation

    monkeypatch.setattr(evaluation, "evaluate", AsyncMock(return_value={"status": "PASSED"}))
    assert cli.main(["eval", "examples/workspace_reporter", "--mode", "fixture"]) == 0


async def test_run_emits_handle_accepted_by_status_before_wait(tmp_path, monkeypatch, capsys):
    from temporalio.client import Client

    from zetra.client import SessionClient
    from zetra.session import SessionHandle

    handle = SessionHandle(namespace="default", workflow_id="one", run_id="run", runtime_id="local")
    monkeypatch.setattr(Client, "connect", AsyncMock(return_value=object()))
    monkeypatch.setattr(SessionClient, "submit", AsyncMock(return_value=handle))
    monkeypatch.setattr(SessionClient, "wait", AsyncMock(side_effect=KeyboardInterrupt))
    args = cli.parser().parse_args(["run", "unused", "--session", "one", "--input", "task"])
    import pytest

    with pytest.raises(KeyboardInterrupt):
        await cli.execute(args, lambda _: object())
    path = tmp_path / "handle.json"
    path.write_text(capsys.readouterr().err)
    assert SessionHandle.model_validate_json(path.read_text()) == handle
    status = AsyncMock(return_value={"status": "RUNNING"})
    monkeypatch.setattr(SessionClient, "status", status)
    result = await cli.execute(cli.parser().parse_args(["status", str(path)]), None)
    assert result == {"status": "RUNNING"}
    status.assert_awaited_once_with(handle)
