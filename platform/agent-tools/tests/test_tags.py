"""The tag a merge owes, the version check, and the workflows that use them."""

import json
import subprocess

import pytest
import yaml

from zetra_agents import cli, render, tags

REV = "0123456789abcdef0123456789abcdef01234567"


def write_agent(root, name, version):
    path = root / "agents" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "zetra-agent-{name}"\nversion = "{version}"\n'
    )
    return path


def test_tags_lists_missing_tags_in_name_order_and_skips_existing(tmp_path, capsys, monkeypatch):
    write_agent(tmp_path, "zeta", "1.0.0")
    write_agent(tmp_path, "alpha", "0.2.0")
    write_agent(tmp_path, "mid", "3.1.0")

    def fake_git(*args):
        assert args == ("tag", "--list")
        return "mid/v3.1.0\nalpha/v0.1.0\n"

    assert tags.missing_tags(tmp_path, fake_git) == ["alpha/v0.2.0", "zeta/v1.0.0"]
    monkeypatch.setattr(tags, "git_in", lambda repo: fake_git)
    assert cli.main(["--repo", str(tmp_path), "tags"]) == 0
    assert json.loads(capsys.readouterr().out) == ["alpha/v0.2.0", "zeta/v1.0.0"]


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def history(tmp_path):
    write_agent(tmp_path, "alpha", "1.0.0")
    write_agent(tmp_path, "beta", "1.0.0")
    (tmp_path / "agents/alpha/src").mkdir()
    (tmp_path / "agents/alpha/src/a.py").write_text("x = 1\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "change")
    return tmp_path


def commit(repo):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")


def check(repo, capsys):
    code = cli.main(["--repo", str(repo), "version-check", "--base", "main"])
    return code, capsys.readouterr().err


def test_version_check_ignores_tests_and_documents(history, capsys):
    (history / "agents/alpha/tests").mkdir()
    (history / "agents/alpha/tests/test_a.py").write_text("def test(): pass\n")
    (history / "agents/alpha/README.md").write_text("words\n")
    commit(history)
    assert check(history, capsys) == (0, "")


def test_version_check_fails_naming_the_agent_when_source_changed_alone(history, capsys):
    (history / "agents/alpha/src/a.py").write_text("x = 2\n")
    commit(history)
    code, err = check(history, capsys)
    assert code == 1
    assert "alpha" in err and "agents/alpha/pyproject.toml" in err
    assert "beta" not in err


def test_version_check_passes_when_the_version_changed_too(history, capsys):
    (history / "agents/alpha/src/a.py").write_text("x = 2\n")
    write_agent(history, "alpha", "1.0.1")
    commit(history)
    assert check(history, capsys) == (0, "")


def test_release_workflow_runs_one_release_at_a_time_and_reports_to_the_registry():
    text = render.render_text(render.template("workflows/release.yml"), {"ZETRA_REV": REV})
    workflow = yaml.safe_load(text)
    assert workflow["concurrency"] == {
        "group": "release-${{ github.repository }}",
        "cancel-in-progress": False,
    }
    job = workflow["jobs"]["release"]
    inputs = job["steps"][-1]["with"]
    assert inputs["registry-url"] == "${{ vars.AGENT_REGISTRY_URL }}"
    assert inputs["registry-audience"] == "${{ vars.AGENT_REGISTRY_AUDIENCE }}"
    # what the signature identity depends on is as it was
    assert workflow["name"] == "Release agent"
    assert workflow[True] == {"push": {"tags": ["*/v*"]}}
    assert list(workflow["jobs"]) == ["release"]
    assert workflow["permissions"] == {"contents": "read"}
    assert job["permissions"] == {"contents": "write", "id-token": "write"}


def test_tag_on_merge_pushes_only_with_the_key_and_prints_otherwise():
    text = render.rendered_files(REV)[".github/workflows/tag-on-merge.yml"]
    workflow = yaml.safe_load(text)
    assert workflow[True] == {"push": {"branches": ["main"]}}
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["tag"]["steps"]
    assert all("permissions" not in job for job in workflow["jobs"].values())
    pushing = [s for s in steps if "git push" in s.get("run", "")]
    printing = [s for s in steps if "Would create" in s.get("run", "")]
    assert len(pushing) == len(printing) == 1
    assert pushing[0]["if"] == "steps.key.outputs.present == 'true'"
    assert printing[0]["if"] == "steps.key.outputs.present != 'true'"
    probe = next(s for s in steps if s.get("id") == "key")
    assert probe["env"] == {"RELEASE_TAG_KEY": "${{ secrets.RELEASE_TAG_KEY }}"}
    assert "secrets." not in "".join(s.get("if", "") for s in steps)


def test_render_check_reports_a_repository_without_the_tag_workflow(tmp_path, capsys):
    target = tmp_path / "repo"
    args = ["init", str(target), "--agent", "demo-agent", "--owner", "dev@example.com"]
    assert cli.main([*args, "--zetra-rev", REV, "--no-lock", "--no-git"]) == 0
    capsys.readouterr()
    from tests.test_repository import lock_text

    (target / "uv.lock").write_text(lock_text(REV))
    assert cli.main(["--repo", str(target), "render", "--check"]) == 0
    (target / ".github/workflows/tag-on-merge.yml").unlink()
    assert cli.main(["--repo", str(target), "render", "--check"]) == 1
    assert ".github/workflows/tag-on-merge.yml: missing" in capsys.readouterr().err
