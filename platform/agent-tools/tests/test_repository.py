"""A repository rendered by `init` is the layout every check expects; hand edits and
fixture files outside the allowlist are refused."""

import hashlib
import json
import shutil
import textwrap

import pytest
import yaml
from agent_platform import cli as agentctl

from zetra_agents import cli, layout, release, render
from zetra_agents import manifest as manifest_tools

REV = "0123456789abcdef0123456789abcdef01234567"
AGENT = "demo-agent"
IMAGE = f"registry.almckay.io/agents/{AGENT}@sha256:" + "a9" * 32
BASE = "https://mcp.almckay.io"


def lock_text(revision, overrides=None):
    entries = []
    for name, sub in render.PINNED.items():
        git = f"{render.ZETRA_GIT}?subdirectory={sub.replace('/', '%2F')}&rev={revision}#{revision}"
        git = (overrides or {}).get(name, git)
        entries.append(
            f'[[package]]\nname = "{name}"\nversion = "0.1.0"\nsource = {{ git = "{git}" }}\n'
        )
    return "version = 1\n\n" + "\n".join(entries)


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
                "--no-lock",
                "--no-git",
            ]
        )
        == 0
    )
    capsys.readouterr()
    (target / "uv.lock").write_text(lock_text(REV))  # what `uv lock` would record
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


def test_a_tool_is_declared_in_the_code_and_render_lists_it(repo, capsys):
    agent = layout.find(repo, AGENT)
    code = agent.package_dir() / "agent.py"
    code.write_text(
        code.read_text()
        .replace(
            "from agent_runtime.report import report_tool",
            "from agent_runtime.report import report_tool\nfrom agent_runtime.tools import Tool",
        )
        .replace(
            '    return {"tools": [report_tool(ctx.workspace)]}',
            "    async def read_notes() -> dict:\n"
            '        """Read the notes."""\n'
            '        return {"notes": (ctx.workspace / "notes.md").read_text()}\n\n'
            '    return {"tools": [Tool(read_notes, "workspace_read"), '
            "report_tool(ctx.workspace)]}",
        )
    )
    assert run(repo, "render", "--check") == 1
    err = capsys.readouterr().err
    assert f"agents/{AGENT}/agent.yaml: read_notes (workspace_read) is in the code" in err
    assert "run `zetra-agents render`" in err
    assert run(repo, "render") == 0
    assert f"agents/{AGENT}/agent.yaml (tools: rewritten from the code)" in capsys.readouterr().out
    assert agent.manifest()["tools"] == [
        {"name": "read_notes", "effect": "workspace_read"},
        {"name": "write_report", "effect": "workspace_write"},
    ]
    assert run(repo, "render", "--check") == 0
    assert run(repo, "check") == 0
    capsys.readouterr()


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


def test_a_fixture_is_not_asked_for_its_tools_outside_its_image(repo, capsys):
    agent = fixture_agent(repo)
    (agent.package_dir() / "system.md").write_text("fixture\n")
    manifest = {
        "name": agent.name,
        "model": None,
        "entrypoint": "checkpoint_fixture.agent:create_agent",
        "instructions": "checkpoint_fixture/system.md",
        "tools": [{"name": "probe", "effect": "workspace_read"}],
    }
    agent.manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    (agent.package_dir() / "agent.py").write_text(
        "def create_agent(ctx):\n"
        '    raise RuntimeError("checkpoint fixture requires the installed step override")\n'
    )
    before = agent.manifest_path.read_bytes()
    assert layout.check_files(agent) == []
    with pytest.raises(manifest_tools.ToolsError, match="requires the installed step override"):
        manifest_tools.code_tools(agent)
    assert [a.name for a in render.derived_agents(repo)] == [AGENT]
    assert render.tool_problems(repo) == []
    assert render.write_tools(repo) == []
    assert agent.manifest_path.read_bytes() == before


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


def test_the_scaffolded_agent_is_a_working_agent(repo, tmp_path):
    """Laid out as the image's /app, the rendered agent passes the SDK's own manifest and
    tool agreement check, and its tool writes a bounded report."""
    import asyncio

    from zetra_agents import testing

    with testing.loaded(layout.find(repo, AGENT).root, tmp_path / "local") as agent:
        assert "Report ready" in agent.instructions
        assert set(agent.tools) == {"write_report"}
        assert agent.schema("write_report")["function"]["parameters"]["required"] == ["content"]
        assert asyncio.run(agent.call("write_report", content="All good.")) == {"path": "report.md"}
        assert (agent.workspace / "report.md").read_text() == "All good.\n"
        assert "error" in asyncio.run(agent.call("write_report", content="word " * 151))


