"""Operational commands bind plans, durable journals and exact tenant roles."""

import json
from unittest.mock import AsyncMock
from types import SimpleNamespace as N

import pytest

from runner.worker_versioning_cli import dispatch, private_json, Journal
from runner.worker_versioning import GuardError


@pytest.mark.asyncio
async def test_promotion_plan_cannot_cross_tenant_or_role(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema": "worker-versioning-operation-v1",
                "namespace": "agents-tenant-other",
                "deployment": "session-runner-tenant-other-governed-v1",
                "build_id": "new",
                "image": "pin",
                "current": "old",
                "conflict_sha256": "a" * 64,
            }
        )
    )
    operator = N(namespace="agents-tenant-test", promote=AsyncMock())
    with pytest.raises(GuardError):
        await dispatch(
            N(command="promote", plan=plan, journal=tmp_path / "journal.jsonl"), operator, None
        )
    operator.promote.assert_not_called()


def test_private_output_never_overwrites_and_is_mode_0600(tmp_path):
    path = tmp_path / "plan.json"
    private_json(path, {"safe": True})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        private_json(path, {"overwrite": True})
    assert json.loads(path.read_text()) == {"safe": True}


def test_journal_flushes_pending_before_any_mutation(tmp_path):
    path = tmp_path / "journal.jsonl"
    with Journal(path) as journal:
        journal({"status": "mutation_pending"})
        assert json.loads(path.read_text())["status"] == "mutation_pending"
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        with Journal(path):
            pass


@pytest.mark.asyncio
async def test_rollback_reuses_guarded_promotion_with_immutable_plan(tmp_path):
    plan = {
        "schema": "worker-versioning-operation-v1",
        "namespace": "agents-tenant-test",
        "deployment": "session-runner-tenant-test-governed-v1",
        "build_id": "retained",
        "image": "pinned",
        "current": "new",
        "conflict_sha256": "a" * 64,
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    operator = N(
        namespace="agents-tenant-test",
        tenant="tenant-test",
        role=lambda dep: "sessions",
        promote=AsyncMock(return_value={"status": "confirmed"}),
    )
    result = await dispatch(N(command="rollback", plan=path), operator, None)
    assert result["status"] == "confirmed"
    operator.promote.assert_awaited_once_with(
        plan["deployment"],
        "retained",
        "pinned",
        expected_current="new",
        expected_conflict_sha256="a" * 64,
    )


def test_cli_never_prints_raw_remote_error_context(monkeypatch, capsys):
    from runner import worker_versioning_cli as cli

    async def fail(args):
        raise RuntimeError("Bearer sensitive-remote-context")

    monkeypatch.setattr(cli, "run", fail)
    code = cli.main(
        [
            "--tenant",
            "tenant-test",
            "current",
            "--role",
            "sessions",
            "--pool",
            "legacy",
            "--image",
            "pinned",
        ]
    )
    assert code == 2
    text = capsys.readouterr().err
    assert "RuntimeError" in text
    assert "sensitive-remote-context" not in text


def test_mutating_commands_require_new_journal_and_plan():
    from runner.worker_versioning_cli import parser

    with pytest.raises(SystemExit):
        parser().parse_args(["--tenant", "tenant-test", "promote", "--plan", "plan.json"])
