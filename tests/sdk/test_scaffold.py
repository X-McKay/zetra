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
    assert not (root / "src/generated_reader/workflows").exists()


def test_static_check_cannot_import_factory(tmp_path):
    root = tmp_path / "created"
    scaffold(root, "unimportable_agent")
    (root / "src/unimportable_agent/agents/document_reader/factory.py").write_text(
        'raise AssertionError("imported")'
    )
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
    tools = second / "src/same_package/agents/document_reader/tools/document_read.py"
    tools.write_text(
        tools.read_text().replace(
            'return "The approved guide',
            'raise ValueError("distinct factory") # "The approved guide',
        )
    )
    assert (await fixture_run(first, "Complete task"))["status"] == "SUCCEEDED"
    with pytest.raises(Exception, match="distinct factory"):
        await fixture_run(second, "Complete task")


@pytest.mark.parametrize("name", ["my-reader", "my_reader"])
def test_generated_distribution_and_import_names_are_consistent(tmp_path, name):
    import tomllib

    from zetra.manifest import load_agent

    scaffold(tmp_path / "agent", name)
    root = tmp_path / "agent"
    assert load_agent(root).entrypoint.startswith("my_reader.agents.")
    config = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    assert config["project"]["name"] == "my-reader"
    assert any(p["name"] == "my-reader" for p in lock["package"])
    assert (root / "src/my_reader/agents/document_reader/instructions/base.md").is_file()


@pytest.mark.asyncio
async def test_evidence_binds_selected_agent_and_dataset(tmp_path):
    import hashlib

    from zetra.evaluation import evaluate
    from zetra.manifest import digest, load_agent

    scaffold(tmp_path / "agent", "evidence-agent")
    root = tmp_path / "agent"
    report = await evaluate(root, mode="fixture")
    assert report["agent"] == "document_reader"
    assert report["agent_sha256"] == digest(load_agent(root).model_dump())
    assert (
        report["dataset_sha256"]
        == hashlib.sha256((root / "evals/document_reader/cases.jsonl").read_bytes()).hexdigest()
    )
