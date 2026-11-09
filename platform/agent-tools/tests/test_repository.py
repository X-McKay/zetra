"""A repository rendered by `init` is the layout every check expects; hand edits and
fixture files outside the allowlist are refused."""

import hashlib
import json
import textwrap
import types

import pytest
import yaml
from agent_platform import cli as agentctl

from zetra_agents import cli, layout, release, render

REV = "0123456789abcdef0123456789abcdef01234567"
AGENT = "demo-agent"
IMAGE = f"registry.almckay.io/agents/{AGENT}@sha256:" + "a9" * 32
BASE = "https://mcp.almckay.io"


@pytest.fixture
def repo(tmp_path, capsys):
    target = tmp_path / "repo"
    assert (
        cli.main(
            [
                "init",
                str(target),
                "--agent",
                AGENT,
                "--owner",
                "dev@example.com",
                "--description",
                "Demo agent",
                "--zetra-rev",
                REV,
            ]
        )
        == 0
    )
    capsys.readouterr()
    (target / "uv.lock").write_text("# locked by uv in a real repository\n")
    return target


def run(repo, *args):
    return cli.main(["--repo", str(repo), *args])


def test_init_renders_a_repository_that_passes_every_check(repo, capsys):
    assert run(repo, "check") == 0
    assert capsys.readouterr().out.strip() == f"ok: {AGENT}"
    assert run(repo, "render", "--check") == 0
    assert REV[:12] in capsys.readouterr().out
    assert run(repo, "list") == 0
    assert json.loads(capsys.readouterr().out) == [AGENT]
    assert run(repo, "image-target", AGENT) == 0
    assert capsys.readouterr().out.strip() == "runtime"
    for relative in render.RENDERED:
        assert (repo / relative).is_file(), relative
        assert "@@" not in (repo / relative).read_text()
    assert REV in (repo / "pyproject.toml").read_text()
    assert f"agent-release@{REV}" in (repo / ".github/workflows/release.yml").read_text()
    manifest = yaml.safe_load((repo / "agents" / AGENT / "agent.yaml").read_text())
    assert manifest["entrypoint"] == "demo_agent.agent:create_agent"
    cr = yaml.safe_load((repo / "agents" / AGENT / "deploy/agent.yaml").read_text())
    assert cr["metadata"] == {"name": AGENT, "namespace": "tenant-test"}
    assert cr["spec"]["owner"] == "dev@example.com"


def test_init_refuses_a_bad_name_and_a_nonempty_target(tmp_path, capsys):
    assert (
        cli.main(
            ["init", str(tmp_path / "x"), "--agent", "Bad", "--owner", "o", "--zetra-rev", REV]
        )
        == 2
    )
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "file").touch()
    assert (
        cli.main(
            ["init", str(tmp_path / "busy"), "--agent", "ok", "--owner", "o", "--zetra-rev", REV]
        )
        == 2
    )
    assert cli.main(["init", str(tmp_path / "y"), "--agent", "ok", "--owner", "o"]) == 2
    assert "zetra revision" in capsys.readouterr().err


def test_render_check_reports_hand_edits_and_render_repairs_them(repo, capsys):
    dockerfile = repo / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "RUN echo tampered\n")
    (repo / "justfile").unlink()
    assert run(repo, "render", "--check") == 1
    err = capsys.readouterr().err
    assert "Dockerfile: differs" in err and "justfile: missing" in err
    assert run(repo, "render") == 0
    capsys.readouterr()
    assert run(repo, "render", "--check") == 0


def test_render_check_requires_a_pinned_zetra_revision(repo, capsys):
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace(REV, "main"))
    assert run(repo, "render", "--check") == 1
    assert "no zetra commit pinned" in capsys.readouterr().err
    assert run(repo, "render") == 2  # nothing to derive the revision from
    assert run(repo, "render", "--zetra-rev", "f" * 40) == 0
    assert ("f" * 40) in pyproject.read_text()


def test_render_check_refuses_a_tooling_revision_mismatch(repo, capsys, monkeypatch):
    monkeypatch.setattr(render, "installed_revision", lambda: "e" * 40)
    assert run(repo, "render", "--check") == 1
    assert "this tooling is from eeeeeeeeeeee" in capsys.readouterr().err


def test_installed_revision_reads_the_git_origin_of_the_distribution(monkeypatch):
    class Dist:
        def __init__(self, payload):
            self.payload = payload

        def read_text(self, name):
            assert name == "direct_url.json"
            return self.payload

    good = json.dumps(
        {
            "url": "https://github.com/X-McKay/zetra.git",
            "subdirectory": "platform/agent-tools",
            "vcs_info": {"vcs": "git", "commit_id": REV, "requested_revision": REV},
        }
    )
    monkeypatch.setattr(render.metadata, "distribution", lambda name: Dist(good))
    assert render.installed_revision() == REV
    other = good.replace("X-McKay/zetra", "someone/fork")
    monkeypatch.setattr(render.metadata, "distribution", lambda name: Dist(other))
    assert render.installed_revision() is None
    monkeypatch.setattr(render.metadata, "distribution", lambda name: Dist(None))
    assert render.installed_revision() is None

    def only_cli(name):  # the zetra CLI came from git; the tooling rode along as a path dep
        if name == "zetra":
            return Dist(good.replace("platform/agent-tools", ""))
        raise render.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(render.metadata, "distribution", only_cli)
    assert render.installed_revision() == REV


