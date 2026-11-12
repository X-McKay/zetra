"""An agent that declares `model: null` supplies `decide`: scaffold, check, test kit, local run."""

import json
import shutil

import pytest

from zetra_agents import cli, layout, local, render, testing
from zetra_agents import manifest as manifest_tools

REV = "0123456789abcdef0123456789abcdef01234567"
AGENT = "free-agent"


def lock_text():
    entries = []
    for name, sub in render.PINNED.items():
        git = f"{render.ZETRA_GIT}?subdirectory={sub.replace('/', '%2F')}&rev={REV}#{REV}"
        entries.append(
            f'[[package]]\nname = "{name}"\nversion = "0.1.0"\nsource = {{ git = "{git}" }}\n'
        )
    return "version = 1\n\n" + "\n".join(entries)


def init(tmp_path, *extra, name=AGENT):
    target = tmp_path / "repo"
    argv = ["init", str(target), "--agent", name, "--owner", "dev@example.com"]
    argv += ["--zetra-rev", REV, "--no-lock", "--no-git", *extra]
    assert cli.main(argv) == 0
    (target / "uv.lock").write_text(lock_text())  # what `uv lock` would record
    return target


@pytest.fixture
def free(tmp_path, capsys):
    target = init(tmp_path, "--model-free")
    capsys.readouterr()
    return layout.find(target, AGENT)


@pytest.fixture
def backed(tmp_path, capsys):
    target = init(tmp_path)
    capsys.readouterr()
    return layout.find(target, AGENT)


def check(agent, capsys):
    code = cli.main(["--repo", str(agent.repo), "check", agent.name])
    return code, capsys.readouterr().err


def module(agent):
    return agent.package_dir() / "agent.py"


def test_the_model_free_scaffold_declares_no_model_and_passes_check(free, capsys):
    manifest = free.manifest()
    assert manifest["model"] is None
    assert free.agent_cr()["spec"]["capabilities"]["models"] == []
    assert "decide" in module(free).read_text()
    assert check(free, capsys) == (0, "")


def test_the_default_scaffold_stays_model_backed(backed, capsys):
    assert isinstance(backed.manifest()["model"], dict)
    assert "decide" not in module(backed).read_text()
    assert check(backed, capsys) == (0, "")


def test_check_asks_a_model_free_agent_for_its_decide(free, capsys):
    module(free).write_text(module(free).read_text().replace('"decide": decide', '"x": 1'))
    code, err = check(free, capsys)
    assert code == 1
    assert "declares model: null but create_agent returns no decide function" in err
    assert 'add `"decide": decide`' in err


def test_check_refuses_a_decide_beside_a_model(backed, capsys):
    path = module(backed)
    path.write_text(
        path.read_text().replace(
            'return {"tools": [report_tool(ctx.workspace)]}',
            'return {"tools": [report_tool(ctx.workspace)], "decide": lambda t, r: None}',
        )
    )
    assert "decide" in path.read_text()
    code, err = check(backed, capsys)
    assert code == 1
    assert "returns a decide function but agent.yaml declares a model" in err
    assert "remove `decide`" in err


def test_check_reads_no_decide_from_an_agent_without_code(backed, capsys):
    shutil.rmtree(backed.src)
    assert manifest_tools.decide_problems(backed) == []
    code, err = check(backed, capsys)  # `check` itself must end, not raise
    assert code in (0, 1)
    assert "Traceback" not in err


def test_a_fixture_is_not_read_for_its_decide(free):
    copy = free.repo / "agents" / "conformance-checkpoint"
    shutil.copytree(free.root, copy)
    path = copy / "src" / AGENT.replace("-", "_") / "agent.py"
    path.write_text(path.read_text().replace('"decide": decide', '"x": 1'))
    assert cli.code_problems(free) == []  # sanity: the model-free scaffold itself is fine
    assert manifest_tools.decide_problems(layout.AgentDir(copy)) != []
    assert cli.code_problems(layout.AgentDir(copy)) == []