def test_the_scaffolded_tests_pass_with_the_plugin_fixture(repo, pytester):
    """The rendered tests need nothing but the `agent` fixture this package provides."""
    agent = layout.find(repo, AGENT)
    result = pytester.runpytest_subprocess("-o", "asyncio_mode=auto", "-q", str(agent.root))
    result.assert_outcomes(passed=3)


SAME_CONFIGURATION = "*mode=Mode.AUTO*asyncio_default_fixture_loop_scope=function*"


def test_pytest_at_the_repository_root_runs_the_tests_as_just_check_does(
    repo, pytester, monkeypatch
):
    """Bare `pytest` at the root, `pytest agents/<name>` (the justfile's `check-agent`,
    CI and the release action) and `pytest` inside the agent directory all read the
    rendered root configuration, so the scaffold's async test runs in each."""
    for directory, arguments in (
        (repo, []),
        (repo, ["-q", f"agents/{AGENT}"]),
        (repo / "agents" / AGENT, []),
    ):
        monkeypatch.chdir(directory)
        result = pytester.runpytest_subprocess(*arguments)
        result.assert_outcomes(passed=3)
        if "-q" not in arguments:
            result.stdout.fnmatch_lines([f"rootdir: {repo}", SAME_CONFIGURATION])


def second_agent(repo, name):
    """Another agent in the repository, with the package and test file names of the first:
    each is alone in its image, so nothing keeps the names apart."""
    target = repo / "agents" / name
    shutil.copytree(repo / "agents" / AGENT, target, ignore=shutil.ignore_patterns("__pycache__"))
    for relative in ("agent.yaml", "deploy/agent.yaml", "pyproject.toml"):
        path = target / relative
        path.write_text(path.read_text().replace(AGENT, name))
    return target


WHO_TEST = """
from demo_agent import who

OWN = {name!r}


async def test_imports_find_this_agent(agent_dir):
    import demo_agent.late  # first imported while the test runs

    assert (who.NAME, demo_agent.late.NAME, agent_dir.name) == (OWN,) * 3
"""
WHO_CONFTEST = """
import pytest
from demo_agent import who


@pytest.fixture
def from_conftest():
    return who.NAME
"""
WHO_CONFTEST_TEST = """
def test_the_conftest_imported_this_agent(agent_dir, from_conftest):
    assert from_conftest == agent_dir.name
"""


def test_pytest_at_the_root_gives_each_agent_its_own_modules(repo, pytester, monkeypatch):
    """Agents that name their package and their test files alike are collected and run
    in one process, and every import in a test, at collection, in its conftest and while
    it runs, finds the code of the agent the test belongs to."""
    agents = [repo / "agents" / AGENT, second_agent(repo, "other-agent")]
    assert run(repo, "check") == 0
    for agent in agents:
        (agent / "tests" / "test_agent.py").unlink()  # the session it runs is slow
        for module in ("who", "late"):
            (agent / "src" / "demo_agent" / f"{module}.py").write_text(f"NAME = {agent.name!r}\n")
        (agent / "tests" / "test_who.py").write_text(WHO_TEST.format(name=agent.name))
    monkeypatch.chdir(repo)
    pytester.runpytest_subprocess().assert_outcomes(passed=2)
    pytester.runpytest_subprocess(
        "agents/other-agent/tests/test_who.py", f"agents/{AGENT}/tests/test_who.py"
    ).assert_outcomes(passed=2)
    for agent in agents:
        (agent / "tests" / "conftest.py").write_text(WHO_CONFTEST)
        (agent / "tests" / "test_conftest.py").write_text(WHO_CONFTEST_TEST)
    pytester.runpytest_subprocess().assert_outcomes(passed=4)
    for agent in agents:
        pytester.runpytest_subprocess(f"agents/{agent.name}").assert_outcomes(passed=2)


def test_an_agent_that_keeps_its_own_pytest_configuration_is_tested_as_before(
    repo, pytester, monkeypatch
):
    """Agents written before the root configuration carry their own; `pytest agents/<name>`
    still reads it, and the root run covers them with the root's."""
    agent = repo / "agents" / AGENT
    pyproject = agent / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text()
        + '\n[tool.pytest.ini_options]\nasyncio_mode = "auto"\n'
        + 'asyncio_default_fixture_loop_scope = "function"\n'
        + 'testpaths = ["tests"]\npythonpath = ["src"]\n'
    )
    (agent / "tests" / "test_direct.py").write_text(
        "from demo_agent import agent\n\n\n"
        "async def test_the_package_is_importable():\n    assert agent.create_agent\n"
    )
    monkeypatch.chdir(repo)
    own = pytester.runpytest_subprocess(f"agents/{AGENT}")
    own.assert_outcomes(passed=4)
    own.stdout.fnmatch_lines([f"rootdir: {agent}", SAME_CONFIGURATION])
    root = pytester.runpytest_subprocess()
    root.assert_outcomes(passed=4)
    root.stdout.fnmatch_lines([f"rootdir: {repo}", SAME_CONFIGURATION])


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


