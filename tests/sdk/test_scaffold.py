import json
import subprocess
import sys

import pytest

from zetra.scaffold import scaffold


def test_generated_agent_runs_tests_and_eval_without_infrastructure(tmp_path):
    root = tmp_path / "created"
    scaffold(root, "generated_reader")
    with pytest.raises(ValueError, match="exists"):
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


def test_bundled_template_matches_recorded_canonical_tree():
    import hashlib
    from importlib.resources import files

    root = files("zetra").joinpath("templates/agent")
    content = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not any(
            part in {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "dist"}
            for part in path.relative_to(root).parts
        )
    }
    expected = json.loads(files("zetra").joinpath("templates/provenance.json").read_text())
    assert (
        hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        == expected["tree_sha256"]
    )


@pytest.mark.parametrize("qualified", [None, "0" * 40])
def test_unqualified_execution_revision_cannot_generate_incompatible_project(
    tmp_path, monkeypatch, qualified
):
    import importlib
    import shutil
    from importlib.resources import files

    module = importlib.import_module("zetra.scaffold")
    package = tmp_path / "sdk"
    shutil.copytree(files("zetra").joinpath("templates"), package / "templates")
    provenance = package / "templates/provenance.json"
    data = json.loads(provenance.read_text())
    data["qualified_sdk_revision"] = qualified
    provenance.write_text(json.dumps(data))
    monkeypatch.setattr(module, "files", lambda _: package)
    with pytest.raises(ValueError, match="qualified template"):
        scaffold(tmp_path / "new-project", "reader")
    assert not (tmp_path / "new-project").exists()


@pytest.mark.parametrize("name", ["zetra", "google", "class", "pytest", "yaml", "json"])
def test_sdk_renderer_rejects_reserved_names_without_writing(tmp_path, name):
    with pytest.raises(ValueError, match="reserved"):
        scaffold(tmp_path / name, name)
    assert not (tmp_path / name).exists()


def test_bundled_renderer_matches_recorded_canonical_implementation():
    import hashlib
    from importlib.resources import files

    root = files("zetra").joinpath("templates")
    expected = json.loads(root.joinpath("provenance.json").read_text())
    assert (
        hashlib.sha256(root.joinpath("render.py").read_bytes()).hexdigest()
        == expected["renderer_sha256"]
    )
