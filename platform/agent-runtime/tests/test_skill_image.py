"""Skills on their way into an image and in it (skills.md section 12)."""

import os
import pathlib
import sys

import pytest
import yaml

from agent_runtime.skills import format as fmt
from agent_runtime.skills import image

SKILL_MD = "---\nname: {name}\ndescription: Counts rows. Use when asked how many.\n---\n\nBody.\n"
SIDECAR = "schema_version: 1\nscripts:\n  - path: scripts/count.py\n    interpreter: {}\n"


def skill(root: pathlib.Path, name: str, interpreter: str | None = "python3") -> pathlib.Path:
    directory = root / "skills" / name
    (directory / "scripts").mkdir(parents=True)
    (directory / "SKILL.md").write_text(SKILL_MD.format(name=name))
    (directory / "scripts" / "count.py").write_text("print(1)\n")
    (directory / "scripts" / "count.py").chmod(0o755)
    if interpreter is not None:
        (directory / "zetra.yaml").write_text(SIDECAR.format(interpreter))
    return directory


@pytest.fixture
def repo(tmp_path):
    """A repository with two skills and an agent that lists one of them, pinned."""
    skill(tmp_path, "row-count")
    skill(tmp_path, "unused")
    agent = tmp_path / "agents" / "demo"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text(yaml.safe_dump({"name": "demo", "skills": ["row-count"]}))
    pinned = [fmt.read(tmp_path / "skills" / "row-count")]
    (agent / "skills.lock").write_text(fmt.lock_text(pinned))
    return tmp_path


def stage(repo, out):
    return image.stage(repo / "agents" / "demo", repo / "skills", out)


def app_of(repo, tmp_path):
    """An app directory as the image has it: the manifest and what `stage` wrote."""
    app = tmp_path / "app"
    assert stage(repo, app) == []
    (app / "agent.yaml").write_bytes((repo / "agents" / "demo" / "agent.yaml").read_bytes())
    return app


def test_stage_writes_only_the_pinned_skills_and_the_lock(repo, tmp_path):
    out = tmp_path / "out"
    assert stage(repo, out) == []
    assert sorted(p.name for p in (out / "skills").iterdir()) == ["row-count"]
    assert (out / "skills.lock").read_bytes() == (repo / "agents/demo/skills.lock").read_bytes()
    # the copy has the digest the lock pins, the execute bit included
    assert fmt.digest(out / "skills" / "row-count") == fmt.digest(repo / "skills" / "row-count")
    assert os.access(out / "skills" / "row-count" / "scripts" / "count.py", os.X_OK)


def test_a_changed_skill_without_a_new_lock_fails_the_build(repo, tmp_path, capsys):
    (repo / "skills" / "row-count" / "scripts" / "count.py").write_text("print(2)\n")
    out = tmp_path / "out"
    assert stage(repo, out) == ["skill row-count changed since it was pinned in skills.lock"]
    assert not out.exists()
    command = ["stage", "--agent", str(repo / "agents" / "demo")]
    assert image.main([*command, "--skills", str(repo / "skills"), "--out", str(out)]) == 1
    # the message names the skill and the command that writes the lock
    assert capsys.readouterr().err == (
        "skill row-count changed since it was pinned in skills.lock\n"
        "skills.lock is written by `zetra-agents render`\n"
    )


def test_a_missing_or_extra_lock_fails_the_build(repo, tmp_path):
    lock = repo / "agents" / "demo" / "skills.lock"
    kept = lock.read_text()
    lock.unlink()
    assert stage(repo, tmp_path / "a") == ["skills.lock is missing"]
    lock.write_text(kept)
    (repo / "agents" / "demo" / "agent.yaml").write_text(yaml.safe_dump({"name": "demo"}))
    assert stage(repo, tmp_path / "b") == ["skills.lock exists and the agent lists no skills"]


def test_an_agent_without_skills_stages_an_empty_directory(repo, tmp_path):
    (repo / "agents" / "demo" / "agent.yaml").write_text(yaml.safe_dump({"name": "demo"}))
    (repo / "agents" / "demo" / "skills.lock").unlink()
    out = tmp_path / "out"
    # the repository need not have a skills directory at all
    assert image.stage(repo / "agents" / "demo", repo / "no-such-directory", out) == []
    assert list((out / "skills").iterdir()) == [] and not (out / "skills.lock").exists()
    (out / "agent.yaml").write_text(yaml.safe_dump({"name": "demo"}))
    assert image.verify(out) == []


def test_verify_passes_what_stage_wrote(repo, tmp_path):
    app = app_of(repo, tmp_path)
    here = tmp_path / "bin"
    here.mkdir()
    (here / "python3").symlink_to(sys.executable)
    assert image.verify(app, interpreter_path=str(here)) == []


def test_verify_fails_when_a_named_interpreter_is_not_in_the_image(repo, tmp_path):
    app = app_of(repo, tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    (problem,) = image.verify(app, interpreter_path=str(empty))
    assert "skill row-count names the interpreter python3, which is not in the image" in problem


def test_verify_fails_when_the_image_differs_from_the_lock(repo, tmp_path):
    app = app_of(repo, tmp_path)
    (app / "skills" / "row-count" / "SKILL.md").write_text(SKILL_MD.format(name="row-count") + "x")
    assert image.verify(app) == ["skill row-count changed since it was pinned in skills.lock"]


def test_verify_fails_on_a_skill_the_agent_does_not_list(repo, tmp_path):
    app = app_of(repo, tmp_path)
    skill(app, "stowaway")
    (problem,) = image.verify(app)
    assert "carries stowaway" in problem