def test_check_compiles_every_release_policy_and_refuses_a_write_grant(repo, capsys):
    agent = layout.find(repo, AGENT)
    cr = agent.agent_cr()
    cr["spec"]["capabilities"]["mcp"] = [
        {
            "backend": "kubernetes",
            "effect": "remote_write",
            "tools": ["pods_delete"],
            "delegation": "inherit",
        }
    ]
    agent.agent_cr_path.write_text(yaml.safe_dump(cr, sort_keys=False))
    assert layout.check(agent) == []  # the layout check alone does not see the grant
    assert run(repo, "check") == 1
    err = capsys.readouterr().err
    assert "release policy does not compile" in err or "only remote_read" in err


def test_approval_does_not_change_the_policy_digest(repo):
    import datetime as dt

    from agent_platform.bundle import compile_bundle, digest_hex

    agent = layout.find(repo, AGENT)
    a = release.build(
        agent,
        image=IMAGE,
        revision="abc",
        mcp_resource_base=BASE,
        trust_domain="cluster.local",
        date=dt.date(2026, 10, 30),
    )
    approved = {**a["release"], "spec": {**a["release"]["spec"], "approval": {"approved": True}}}
    bundle = compile_bundle(
        a["agent"], approved, mcp_resource_base=BASE, trust_domain="cluster.local"
    )
    assert digest_hex(bundle) == a["release"]["spec"]["policySha256"]
    assert a["predicate"]["policySha256"] == a["release"]["spec"]["policySha256"]
    assert a["name"] == f"{AGENT}-2026-10-30-a9a9a9a9"


@pytest.mark.parametrize("image", [f"registry.almckay.io/agents/{AGENT}:latest", "x@sha256:ab"])
def test_release_requires_a_digest_pinned_image(image):
    import datetime as dt

    with pytest.raises(ValueError):
        release.release_name(AGENT, image, dt.date(2026, 10, 30))


@pytest.mark.parametrize(
    ("agent", "owner", "description"),
    [
        ("1x", "dev@example.com", "Demo"),
        ("class", "dev@example.com", "Demo"),
        ("mcp", "dev@example.com", "Demo"),
        ("ok-agent", "not an email", "Demo"),
        ("ok-agent", "dev@example.com", 'Reads "logs": summarises'),
        ("ok-agent", "dev@example.com", "a: b"),
    ],
)
def test_init_refuses_values_that_would_not_render_safely(tmp_path, agent, owner, description):
    with pytest.raises(render.RenderError):
        render.init(
            tmp_path / "x",
            agent=agent,
            revision=REV,
            tenant="tenant-test",
            owner=owner,
            description=description,
        )
    assert not (tmp_path / "x").exists()


def test_render_check_verifies_the_lock_and_member_sources(repo, capsys):
    (repo / "uv.lock").write_text(lock_text(REV))
    assert run(repo, "render", "--check") == 0
    capsys.readouterr()
    fork = (
        "https://github.com/someone/zetra.git?subdirectory=platform%2Fagent-runtime&rev="
        + "f" * 40
        + "#"
        + "f" * 40
    )
    (repo / "uv.lock").write_text(lock_text(REV, {"agent-runtime": fork}))
    assert run(repo, "render", "--check") == 1
    assert "agent-runtime is locked from" in capsys.readouterr().err
    (repo / "uv.lock").write_text(lock_text(REV))
    member = repo / "agents" / AGENT / "pyproject.toml"
    member.write_text(
        member.read_text()
        + '\n[tool.uv.sources]\nagent-runtime = { git = "https://example.invalid/x" }\n'
    )
    assert run(repo, "render", "--check") == 1
    assert "[tool.uv.sources] in a member" in capsys.readouterr().err


def test_installed_revision_requires_the_zetra_origin_exactly(monkeypatch):
    class Dist:
        def __init__(self, url):
            self.url = url

        def read_text(self, name):
            return json.dumps({"url": self.url, "vcs_info": {"vcs": "git", "commit_id": REV}})

    for url, ok in (
        ("https://github.com/X-McKay/zetra.git", True),
        ("https://github.com/X-McKay/zetra", True),
        ("git+https://github.com/X-McKay/zetra.git", True),
        ("https://github.com/X-McKay/zetra.github.io", False),
        ("https://github.com/X-McKay/zetra.git.evil.example/", False),
    ):
        monkeypatch.setattr(render.metadata, "distribution", lambda name, url=url: Dist(url))
        assert (render.installed_revision() == REV) is ok, url


