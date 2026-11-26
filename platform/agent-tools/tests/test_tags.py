"""The tag a merge owes, the version check, and the workflows that use them."""

import json
import os
import subprocess
from pathlib import Path

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


def test_version_check_refuses_a_new_version_that_already_has_its_tag(history, capsys):
    git(history, "tag", "alpha/v1.0.1")
    (history / "agents/alpha/src/a.py").write_text("x = 2\n")
    write_agent(history, "alpha", "1.0.1")
    commit(history)
    code, err = check(history, capsys)
    assert code == 1
    assert err == (
        "alpha: version 1.0.1 in agents/alpha/pyproject.toml already has the tag "
        "alpha/v1.0.1; raise it above the highest tag\n"
    )


def test_version_check_does_not_blame_an_untouched_agent_for_its_own_tag(history, capsys):
    git(history, "tag", "alpha/v1.0.0")
    git(history, "tag", "beta/v1.0.0")
    (history / "agents/alpha/src/a.py").write_text("x = 2\n")
    write_agent(history, "alpha", "1.0.1")
    commit(history)
    assert check(history, capsys) == (0, "")


def test_version_check_refuses_a_new_agent_whose_tag_exists(history, capsys):
    write_agent(history, "gamma", "0.1.0")
    git(history, "tag", "gamma/v0.1.0")
    commit(history)
    code, err = check(history, capsys)
    assert code == 1
    assert err.startswith("gamma: version 0.1.0 in agents/gamma/pyproject.toml already has")


def test_unversioned_changes_asks_git_for_the_tags_only_when_an_agent_changed(tmp_path):
    calls = []

    def fake_git(*args):
        calls.append(args[0])
        return {"merge-base": "abc\n", "diff": "agents/alpha/tests/t.py\0"}[args[0]]

    assert tags.unversioned_changes(tmp_path, "main", fake_git) == []
    assert "tag" not in calls


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
    # No tag is created, so the run says so, and only when a tag is owed; it still succeeds.
    assert "::warning" in printing[0]["run"]
    assert "RELEASE_TAG_KEY" in printing[0]["run"]
    assert "length')\" -gt 0" in printing[0]["run"]
    assert "exit" not in printing[0]["run"]


def test_tag_on_merge_warning_is_printed_only_when_a_tag_is_owed(tmp_path):
    step = next(
        s
        for s in yaml.safe_load(render.rendered_files(REV)[".github/workflows/tag-on-merge.yml"])[
            "jobs"
        ]["tag"]["steps"]
        if "Would create" in s.get("run", "")
    )

    def run(tags):
        done = subprocess.run(
            ["bash", "-ec", step["run"]],
            env={**os.environ, "TAGS": tags},
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    owed = run('["alpha/v1.0.0","beta/v2.0.0"]')
    assert owed.count("::warning") == 1
    assert "no tag was created" in owed and "RELEASE_TAG_KEY" in owed
    assert "alpha/v1.0.0, beta/v2.0.0" in owed
    assert "::warning" not in run("[]")


def test_release_template_names_the_registry_variables():
    text = render.rendered_files(REV)[".github/workflows/release.yml"]
    assert "AGENT_REGISTRY_URL, AGENT_REGISTRY_AUDIENCE" in text


def test_the_release_action_warns_when_the_registry_url_is_unset():
    action = Path(__file__).parents[3] / ".github" / "actions" / "agent-release" / "action.yml"
    steps = yaml.safe_load(action.read_text())["runs"]["steps"]
    warning = next(s for s in steps if "not reported" in s.get("name", ""))
    assert warning["if"] == "inputs.registry-url == ''"
    for text in ("::warning", "AGENT_REGISTRY_URL", "AGENT_REGISTRY_AUDIENCE", "not reported"):
        assert text in warning["run"]
    assert "exit" not in warning["run"]
    assert steps.index(warning) < len(steps) - 1  # the report step stays last


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
