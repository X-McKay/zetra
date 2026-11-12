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


# --- review findings: what `check` and the kit say about decide --------------------------------


def replace_decide(agent, text):
    path = module(agent)
    path.write_text(path.read_text().replace('"decide": decide', text))


def test_check_refuses_a_decide_that_is_not_callable(free, capsys):
    replace_decide(free, '"decide": 1')
    code, err = check(free, capsys)
    assert code == 1
    assert "decide create_agent returns is not callable" in err
    assert "plain function taking (task, results)" in err


def test_check_refuses_an_async_decide(free, capsys):
    path = module(free)
    path.write_text(path.read_text().replace("    def decide(", "    async def decide("))
    assert "async def decide" in path.read_text()
    code, err = check(free, capsys)
    assert code == 1
    assert "is an async function" in err and "plain `def decide(task, results)`" in err


def test_check_refuses_a_decide_object_with_an_async_call(free, capsys):
    path = module(free)
    text = path.read_text().replace('"decide": decide', '"decide": AsyncDecide()')
    path.write_text(
        "class AsyncDecide:\n    async def __call__(self, task, results):\n        return None\n\n"
        + text
    )
    code, err = check(free, capsys)
    assert code == 1
    assert "is an async function" in err and "plain `def decide(task, results)`" in err


def unreadable_file(agent):
    import os

    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    path = agent.package_dir() / "notes.txt"
    path.write_text("x")
    path.chmod(0)
    return path


def test_check_does_not_crash_when_the_source_cannot_be_copied(backed, capsys):
    path = unreadable_file(backed)
    try:
        with pytest.raises(manifest_tools.ToolsError, match="cannot copy src/") as caught:
            manifest_tools.read_code(backed)
        assert "\n" not in str(caught.value)
        code, err = check(backed, capsys)  # a model-backed agent has nothing to confirm
        assert "Traceback" not in err and code in (0, 1)
    finally:
        path.chmod(0o644)


def test_check_says_a_model_free_agent_could_not_be_confirmed(free, capsys):
    path = unreadable_file(free)
    try:
        code, err = check(free, capsys)
    finally:
        path.chmod(0o644)
    assert code == 1 and "Traceback" not in err
    assert "agent.yaml declares model: null, but the code could not be read" in err
    assert "cannot copy src/" in err and "could not be confirmed" in err


def test_unimportable_code_is_reported_for_a_model_free_agent(free, capsys):
    module(free).write_text("raise RuntimeError('does not import')\n")
    code, err = check(free, capsys)
    assert code == 1 and "the code could not be read" in err and "does not import" in err


def test_unimportable_code_is_not_a_decide_problem_for_a_model_backed_agent(backed, capsys):
    module(backed).write_text("raise RuntimeError('does not import')\n")
    assert manifest_tools.decide_problems(backed) == []
    assert check(backed, capsys) == (0, "")


def test_a_fixture_whose_code_cannot_be_read_stays_exempt(free):
    copy = free.repo / "agents" / "conformance-checkpoint"
    shutil.copytree(free.root, copy)
    (copy / "src" / AGENT.replace("-", "_") / "agent.py").write_text("raise RuntimeError('x')\n")
    assert cli.code_problems(layout.AgentDir(copy)) == []


def test_the_kit_loads_a_fixture_without_decide_and_refuses_an_ordinary_agent(free, tmp_path):
    copy = free.repo / "agents" / "conformance-checkpoint"
    shutil.copytree(free.root, copy)
    fixture = layout.AgentDir(copy)
    replace_decide(fixture, '"x": 1')
    with testing.loaded(fixture, tmp_path / "fixture") as agent:
        assert agent.decide is None and "write_report" in agent.tools
    replace_decide(free, '"x": 1')
    with (
        pytest.raises(ValueError, match="must also return a decide"),
        testing.loaded(free, tmp_path / "ordinary"),
    ):
        pass


def test_session_runs_tools_through_run_tool_so_decide_sees_a_failure(free, tmp_path):
    import asyncio

    path = module(free)
    text = path.read_text()
    start = text.index("    def decide")
    end = text.index("    return {")
    decide = (
        "    def decide(task, results):\n"
        "        if not results:\n"
        "            return ToolCall('write_report', {'content': 'x', 'extra': 'unexpected'})\n"
        "        return Done('saw ' + repr(results[0].result))\n\n"
    )
    path.write_text(
        "from agent_runtime.tools import Done, ToolCall\n" + text[:start] + decide + text[end:]
    )

    async def session(agent):
        return await agent.session({"request": "go"})

    with testing.loaded(free, tmp_path / "local") as agent:
        result = asyncio.run(session(agent))  # the tool's TypeError must not propagate
    assert result.calls[0].result == {"error": "TypeError"}
    assert result.summary == "saw {'error': 'TypeError'}"
