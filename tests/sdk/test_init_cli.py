"""`zetra init` renders a platform agent repository; `--harness` keeps the OpenShell project."""

import json
import subprocess
import sys

import yaml

REV = "0123456789abcdef0123456789abcdef01234567"


def zetra(*args):
    return subprocess.run(
        [sys.executable, "-m", "zetra", *args], capture_output=True, text=True, check=False
    )


def test_init_renders_a_platform_agent_repository(tmp_path):
    root = tmp_path / "my-agent"
    result = zetra(
        "init",
        str(root),
        "--name",
        "my-agent",
        "--owner",
        "dev@example.com",
        "--tenant",
        "tenant-test",
        "--zetra-rev",
        REV,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "CREATED" and summary["zetra_revision"] == REV
    assert (root / "Dockerfile").is_file() and (root / "justfile").is_file()
    assert (root / ".github/workflows/release.yml").is_file()
    assert REV in (root / "pyproject.toml").read_text()
    manifest = yaml.safe_load((root / "agents/my-agent/agent.yaml").read_text())
    assert manifest["entrypoint"] == "my_agent.agent:create_agent"
    cr = yaml.safe_load((root / "agents/my-agent/deploy/agent.yaml").read_text())
    assert cr["spec"]["owner"] == "dev@example.com"
    assert cr["metadata"]["namespace"] == "tenant-test"
    assert not (root / "src").exists()  # the harness layout is not mixed in
    again = zetra(
        "init", str(root), "--name", "my-agent", "--owner", "dev@example.com", "--zetra-rev", REV
    )
    assert again.returncode != 0 and "not empty" in again.stdout + again.stderr


def test_init_requires_a_revision_or_owner_it_can_determine(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-git-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    result = zetra("init", str(tmp_path / "a"), "--name", "a", "--zetra-rev", REV)
    assert result.returncode != 0 and "--owner" in result.stdout + result.stderr
    result = zetra(
        "init",
        str(tmp_path / "b"),
        "--name",
        "b",
        "--owner",
        "o@example.com",
        "--zetra-rev",
        "main",
    )
    assert result.returncode != 0 and "40-hex" in result.stdout + result.stderr
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_init_harness_renders_the_openshell_project(tmp_path):
    root = tmp_path / "reader"
    result = zetra("init", "--harness", str(root), "--name", "reader")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "CREATED"
    assert (root / "src/reader/agents/document_reader/agent.yaml").is_file()
    assert not (root / "agents").exists()
