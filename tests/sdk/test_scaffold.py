import json
import subprocess
import sys

import pytest

from zetra.scaffold import scaffold


def test_generated_agent_runs_tests_and_eval_without_infrastructure(tmp_path):
    root = tmp_path / "created"
    scaffold(root, "generated_reader")
    with pytest.raises(ValueError, match="empty"):
        scaffold(root, "generated_reader")
    tested = subprocess.run(
        [sys.executable, "-m", "zetra", "test", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert tested.returncode == 0, tested.stdout + tested.stderr
    evaluated = subprocess.run(
        [sys.executable, "-m", "zetra", "eval", str(root), "--mode", "fixture"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert evaluated.returncode == 0, evaluated.stdout + evaluated.stderr
    assert json.loads(evaluated.stdout)["status"] == "PASSED"
    assert not list(root.rglob("*workflow*"))


def test_static_check_cannot_import_factory(tmp_path):
    root = tmp_path / "created"
    scaffold(root, "unimportable_agent")
    (root / "src/unimportable_agent/agent.py").write_text('raise AssertionError("imported")')
    result = subprocess.run(
        [sys.executable, "-m", "zetra", "check", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_profile_denials_never_become_permissions(tmp_path):
    from zetra.policy import profile

    path = tmp_path / "events.jsonl"
    event = {
        "agent": "one",
        "image": "example@sha256:" + "a" * 64,
        "kind": "read",
        "target": "/secret",
        "outcome": "denied",
    }
    path.write_text(json.dumps(event) + "\n")
    result = profile(path)
    assert result["authorizes"] is False
    assert result["observations"] == []
    assert result["denied"] == [event]


@pytest.mark.asyncio
async def test_fixture_runs_do_not_reuse_factories_from_other_sources(tmp_path):
    from zetra.evaluation import fixture_run

    first, second = tmp_path / "first", tmp_path / "second"
    scaffold(first, "same_package")
    scaffold(second, "same_package")
    tools = second / "src/same_package/tools.py"
    tools.write_text(
        tools.read_text()
        .replace("summary.md", "second.md")
        .replace("(workspace /", 'print("agent diagnostic")\n        (workspace /')
    )
    assert "summary.md" in (await fixture_run(first, "Complete task"))["files"]
    files = (await fixture_run(second, "Complete task"))["files"]
    assert "second.md" in files
    assert "summary.md" not in files
