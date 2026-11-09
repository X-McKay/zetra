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
