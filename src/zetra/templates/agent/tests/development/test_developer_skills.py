"""Exercise managed skill updates without importing application code."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/developer_skills.py"
RELATIVE = "zetra-example/SKILL.md"
ORIGINAL = b"---\nname: zetra-example\ndescription: Example workflow.\n---\nRead the manifest.\n"


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def project(tmp_path):
    write(tmp_path / ".agents/skills" / RELATIVE, ORIGINAL)
    write(tmp_path / ".claude/skills" / RELATIVE, ORIGINAL)
    (tmp_path / "developer-skills.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_version": "0.1.0",
                "files": {RELATIVE: hashlib.sha256(ORIGINAL).hexdigest()},
            }
        )
    )
    return tmp_path


def run(root, mode="--check"):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), mode],
        capture_output=True,
        text=True,
        check=False,
    )


def test_matching_pack_passes_without_writing(project):
    before = {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()}
    result = run(project)
    assert result.returncode == 0, result.stderr
    assert before == {p: p.stat().st_mtime_ns for p in before}


def test_missing_mirror_fails_check_and_sync_restores_it(project):
    mirror = project / ".claude/skills" / RELATIVE
    mirror.unlink()
    assert run(project).returncode != 0
    assert not mirror.exists()
    result = run(project, "--sync")
    assert result.returncode == 0, result.stderr
    assert mirror.read_bytes() == ORIGINAL
    assert run(project).returncode == 0


def test_sync_updates_canonical_change_and_preserves_unrelated_skill(project):
    updated = ORIGINAL + b"Use the local runner.\n"
    write(project / ".agents/skills" / RELATIVE, updated)
    unrelated = project / ".claude/skills/team-review/SKILL.md"
    write(unrelated, b"Project-owned instructions.\n")
    assert run(project).returncode != 0
    result = run(project, "--sync")
    assert result.returncode == 0, result.stderr
    assert (project / ".claude/skills" / RELATIVE).read_bytes() == updated
    assert unrelated.read_bytes() == b"Project-owned instructions.\n"
    assert run(project).returncode == 0


def test_conflicting_mirror_prevents_all_writes(project):
    write(project / ".agents/skills" / RELATIVE, ORIGINAL + b"New upstream.\n")
    mirror = project / ".claude/skills" / RELATIVE
    write(mirror, b"Local changes must survive.\n")
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    result = run(project, "--sync")
    assert result.returncode != 0
    assert "conflict" in result.stderr.lower()
    assert before == {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}


@pytest.mark.parametrize("relative", ["../outside", "/tmp/outside", "zetra-x/../SKILL.md"])
def test_unsafe_inventory_is_rejected_before_writes(project, relative):
    manifest = project / "developer-skills.json"
    data = json.loads(manifest.read_text())
    data["files"] = {relative: hashlib.sha256(ORIGINAL).hexdigest()}
    manifest.write_text(json.dumps(data))
    before = manifest.read_bytes()
    assert run(project, "--sync").returncode != 0
    assert manifest.read_bytes() == before


@pytest.mark.parametrize("directory", [".agents", ".claude"])
def test_symlinked_skill_root_is_rejected(project, tmp_path_factory, directory):
    import shutil

    outside = tmp_path_factory.mktemp("outside")
    write(outside / "skills" / RELATIVE, ORIGINAL)
    shutil.rmtree(project / directory)
    (project / directory).symlink_to(outside, target_is_directory=True)
    result = run(project, "--sync")
    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert (outside / "skills" / RELATIVE).read_bytes() == ORIGINAL


@pytest.mark.parametrize("value", [{}, {"schema_version": 2}, {"files": []}, []])
def test_malformed_inventory_fails_cleanly(project, value):
    (project / "developer-skills.json").write_text(json.dumps(value))
    result = run(project)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def test_additional_file_in_managed_skill_is_not_silently_ignored(project):
    write(project / ".claude/skills/zetra-example/old-instructions.md", b"stale")
    assert run(project).returncode != 0


def test_generated_repository_pack_is_consistent():
    result = run(SCRIPT.parents[1])
    assert result.returncode == 0, result.stderr
