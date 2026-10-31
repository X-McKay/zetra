"""The runtime wheel must not accidentally include assistant configuration."""

import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "asset",
    [
        ".agents/skills/zetra-example/SKILL.md",
        "agent_package/.claude/skills/zetra-example/SKILL.md",
        "agent_package/CLAUDE.md",
        "developer-skills.json",
    ],
)
def test_wheel_rejects_developer_assistant_assets(tmp_path, asset):
    wheel = tmp_path / "agent_package-0.1.0-py3-none-any.whl"
    dependency = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"][0]
    with ZipFile(wheel, "w") as archive:
        archive.writestr("agent_package-0.1.0.dist-info/METADATA", f"Requires-Dist: {dependency}\n")
        for relative in (
            "agent_package/agents/document_reader/agent.yaml",
            "agent_package/agents/document_reader/instructions/base.md",
        ):
            archive.writestr(relative, (ROOT / "src" / relative).read_bytes())
        archive.writestr(asset, "developer-only")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/packaging/check_wheel.py"), str(wheel)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "developer" in result.stderr.lower()
