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