def test_src_may_hold_only_the_package(repo):
    agent = layout.find(repo, AGENT)
    (agent.src / "openai.py").write_text("shadow = True\n")
    assert any("holds more than the package: openai.py" in p for p in layout.check(agent))


def test_init_bootstraps_git_and_reports_the_lock(tmp_path, capsys, monkeypatch):
    target = tmp_path / "repo"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    assert (
        cli.main(
            [
                "init",
                str(target),
                "--name",
                "my-agent",
                "--owner",
                "dev@example.com",
                "--zetra-rev",
                REV,
                "--no-lock",
            ]
        )
        == 0
    )
    err = capsys.readouterr().err
    assert (target / ".git").is_dir()
    assert "lock: skipped" in err and "git: initialised" in err
    assert (
        "uv lock" in err and "just agent register tenant-test" in err and "my-agent/v0.1.0" in err
    )
    assert "git add -A" in err


def test_next_steps_do_not_tell_a_nested_checkout_to_commit(tmp_path):
    text = render.next_steps(
        tmp_path / "a b",
        "x",
        "tenant-test",
        {"lock": "locked", "git": "already inside a repository"},
    )
    assert "git add -A" not in text and "git was not initialised" in text
    assert "cd '" in text and "a b'" in text  # shell-quoted path
    assert "uv lock" not in text


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("my-agent", "db-watch"),
        ("watcher", "db-watch"),
        ("agent", "db-watch"),
        ("a", "ab"),
        ("my-agent", "agent"),
    ],
)
def test_rename_edits_only_the_known_fields(tmp_path, capsys, monkeypatch, old, new):
    monkeypatch.setattr(render, "bootstrap", lambda *a, **k: {"lock": "skipped", "git": "skipped"})
    target = tmp_path / "repo"
    argv = ["init", str(target), "--name", old, "--owner", "dev@example.com", "--zetra-rev", REV]
    assert cli.main([*argv, "--no-lock", "--no-git"]) == 0
    (target / "uv.lock").write_text(lock_text(REV))
    readme_before = (target / "README.md").read_text()
    # The starter tests name the agent only through the `agent` fixture; tests that import
    # the package, as older ones do, are followed too.
    old_package = old.replace("-", "_")
    with (target / "agents" / old / "tests/test_agent.py").open("a") as tests:
        tests.write(f"\nfrom {old_package}.agent import create_agent  # noqa\n")
        tests.write(f'PACKAGE = "{old_package}"\n')
    capsys.readouterr()
    assert cli.main(["--repo", str(target), "rename", old, new]) == 0
    assert f"agents/{new}" in capsys.readouterr().out
    agent = layout.find(target, new)
    new_package = new.replace("-", "_")
    assert agent.package_dir().name == new_package
    manifest = agent.manifest()
    assert manifest["name"] == new
    assert manifest["entrypoint"] == f"{new_package}.agent:create_agent"
    assert manifest["instructions"] == f"{new_package}/instructions/base.md"
    cr = agent.agent_cr()
    assert cr["metadata"]["name"] == new and cr["spec"]["identity"] == {"mode": "agent"}
    pyproject = (agent.root / "pyproject.toml").read_text()
    assert f'name = "zetra-agent-{new}"' in pyproject and '"agent-runtime"' in pyproject
    tests = (agent.root / "tests/test_agent.py").read_text()
    assert f'PACKAGE = "{new_package}"' in tests and f"from {new_package}.agent import" in tests
    assert "async def test_report_is_bounded(agent):" in tests
    assert (agent.package_dir() / "instructions/base.md").read_text().startswith(f"You are {new}.")
    assert (target / "README.md").read_text() == readme_before  # prose is left to the user
    assert layout.check(agent) == []
    assert run(target, "render", "--check") == 0
    capsys.readouterr()


def test_rename_refusals_leave_the_agent_untouched(repo, capsys, monkeypatch):
    monkeypatch.setattr(render, "bootstrap", lambda *a, **k: {"lock": "skipped", "git": "skipped"})
    before = sorted(str(p.relative_to(repo)) for p in (repo / "agents").rglob("*"))
    for old, new in ((AGENT, "1bad"), ("missing", "ok"), (AGENT, AGENT), (AGENT, "mcp")):
        assert cli.main(["--repo", str(repo), "rename", old, new]) == 2
    (repo / "agents" / "taken").mkdir()
    assert cli.main(["--repo", str(repo), "rename", AGENT, "taken"]) == 2
    (repo / "agents" / "taken").rmdir()
    assert sorted(str(p.relative_to(repo)) for p in (repo / "agents").rglob("*")) == before
    capsys.readouterr()
