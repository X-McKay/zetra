"""The local agent loader holds an agent to its manifest through the SDK, and the scripted
MCP answers only what the Agent grants."""

import asyncio
import os
import sys
import textwrap

import pytest
import yaml
from agent_runtime import mcp as runtime_mcp
from agent_runtime.gateway import PolicyDenied

from zetra_agents import testing
from zetra_agents.layout import AgentDir

AGENT_CODE = '''
from agent_runtime.tools import Tool


def create_agent(ctx) -> dict:
    async def list_pods(namespace: str = "demo") -> dict:
        """List pods."""
        return await ctx.mcp("kubernetes", "pods_list", {"namespace": namespace})

    async def delete_pod(name: str) -> dict:
        """Not granted."""
        try:
            return await ctx.mcp("kubernetes", "pods_delete", {"name": name})
        except Exception as exc:
            return {"error": type(exc).__name__}

    async def note(text: str) -> dict:
        """Write a note, and wrongly read remotely while doing it."""
        await ctx.mcp("kubernetes", "pods_list", {})
        (ctx.workspace / "note.txt").write_text(text)
        return {"path": "note.txt"}

    return {
        "tools": [
            Tool(list_pods, "remote_read"),
            Tool(delete_pod, "remote_read"),
            Tool(note, "workspace_write"),
        ]
    }
'''
TOOLS = [
    {"name": "list_pods", "effect": "remote_read"},
    {"name": "delete_pod", "effect": "remote_read"},
    {"name": "note", "effect": "workspace_write"},
]


@pytest.fixture
def agent_dir(tmp_path):
    root = tmp_path / "repo" / "agents" / "probe"
    (root / "src" / "probe_agent" / "instructions").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "src" / "probe_agent" / "__init__.py").write_text("")
    (root / "src" / "probe_agent" / "agent.py").write_text(AGENT_CODE)
    (root / "src" / "probe_agent" / "instructions" / "base.md").write_text("Probe.\n")
    (root / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "probe",
                "entrypoint": "probe_agent.agent:create_agent",
                "instructions": "probe_agent/instructions/base.md",
                "model": None,
                "tools": TOOLS,
            }
        )
    )
    (root / "deploy" / "agent.yaml").write_text(
        textwrap.dedent(
            """
            apiVersion: agentplatform.io/v1alpha1
            kind: Agent
            metadata: {name: probe, namespace: tenant-test}
            spec:
              capabilities:
                models: []
                mcp:
                  - {backend: kubernetes, effect: remote_read, tools: [pods_list]}
            """
        )
    )
    return root


def test_loaded_builds_the_agent_from_the_image_layout_and_restores_the_process(
    agent_dir, tmp_path
):
    path, real, environment = list(sys.path), runtime_mcp.call_tool, dict(os.environ)
    with testing.loaded(agent_dir, tmp_path / "local") as agent:
        assert agent.name == "probe" and agent.instructions == "Probe.\n"
        assert set(agent.tools) == {"list_pods", "delete_pod", "note"}
        assert (agent.app / "agent.yaml").is_file() and (agent.app / "probe_agent").is_dir()
        assert agent.workspace == tmp_path / "local" / "sandbox" / "workspace"
        assert agent.workspace.is_dir()
        assert agent.schema("list_pods")["function"]["parameters"]["required"] == []
        assert sys.modules["probe_agent.agent"].__file__.startswith(str(agent.app))
        with pytest.raises(KeyError, match="has no tool 'missing'"):
            asyncio.run(agent.call("missing"))
    assert sys.path == path and runtime_mcp.call_tool is real and dict(os.environ) == environment
    assert "probe_agent" not in sys.modules and "probe_agent.agent" not in sys.modules


def test_a_manifest_that_disagrees_with_the_code_fails_as_it_does_in_the_sandbox(
    agent_dir, tmp_path
):
    manifest = yaml.safe_load((agent_dir / "agent.yaml").read_text())
    manifest["tools"] = TOOLS[:2]
    (agent_dir / "agent.yaml").write_text(yaml.safe_dump(manifest))
    with (
        pytest.raises(ValueError, match="do not match its manifest"),
        testing.loaded(agent_dir, tmp_path / "local"),
    ):
        pass
    assert "probe_agent.agent" not in sys.modules