def test_the_kit_drives_a_model_free_agent_through_decide(free, tmp_path):
    import asyncio

    async def session(agent):
        return await agent.session({"request": "go"})

    with testing.loaded(free, tmp_path / "local") as agent:
        assert agent.decide is not None
        result = asyncio.run(session(agent))
        assert [c.name for c in result.calls] == ["write_report"]
        assert result.calls[0].result == {"path": "report.md"}
        assert result.summary == "Report ready."


def test_a_model_backed_agent_has_nothing_to_decide(backed, tmp_path):
    import asyncio

    with testing.loaded(backed, tmp_path / "local") as agent:
        assert agent.decide is None
        with pytest.raises(TypeError, match="declares a model"):
            asyncio.run(agent.session({}))


@pytest.mark.parametrize(
    "decision,message",
    [
        ("raise RuntimeError('x')", "decide raised RuntimeError"),
        ("return 'write_report'", "decide returned str, not a ToolCall or a Done"),
        ("return ToolCall('nope', {})", "named a tool the agent does not have (nope)"),
    ],
)
def test_the_kit_reports_agent_errors_as_the_sandbox_does(free, tmp_path, decision, message):
    import asyncio

    path = module(free)
    text = path.read_text()
    start = text.index("    def decide")
    end = text.index("    return {")
    path.write_text(
        text[:start] + f"    def decide(task, results):\n        {decision}\n\n" + text[end:]
    )
    path.write_text("from agent_runtime.tools import ToolCall\n" + path.read_text())

    async def session(agent):
        return await agent.session({"request": "go"})

    with (
        testing.loaded(free, tmp_path / "local") as agent,
        pytest.raises(
            testing.AgentError,
            match="agent error: .*" + message.replace("(", r"\(").replace(")", r"\)"),
        ),
    ):
        asyncio.run(session(agent))


def test_the_scaffolded_tests_pass_with_the_plugin_fixture(free, pytester):
    result = pytester.runpytest_subprocess("-o", "asyncio_mode=auto", "-q", str(free.root))
    result.assert_outcomes(passed=4)


def test_a_local_run_drives_decide_without_a_model(free, tmp_path):
    seen = []
    result = local.run(free, "smoke", out=tmp_path / "run", on_boundary=seen.append)
    assert result.ok, (result.outcome, result.detail, result.problems)
    assert result.model == "none"
    # decide, the tool, decide: no boundary reports a model call or any usage
    assert [(b["kind"], b["tool"], b["usage"]) for b in seen] == [
        ("tool", None, None),
        ("tool", "write_report", None),
        ("tool", None, None),
    ]
    assert result.document()["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert result.report.read_text() == "The starter agent ran without a model.\n"


def test_a_model_free_agent_takes_no_model_script_and_no_live(free, tmp_path):
    scripted = local.Scenario(task="x", model=({"say": "hi"},))
    with pytest.raises(local.ScenarioError, match="declares no model"):
        local.run(free, scripted, out=tmp_path / "a")
    with pytest.raises(local.ScenarioError, match="no --live"):
        local.run(free, local.Scenario(task="x"), live=True, out=tmp_path / "b")


def test_a_failing_decide_ends_a_local_run_as_an_agent_error(free, tmp_path):
    path = module(free)
    path.write_text(
        path.read_text().replace(
            "if not results:",
            "if task:\n            raise ValueError('secret-in-task')\n        if not results:",
        )
    )
    result = local.run(free, local.Scenario(task="secret-in-task"), out=tmp_path / "run")
    assert result.outcome == "agent_error"
    assert "decide raised ValueError" in result.detail
    assert "secret-in-task" not in result.detail
    assert "secret-in-task" not in json.dumps(result.document()["detail"])


def test_run_cli_names_the_missing_model(free, capsys, tmp_path):
    code = cli.main(["--repo", str(free.repo), "run", AGENT, "--out", str(tmp_path / "run")])
    assert code == 0
    assert "no model (decide)" in capsys.readouterr().err
