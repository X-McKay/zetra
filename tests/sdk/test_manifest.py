import importlib.util


def test_manifest_module_exists():
    assert importlib.util.find_spec("zetra.manifest") is not None


import pytest

from zetra.build import snapshot_package
from zetra.manifest import AgentSpec, Release, load_agent, source_digest


def test_mutable_images_and_undeclared_fields_rejected():
    with pytest.raises(ValueError):
        Release.immutable_image("agent:latest")
    with pytest.raises(ValueError):
        AgentSpec(name="a", owner="o", entrypoint="a:create", unknown=True)
    with pytest.raises(ValueError):
        AgentSpec(name="a", owner="o", entrypoint="a:create", model="vllm").validate_policy()


def test_snapshot_and_symlink_rules(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "file").write_text("first")
    captured = snapshot_package(root, tmp_path / "copy")
    (root / "file").write_text("second")
    assert captured != source_digest(root)
    assert captured == source_digest(tmp_path / "copy")
    (root / "link").symlink_to("file")
    with pytest.raises(ValueError, match="symlink"):
        source_digest(root)


def test_check_never_imports_agent_and_duplicate_keys_rejected(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: a\nowner: owner\nentrypoint: nonexistent.agent:create\n"
    )
    assert load_agent(tmp_path).name == "a"
    (tmp_path / "agent.yaml").write_text("name: a\nname: b\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_agent(tmp_path)


def test_source_digest_has_unambiguous_file_boundaries(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a").write_bytes(b"\0b\0y")
    (second / "a").write_bytes(b"")
    (second / "b").write_bytes(b"y")
    assert source_digest(first) != source_digest(second)


def test_agent_recipe_preserves_canonical_sdk_build_assets(tmp_path, monkeypatch):
    from zetra import build as builder
    from zetra.scaffold import scaffold

    source = tmp_path / "agent"
    scaffold(source, "image_agent")

    class BuildObserved(Exception):
        pass

    def inspect_build(command, **kwargs):
        from pathlib import Path

        context = Path(command[-1])
        assert "COPY agent /app/agent" not in (context / "Dockerfile").read_text()
        assert "COPY agent /app/agent" in (context / "Dockerfile.agent").read_text()
        assert command[command.index("-f") + 1] == str(context / "Dockerfile.agent")
        raise BuildObserved

    monkeypatch.setattr(builder.subprocess, "run", inspect_build)
    with pytest.raises(BuildObserved):
        builder.build(source, tag="image-agent:local")