def test_scripted_replies_are_shaped_like_the_gateway_client(agent_dir, tmp_path):
    with testing.loaded(agent_dir, tmp_path / "local") as agent:
        agent.mcp.reply("pods_list", "demo-1 Running")
        assert asyncio.run(agent.call("list_pods")) == {
            "tool": "pods_list",
            "backend": "kubernetes",
            "text": "demo-1 Running",
        }
        assert agent.mcp.calls == [("kubernetes", "pods_list", {"namespace": "demo"})]
        agent.mcp.reply("pods_list", lambda arguments: {"namespace": arguments["namespace"]})
        assert asyncio.run(agent.call("list_pods", namespace="other"))["text"] == (
            '{"namespace": "other"}'
        )
        agent.mcp.reply("pods_list", "x" * 70_000, backend="kubernetes")
        assert len(asyncio.run(agent.call("list_pods"))["text"]) == runtime_mcp.MAX_RESULT
        # A reply for one backend takes precedence over a reply for the tool on any.
        agent.mcp.reply("pods_list", "any backend")
        assert len(asyncio.run(agent.call("list_pods"))["text"]) == runtime_mcp.MAX_RESULT
        agent.mcp.reply("pods_list", RuntimeError("returned an error"), backend="kubernetes")
        with pytest.raises(RuntimeError):
            asyncio.run(agent.call("list_pods"))
        assert agent.mcp.problems() == []


def test_arguments_are_checked_as_the_real_client_checks_them():
    mcp = testing.FakeMcp({"kubernetes": {"pods_list"}}).reply("pods_list", "ok")
    with pytest.raises(ValueError, match="backend"):
        asyncio.run(mcp("Kubernetes", "pods_list", {}))
    with pytest.raises(TypeError):
        asyncio.run(mcp("kubernetes", "pods_list", ["demo"]))
    with pytest.raises(ValueError, match="32 KiB"):
        asyncio.run(mcp("kubernetes", "pods_list", {"x": "y" * 40_000}))
    assert mcp.calls == []


def test_an_ungranted_call_is_refused_and_reported_even_when_the_agent_swallows_it(
    agent_dir, tmp_path
):
    with testing.loaded(agent_dir, tmp_path / "local") as agent:
        assert asyncio.run(agent.call("delete_pod", name="demo-1")) == {"error": "PolicyDenied"}
        assert agent.mcp.denied == [("kubernetes", "pods_delete", {"name": "demo-1"})]
        (problem,) = agent.mcp.problems()
    assert "probe called MCP kubernetes/pods_delete" in problem
    assert "add `pods_delete` to the tools of the `kubernetes` entry" in problem
    stranger = testing.FakeMcp({})
    with pytest.raises(PolicyDenied):
        asyncio.run(stranger("github", "issues_list", {}))
    (problem,) = stranger.problems()
    assert "add `{backend: github, effect: remote_read, tools: [issues_list]}`" in problem


def test_an_unscripted_call_and_a_misdeclared_effect_are_reported(agent_dir, tmp_path):
    with testing.loaded(agent_dir, tmp_path / "local") as agent:
        with pytest.raises(testing.UnscriptedCall):
            asyncio.run(agent.call("list_pods"))
        agent.mcp.reply("pods_list", "ok")
        asyncio.run(agent.call("note", text="hello"))
        problems = agent.mcp.problems()
    assert any('agent.mcp.reply("pods_list"' in p for p in problems)
    assert any("tool note is declared workspace_write but called MCP" in p for p in problems)
    assert len(problems) == 2


def test_grants_are_read_per_backend():
    cr = {
        "spec": {
            "capabilities": {
                "mcp": [
                    {"backend": "kubernetes", "tools": ["pods_get"]},
                    {"backend": "kubernetes", "tools": ["events_list"]},
                    {"backend": "github", "tools": []},
                ]
            }
        }
    }
    assert testing.mcp_grants(cr) == {"kubernetes": {"pods_get", "events_list"}, "github": set()}
    assert testing.mcp_grants({"spec": {"capabilities": {"mcp": []}}}) == {}


PLUGIN_TEST = """
import pytest


async def test_scripted(agent):
    agent.mcp.reply("pods_list", "ok")
    assert (await agent.call("list_pods"))["text"] == "ok"


async def test_swallowed_denial(agent):
    assert await agent.call("delete_pod", name="x") == {"error": "PolicyDenied"}


async def test_expected_denial(agent):
    agent.mcp.strict = False
    assert await agent.call("delete_pod", name="x") == {"error": "PolicyDenied"}


def test_directory(agent_dir, agent):
    assert agent_dir.name == "probe" and agent.dir.root == agent_dir
"""


