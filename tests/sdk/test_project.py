"""Project selection and immutable packaged resources are data-only contracts."""

import sys

import pytest

from zetra.manifest import load_agent, source_digest


def project(root, names=("reader",)):
    index = "[tool.zetra.agents]\n"
    for name in names:
        package = root / "src" / "sample" / "agents" / name
        (package / "instructions").mkdir(parents=True)
        for parent in (root / "src/sample", root / "src/sample/agents", package):
            (parent / "__init__.py").write_text("")
        (package / "factory.py").write_text('raise AssertionError("factory imported")\n')
        (package / "instructions/base.md").write_text("Read the approved guide.")
        (package / "agent.yaml").write_text(
            f"name: {name}\nowner: test\nentrypoint: sample.agents.{name}.factory:create_agent\n"
            f"instructions: sample.agents.{name}:instructions/base.md\n"
        )
        index += f'{name} = "src/sample/agents/{name}/agent.yaml"\n'
    (root / "pyproject.toml").write_text(index)
    return root


def test_single_agent_is_selected_without_importing_factory(tmp_path):
    root = project(tmp_path)
    spec = load_agent(root)
    assert spec.name == "reader"
    assert spec.instructions == "sample.agents.reader:instructions/base.md"
    assert "sample.agents.reader.factory" not in sys.modules


def test_multiple_agents_require_explicit_selection(tmp_path):
    root = project(tmp_path, ("reader", "writer"))
    with pytest.raises(ValueError, match="select"):
        load_agent(root)
    assert load_agent(root, agent="writer").name == "writer"
    with pytest.raises(ValueError, match="unknown"):
        load_agent(root, agent="missing")


@pytest.mark.parametrize("path", ["../outside.yaml", "/tmp/outside.yaml", "src/../outside.yaml"])
def test_manifest_path_cannot_escape_project(tmp_path, path):
    project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(f'[tool.zetra.agents]\nreader = "{path}"\n')
    with pytest.raises(ValueError, match="path"):
        load_agent(tmp_path)


def test_manifest_and_instruction_symlinks_are_rejected(tmp_path):
    project(tmp_path)
    resource = tmp_path / "src/sample/agents/reader/instructions/base.md"
    outside = tmp_path / "outside.md"
    outside.write_text("not a packaged resource")
    resource.unlink()
    resource.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        load_agent(tmp_path)


def test_manifest_name_must_match_index(tmp_path):
    project(tmp_path)
    manifest = tmp_path / "src/sample/agents/reader/agent.yaml"
    manifest.write_text(manifest.read_text().replace("name: reader", "name: another"))
    with pytest.raises(ValueError, match="name"):
        load_agent(tmp_path)


def test_missing_packaged_instruction_is_rejected_before_import(tmp_path):
    project(tmp_path)
    (tmp_path / "src/sample/agents/reader/instructions/base.md").unlink()
    with pytest.raises(ValueError, match="resource"):
        load_agent(tmp_path)


def test_generated_outputs_do_not_change_release_source(tmp_path):
    project(tmp_path)
    before = source_digest(tmp_path)
    (tmp_path / "artifacts/release/evidence").mkdir(parents=True)
    (tmp_path / "artifacts/release/evidence/result.json").write_text('{"passed":true}')
    (tmp_path / ".env").write_text("secret=value")
    assert source_digest(tmp_path) == before
    resource = tmp_path / "src/sample/agents/reader/instructions/base.md"
    resource.write_text("Changed instructions")
    assert source_digest(tmp_path) != before


def test_packaged_instruction_is_supplied_before_factory(tmp_path, monkeypatch):
    from zetra._runtime.definition import Definition

    project(tmp_path)
    path = tmp_path / "src/sample/agents/reader/factory.py"
    path.write_text(
        "from google.adk.agents import LlmAgent\n"
        "def create_agent(ctx):\n"
        "    assert ctx.instruction == 'Read the approved guide.'\n"
        "    return LlmAgent(name='reader', model=ctx.model, instruction=ctx.instruction)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path / "src"))
    try:
        definition = Definition(load_agent(tmp_path).model_dump(), tmp_path / "workspace")
        assert definition.build(object()).instruction == "Read the approved guide."
    finally:
        for name in list(sys.modules):
            if name == "sample" or name.startswith("sample."):
                sys.modules.pop(name)
