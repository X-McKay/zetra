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
    (root / "src").mkdir(parents=True)
    (root / "src/file").write_text("first")
    captured = snapshot_package(root, tmp_path / "copy")
    (root / "src/file").write_text("second")
    assert captured != source_digest(root)
    assert captured == source_digest(tmp_path / "copy")
    (root / "src/link").symlink_to("file")
    with pytest.raises(ValueError, match="symlink"):
        source_digest(root)


def test_check_never_imports_agent_and_duplicate_keys_rejected(tmp_path):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "pyproject.toml").write_text('[tool.zetra.agents]\na="src/agent.yaml"\n')
    (tmp_path / "src/agent.yaml").write_text(
        "name: a\nowner: owner\nentrypoint: nonexistent.agent:create\n"
    )
    assert load_agent(tmp_path).name == "a"
    (tmp_path / "src/agent.yaml").write_text("name: a\nname: b\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_agent(tmp_path)


def test_source_digest_has_unambiguous_file_boundaries(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    (first / "src").mkdir(parents=True)
    (second / "src").mkdir(parents=True)
    (first / "src/a").write_bytes(b"\0b\0y")
    (second / "src/a").write_bytes(b"")
    (second / "src/b").write_bytes(b"y")
    assert source_digest(first) != source_digest(second)


def test_agent_recipe_uses_only_project_lock_and_snapshot(tmp_path, monkeypatch):
    from zetra import build as builder
    from zetra.scaffold import scaffold

    source = tmp_path / "agent"
    scaffold(source, "image_agent")

    class BuildObserved(Exception):
        pass

    def inspect_build(command, **kwargs):
        from pathlib import Path

        context = Path(command[-1])
        recipe = (context / "Dockerfile.agent").read_text()
        assert "COPY agent/ ./" in recipe
        assert "uv sync --locked --no-dev --no-editable" in recipe
        assert not (context / "src/zetra").exists()
        assert (context / "agent/uv.lock").read_bytes() == (source / "uv.lock").read_bytes()
        assert not (context / "agent/artifacts").exists()
        assert command[command.index("-f") + 1] == str(context / "Dockerfile.agent")
        raise BuildObserved

    monkeypatch.setattr(builder.subprocess, "run", inspect_build)
    with pytest.raises(BuildObserved):
        builder.build(source, tag="image-agent:local")


@pytest.mark.parametrize("change", ["missing", "mutable", "mismatch", "local"])
def test_build_rejects_unbound_sdk_lock(tmp_path, change):
    from zetra.build import lock_identity
    from zetra.scaffold import scaffold

    scaffold(tmp_path / "agent", "locked-agent")
    root = tmp_path / "agent"
    if change == "missing":
        (root / "uv.lock").unlink()
    elif change == "mutable":
        import re

        p = root / "pyproject.toml"
        p.write_text(re.sub(r'rev = "[a-f0-9]{40}"', 'rev = "main"', p.read_text()))
    elif change == "mismatch":
        import re

        p = root / "uv.lock"
        p.write_text(re.sub(r"#[a-f0-9]{40}", "#" + "0" * 40, p.read_text()))
    else:
        p = root / "uv.lock"
        p.write_text(
            p.read_text()
            + '\n[[package]]\nname="local-extra"\nversion="1"\nsource={directory="../local"}\n'
        )
    with pytest.raises(ValueError):
        lock_identity(root)


def test_snapshot_excludes_local_secrets_and_generated_evidence(tmp_path):
    from zetra.scaffold import scaffold

    root = tmp_path / "agent"
    scaffold(root, "locked-agent")
    (root / ".env").write_text("SECRET=not-for-image")
    (root / "artifacts").mkdir()
    (root / "artifacts/report.json").write_text("{}")
    snapshot_package(root, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot/.env").exists()
    assert not (tmp_path / "snapshot/artifacts").exists()
    assert not (tmp_path / "snapshot/tests").exists()
