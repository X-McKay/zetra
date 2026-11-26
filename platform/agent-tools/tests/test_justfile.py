"""The rendered justfile's `check` stops at the first failing agent or discovery failure,
exercised with a fake `uv` so no environment is installed."""

import os
import shutil
import subprocess

import pytest

from zetra_agents import render

FAKE_UV = """#!/bin/sh
case "$*" in
  *"zetra-agents list"*)
    if [ "$FAILING_AGENT" = list ]; then exit 19; fi
    echo '["first", "last"]'
    if [ "$FAILING_AGENT" = partial-list ]; then exit 19; fi
    exit 0 ;;
  *"zetra-agents version-check --base origin/main"*)
    echo version-check >> "$CHECK_LOG"
    exit "${VERSION_CHECK_RC:-0}" ;;
  *"zetra-agents check"*|*"zetra-agents render --check"*|*"ruff check ."*|*"ruff format --check ."*)
    exit 0 ;;
  *"pytest -q agents/"*)
    agent=${*##*agents/}
    echo "$agent" >> "$CHECK_LOG"
    if [ "$agent" = "$FAILING_AGENT" ]; then exit 17; fi
    exit 0 ;;
esac
echo "unexpected uv invocation: $*" >&2
exit 23
"""


@pytest.mark.parametrize("failing_agent", ["first", "", "list", "partial-list"])
def test_check_propagates_agent_and_discovery_failures(tmp_path, failing_agent):
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is required to exercise the rendered recipes")
    (tmp_path / "justfile").write_text(render.template("justfile"))
    (tmp_path / "bin").mkdir()
    uv = tmp_path / "bin/uv"
    uv.write_text(FAKE_UV)
    uv.chmod(0o755)
    log = tmp_path / "checks.log"
    result = subprocess.run(
        [just, "check"],
        check=False,
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}",
            "CHECK_LOG": str(log),
            "FAILING_AGENT": failing_agent,
        },
        capture_output=True,
        text=True,
    )
    if failing_agent:
        assert result.returncode != 0, result.stdout + result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert log.read_text().splitlines() == ["first", "last"]


def run_check(tmp_path, with_origin, version_check_rc=0):
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is required to exercise the rendered recipes")
    (tmp_path / "justfile").write_text(render.template("justfile"))
    (tmp_path / "bin").mkdir()
    uv = tmp_path / "bin/uv"
    uv.write_text(FAKE_UV)
    uv.chmod(0o755)
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    if with_origin:
        subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    log = tmp_path / "checks.log"
    result = subprocess.run(
        [just, "check"],
        check=False,
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}",
            "CHECK_LOG": str(log),
            "FAILING_AGENT": "",
            "VERSION_CHECK_RC": str(version_check_rc),
        },
        capture_output=True,
        text=True,
    )
    return result, log.read_text().splitlines() if log.exists() else []


def test_check_runs_version_check_against_origin_main_when_it_exists(tmp_path):
    result, log = run_check(tmp_path, with_origin=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert log == ["version-check", "first", "last"]


def test_check_fails_when_version_check_does(tmp_path):
    result, log = run_check(tmp_path, with_origin=True, version_check_rc=1)
    assert result.returncode == 1
    assert log == ["version-check"]


def test_check_skips_version_check_with_one_line_when_there_is_no_origin_main(tmp_path):
    result, log = run_check(tmp_path, with_origin=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert log == ["first", "last"]
    assert result.stdout.count("version-check skipped") == 1