def test_templates_leave_no_unknown_placeholders():
    with pytest.raises(render.RenderError, match="@@NOPE@@"):
        render.render_text("x @@NOPE@@ y", {"ZETRA_REV": REV})
    for text in render.rendered_files(REV).values():
        assert "@@" not in text


def test_repository_check_requires_the_workspace_files(repo, capsys):
    (repo / "uv.lock").unlink()
    assert run(repo, "check") == 1
    assert "repository: missing uv.lock" in capsys.readouterr().err


def test_release_lock_is_the_repository_lock_and_digests_agree_with_agentctl(
    repo, tmp_path, capsys
):
    out = tmp_path / "out"
    assert (
        run(repo, "release", AGENT, "--image", IMAGE, "--revision", "0" * 40, "--out", str(out))
        == 0
    )
    capsys.readouterr()
    rel = yaml.safe_load((out / "agentrelease.yaml").read_text())
    spec = rel["spec"]
    assert spec["approval"] == {"approved": False}
    assert spec["lockSha256"] == hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest()
    agent = layout.find(repo, AGENT)
    assert agent.lock_path == repo / "uv.lock"
    assert spec["manifestSha256"] == release.file_sha256(agent.manifest_path)
    assert rel["metadata"]["namespace"] == "tenant-test"
    predicate = json.loads((out / "predicate.json").read_text())
    assert predicate["lockSha256"] == spec["lockSha256"]
    sums = (out / "SHA256SUMS").read_text()
    assert "agentrelease.yaml" in sums and "predicate.json" in sums
    agentctl.main(
        [
            "policy-digest",
            "--agent",
            str(out / "agent.yaml"),
            "--release",
            str(out / "agentrelease.yaml"),
            "--mcp-resource-base",
            BASE,
        ]
    )
    assert capsys.readouterr().out.strip() == spec["policySha256"]


def test_check_rejects_network_imports_and_an_ungranted_model(repo):
    agent = layout.find(repo, AGENT)
    (agent.src / "demo_agent" / "leak.py").write_text("import httpx\nfrom socket import socket\n")
    manifest = agent.manifest()
    manifest["model"]["name"] = "some-other-model"
    agent.manifest_path.write_text(yaml.safe_dump(manifest))
    problems = layout.check(agent)
    assert any("imports httpx" in p for p in problems)
    assert any("imports socket" in p for p in problems)
    assert any("model is not granted" in p for p in problems)


def test_src_must_hold_exactly_one_package(repo):
    agent = layout.find(repo, AGENT)
    (agent.src / "second").mkdir()
    assert any("exactly one package" in p for p in layout.check(agent))
    assert agent.package_dir() is None


def test_fixture_files_are_refused_outside_the_allowlist(repo):
    agent = layout.find(repo, AGENT)
    (agent.root / layout.VERIFY_IMAGE).write_text("assert True\n")
    (agent.src / "demo_agent" / "step.py").write_text(
        "from agent_runtime._demo_sdk_step import x\n"
    )
    problems = layout.check(agent)
    assert len([p for p in problems if "belongs to a conformance fixture" in p]) == 2
    assert layout.image_target(agent) == "runtime"


def fixture_agent(repo, name="conformance-checkpoint", *, step=True, verify=True, delegates=True):
    root = repo / "agents" / name
    (root / "deploy").mkdir(parents=True)
    (root / "src" / "checkpoint_fixture").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "zetra-agent-{name}"\n')
    (root / "agent.yaml").write_text(yaml.safe_dump({"name": name, "model": None, "tools": []}))
    (root / "deploy/agent.yaml").write_text(
        yaml.safe_dump(
            {"kind": "Agent", "metadata": {"name": name}, "spec": {"capabilities": {"models": []}}}
        )
    )
    if verify:
        (root / layout.VERIFY_IMAGE).write_text("from agent_runtime import step\n")
    if step:
        body = (
            "from agent_runtime._checkpoint_sdk_step import load_manifest\n"
            if delegates
            else "CHECKPOINT_FIXTURE_OVERRIDE = 'x'\n"
        )
        (root / "src" / "checkpoint_fixture" / "step.py").write_text(body)
    return layout.AgentDir(root)