def test_the_fixture_fails_a_test_whose_agent_made_an_ungranted_call(agent_dir, pytester):
    (agent_dir / "tests").mkdir()
    (agent_dir / "tests" / "test_probe.py").write_text(PLUGIN_TEST)
    result = pytester.runpytest_subprocess("-o", "asyncio_mode=auto", "-q", str(agent_dir))
    result.assert_outcomes(passed=4, errors=1)
    result.stdout.fnmatch_lines(["*probe called MCP kubernetes/pods_delete*does not*"])


def test_the_fixture_needs_an_agent_directory(pytester):
    pytester.makepyfile("def test_x(agent):\n    pass\n")
    result = pytester.runpytest_subprocess("-q")
    result.stdout.fnmatch_lines(["*is not under an agent directory*"])
    assert result.ret != 0


# The manifest's tool list is derived from the code (zetra_agents.manifest).


def test_code_tools_are_read_in_declaration_order(agent_dir):
    from zetra_agents import manifest

    assert manifest.code_tools(AgentDir(agent_dir)) == TOOLS
    assert manifest.difference(AgentDir(agent_dir)) is None
    assert manifest.write(AgentDir(agent_dir)) is False


def test_write_replaces_only_the_tools_list(agent_dir):
    from zetra_agents import manifest

    agent = AgentDir(agent_dir)
    agent.manifest_path.write_text(
        textwrap.dedent(
            """\
            # A comment that stays.
            schema_version: 1
            name: probe
            entrypoint: probe_agent.agent:create_agent
            tools:
              - name: list_pods
                effect: workspace_read   # wrong

              - name: gone
                effect: remote_read

            instructions: probe_agent/instructions/base.md
            model: null   # deterministic
            """
        )
    )
    detail = manifest.difference(agent)
    assert "delete_pod (remote_read) is in the code but not listed" in detail
    assert "gone is listed but not in the code" in detail
    assert "list_pods is remote_read in the code but listed workspace_read" in detail
    assert manifest.write(agent) is True
    assert agent.manifest_path.read_text() == textwrap.dedent(
        """\
        # A comment that stays.
        schema_version: 1
        name: probe
        entrypoint: probe_agent.agent:create_agent
        tools:
          - name: list_pods
            effect: remote_read
          - name: delete_pod
            effect: remote_read
          - name: note
            effect: workspace_write

        instructions: probe_agent/instructions/base.md
        model: null   # deterministic
        """
    )
    assert manifest.write(agent) is False


@pytest.mark.parametrize(
    "tools_text",
    [
        "",
        "tools: []\n",
        "tools:\n- name: note\n  effect: workspace_write\n",
        "tools: [{name: note, effect: workspace_write}]",
    ],
)
def test_with_tools_handles_every_way_the_list_is_written(tools_text):
    from zetra_agents import manifest

    text = "name: probe\n" + tools_text
    updated = manifest.with_tools(text, TOOLS[:1])
    assert updated == "name: probe\ntools:\n  - name: list_pods\n    effect: remote_read\n"
    assert manifest.with_tools(updated, []) == "name: probe\ntools: []\n"


def test_with_tools_refuses_what_it_cannot_rewrite_in_place():
    from zetra_agents import manifest

    with pytest.raises(manifest.ToolsError, match="edit it by hand"):
        manifest.with_tools(
            "tools:\n  - name: a\n# a comment at the margin\n  - name: b\n", TOOLS[:1]
        )
    with pytest.raises(manifest.ToolsError, match="more than one"):
        manifest.with_tools("tools: []\ntools: []\n", TOOLS[:1])


def test_code_that_cannot_be_loaded_is_reported_with_its_error(agent_dir):
    from zetra_agents import manifest

    (agent_dir / "src" / "probe_agent" / "agent.py").write_text("import not_installed_anywhere\n")
    with pytest.raises(manifest.ToolsError, match="ModuleNotFoundError.*just setup"):
        manifest.code_tools(AgentDir(agent_dir))
    (agent_dir / "src" / "probe_agent" / "agent.py").write_text(
        AGENT_CODE.replace("Tool(note,", "Tool(list_pods,")
    )
    with pytest.raises(manifest.ToolsError, match="same name"):
        manifest.code_tools(AgentDir(agent_dir))