def test_allowlisted_fixtures_build_with_the_fixture_target_and_need_both_files(repo, capsys):
    agent = fixture_agent(repo)
    assert layout.check(agent) == []
    assert layout.image_target(agent) == "fixture"
    assert run(repo, "image-target", "conformance-checkpoint") == 0
    assert capsys.readouterr().out.strip() == "fixture"
    assert "conformance-checkpoint" in layout.FIXTURES
    assert "conformance" not in layout.FIXTURES and "investigator" not in layout.FIXTURES


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"verify": False}, "fixture is missing verify_image.py"),
        ({"step": False}, "fixture is missing step.py"),
        ({"delegates": False}, "must delegate to agent_runtime._<name>_sdk_step"),
    ],
)
def test_incomplete_fixtures_are_refused(repo, kwargs, message):
    agent = fixture_agent(repo, **kwargs)
    assert any(message in p for p in layout.check(agent)), layout.check(agent)


@pytest.mark.parametrize(
    ("model", "grants", "valid"),
    [
        (None, [], True),
        (None, None, False),
        (None, "missing", False),
        (None, [{"backend": "llm-main", "models": ["test"]}], False),
        ("missing", [], False),
        (
            {"backend": "llm-main", "name": "test"},
            [{"backend": "llm-main", "models": ["test"]}],
            True,
        ),
        (
            {"backend": "llm-main", "name": "wrong"},
            [{"backend": "llm-main", "models": ["test"]}],
            False,
        ),
    ],
)
def test_explicit_no_inference_contract(tmp_path, model, grants, valid):
    root = tmp_path / "agents" / "example"
    (root / "deploy").mkdir(parents=True)
    manifest = {"name": "example"}
    if model != "missing":
        manifest["model"] = model
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest))
    cr = {
        "kind": "Agent",
        "metadata": {"name": "example"},
        "spec": {"capabilities": {"models": grants}},
    }
    if grants == "missing":
        del cr["spec"]["capabilities"]["models"]
    (root / "deploy/agent.yaml").write_text(yaml.safe_dump(cr))
    (root / "pyproject.toml").touch()
    assert (not layout.check(layout.AgentDir(root))) is valid


def test_the_scaffolded_agent_is_a_working_agent(repo, tmp_path, monkeypatch):
    """Laid out as the image's /app, the rendered agent passes the SDK's own manifest and
    tool agreement check, and its tool writes a bounded report."""
    import asyncio
    import shutil
    import sys

    from agent_runtime import step

    app = tmp_path / "app"
    app.mkdir()
    agent = layout.find(repo, AGENT)
    shutil.copy(agent.manifest_path, app / "agent.yaml")
    shutil.copytree(agent.package_dir(), app / agent.package_dir().name)
    monkeypatch.setenv("AGENT_APP_DIR", str(app))
    monkeypatch.setenv("AGENT_SANDBOX_DIR", str(tmp_path / "sandbox"))
    try:
        instructions, built = step.build_agent(step.load_manifest())
        assert "Report ready" in instructions
        assert set(built) == {"write_report"}
        assert step.schema(built["write_report"])["function"]["parameters"]["required"] == [
            "content"
        ]
        module = sys.modules["demo_agent.agent"]  # imported from /app, as in the image
        ctx = types.SimpleNamespace(workspace=tmp_path, instructions="", mcp=None)
        tools = {t.fn.__name__: t.fn for t in module.create_agent(ctx)["tools"]}
        assert asyncio.run(tools["write_report"]("All good.")) == {"path": "report.md"}
        assert (tmp_path / "report.md").read_text() == "All good.\n"
        assert "error" in asyncio.run(tools["write_report"]("word " * 151))
    finally:
        if str(app) in sys.path:
            sys.path.remove(str(app))
        sys.modules.pop("demo_agent.agent", None)
        sys.modules.pop("demo_agent", None)


def test_dockerfile_targets_match_the_layout():
    text = render.template("Dockerfile")
    for target in layout.IMAGE_TARGETS:
        assert f" AS {target}" in text
    assert "COPY agents/${AGENT}/src/ /app/" in text
    assert 'uv sync --locked --no-dev --package "zetra-agent-${AGENT}"' in text
    assert textwrap.dedent("agent_runtime\\._([a-z]+)_sdk_step") in text


def test_network_and_fixture_allowlists_are_exact():
    assert layout.NETWORK_ALLOWED == {
        "conformance",
        "conformance-b",
        "conformance-budget-day",
        "conformance-budget-day-retry",
        "conformance-budget-day-retry2",
        "conformance-budget-session",
        "conformance-transport",
    }
    assert layout.FIXTURES == {
        "conformance-budget-day",
        "conformance-budget-day-retry",
        "conformance-budget-day-retry2",
        "conformance-budget-session",
        "conformance-checkpoint",
        "conformance-lifecycle",
        "conformance-performance",
        "conformance-protocol",
        "conformance-transport",
    }
    assert "investigator" not in layout.FIXTURES and "db-admin" not in layout.FIXTURES
