"""`zetra-agents run` drives the SDK's own boundary driver through a whole session, with
the scenario standing in for the model and for MCP behind the Agent's grants."""

import contextlib
import datetime
import http.server
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from typing import ClassVar

import pytest
import yaml

from zetra_agents import cli, layout, local, local_step

AGENT_CODE = '''
from agent_runtime.report import report_tool
from agent_runtime.tools import Tool


def create_agent(ctx) -> dict:
    async def list_pods(namespace: str) -> dict:
        """List pods."""
        return await ctx.mcp("kubernetes", "pods_list", {"namespace": namespace})

    async def list_events(namespace: str) -> dict:
        """List events."""
        return await ctx.mcp("kubernetes", "events_list", {"namespace": namespace})

    async def read_secret(name: str) -> dict:
        """Not granted."""
        return await ctx.mcp("kubernetes", "secrets_get", {"name": name})

    async def poke() -> dict:
        """What this process may write and what it was handed."""
        import os, pathlib

        def writes(path) -> bool:
            try:
                pathlib.Path(path).write_text("x")
            except OSError:
                return False
            return True

        return {
            "app": writes(pathlib.Path(__file__).with_name("scratch")),
            "cwd": writes("scratch"),
            "workspace": writes(ctx.workspace / "scratch"),
            "environment": sorted(os.environ),
        }

    async def bail() -> dict:
        """A tool that ends the process the way a command-line parser inside it would."""
        (ctx.workspace / "report.md").write_text("Written, then gone.")
        raise SystemExit(2)

    async def forge(record: str, code: int = 2) -> dict:
        """Prints its own result where the driver prints one, then ends the process."""
        import os

        (ctx.workspace / "report.md").write_text("Written, then gone.")
        os.write(1, record.encode())
        os._exit(code)

    async def nap() -> dict:
        """Says which process it is, then outlives any test."""
        import os, time

        (ctx.workspace / "pid").write_text(str(os.getpid()))
        time.sleep(120)
        return {}

    return {
        "tools": [
            Tool(list_pods, "remote_read"),
            Tool(list_events, "remote_read"),
            Tool(read_secret, "remote_read"),
            Tool(poke, "workspace_write"),
            Tool(bail, "workspace_write"),
            Tool(forge, "workspace_write"),
            Tool(nap, "workspace_write"),
            report_tool(ctx.workspace),
        ]
    }
'''
TOOLS = [
    {"name": "list_pods", "effect": "remote_read"},
    {"name": "list_events", "effect": "remote_read"},
    {"name": "read_secret", "effect": "remote_read"},
    {"name": "poke", "effect": "workspace_write"},
    {"name": "bail", "effect": "workspace_write"},
    {"name": "forge", "effect": "workspace_write"},
    {"name": "nap", "effect": "workspace_write"},
    {"name": "write_report", "effect": "workspace_write"},
]
REPORT = {"call": "write_report", "arguments": {"content": "Two pods, both running."}}
# What the driver prints for a session's second boundary when it is the last.
RESULT = {
    "boundary": 1,
    "kind": "tool",
    "done": True,
    "tool": "forge",
    "usage": None,
    "summary": "",
}
PODS = {"tool": "pods_list", "backend": "kubernetes", "result": {"items": ["a", "b"]}}
SCENARIO = {
    "task": "How many pods are in shop?",
    "mcp": [PODS],
    "model": [
        {"call": "list_pods", "arguments": {"namespace": "shop"}},
        REPORT,
        {"say": "Report ready."},
    ],
}


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / "repo" / "agents" / "probe"
    (root / "src" / "probe_agent" / "instructions").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "scenarios").mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "zetra-agent-probe"\n')
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
                "model": {"backend": "llm-main", "name": "platform-model", "max_tokens": 64},
                "tools": TOOLS,
            },
            sort_keys=False,
        )
    )
    (root / "deploy" / "agent.yaml").write_text(
        textwrap.dedent(
            """
            apiVersion: agentplatform.io/v1alpha1
            kind: Agent
            metadata: {name: probe, namespace: tenant-test}
            spec:
              owner: dev@example.com
              identity: {mode: agent}
              capabilities:
                models:
                  - backend: llm-main
                    models: [platform-model]
                    budget: {tokensPerDay: 1000, tokensPerSession: 100}
                mcp:
                  - {backend: kubernetes, effect: remote_read, tools: [pods_list, events_list]}
            """
        )
    )
    (root / "scenarios" / "smoke.yaml").write_text(yaml.safe_dump(SCENARIO, sort_keys=False))
    return layout.AgentDir(root)


def scenario(**changes):
    return local.parse_scenario({**SCENARIO, **changes})


def test_a_scripted_session_runs_every_boundary_through_the_sdk_driver(agent, tmp_path):
    seen = []
    result = local.run(agent, "smoke", out=tmp_path / "run", on_boundary=seen.append)
    assert result.ok, (result.outcome, result.detail, result.problems)
    assert [(b["boundary"], b["kind"], b["rc"]) for b in result.boundaries] == [
        (0, "model", 0),
        (1, "tool", 0),
        (2, "model", 0),
        (3, "tool", 0),
        (4, "model", 2),
    ]
    assert seen == result.boundaries
    assert seen[0]["detail"] == 'calls list_pods({"namespace": "shop"})'
    assert seen[1]["detail"].startswith("list_pods -> ") and "items" in seen[1]["detail"]
    assert seen[4]["detail"] == "Report ready."
    assert result.report.read_text() == "Two pods, both running.\n"
    assert result.mcp == [
        {
            "backend": "kubernetes",
            "tool": "pods_list",
            "arguments": {"namespace": "shop"},
            "outcome": "ok",
        }
    ]
    transcript = json.loads(result.transcript.read_text())["messages"]
    assert transcript[0] == {"role": "system", "content": "Probe.\n"}
    assert json.loads(transcript[1]["content"]) == {"request": "How many pods are in shop?"}
    assert json.loads(transcript[3]["content"])["text"] == '{"items": ["a", "b"]}'
    document = json.loads((result.directory / "result.json").read_text())
    assert document == result.document()
    assert document["ok"] and document["outcome"] == "completed" and document["boundaries"] == 5
    assert document["tools"] == ["list_pods", "write_report"]
    assert document["model"] == "scripted" and document["mcp"]["ok"] == 1
    assert "exercised only on the platform" in document["note"]
    assert (result.directory / "app" / "probe_agent" / "agent.py").is_file()
    assert not list((result.directory / "app").rglob("__pycache__"))


def test_what_the_grants_refuse_and_what_the_scenario_omits_are_named(agent, tmp_path):
    turns = [
        {"call": "read_secret", "arguments": {"name": "db"}},
        {"call": "list_events", "arguments": {"namespace": "shop"}},
        REPORT,
        {"say": "Done."},
    ]
    result = local.run(agent, scenario(model=turns), out=tmp_path / "run")
    assert result.outcome == "completed" and not result.ok
    transcript = json.loads(result.transcript.read_text())["messages"]
    assert json.loads(transcript[3]["content"]) == {"error": "policy_denied"}
    assert json.loads(transcript[5]["content"]) == {"error": "UnscriptedCall"}
    assert [c["outcome"] for c in result.mcp] == ["denied", "unscripted"]
    denied, unscripted = result.problems
    assert "kubernetes/secrets_get, which deploy/agent.yaml does not grant" in denied
    assert "add `secrets_get` to the tools of the `kubernetes` entry" in denied
    assert "kubernetes/events_list and the scenario has no reply" in unscripted
    assert "add `- tool: events_list` with a `result:` under `mcp:`" in unscripted
    assert result.document()["mcp"] == {
        "ok": 0,
        "error": 0,
        "denied": 1,
        "unscripted": 1,
        "invalid": 0,
    }


def test_a_scripted_mcp_error_reaches_the_model_as_the_platform_reports_it(agent, tmp_path):
    failing = {"tool": "pods_list", "error": "upstream unavailable"}
    result = local.run(agent, scenario(mcp=[failing]), out=tmp_path / "run")
    transcript = json.loads(result.transcript.read_text())["messages"]
    assert json.loads(transcript[3]["content"]) == {"error": "RuntimeError"}
    assert result.ok and [c["outcome"] for c in result.mcp] == ["error"]


@pytest.mark.parametrize(
    ("changes", "limit", "outcome", "detail", "problem"),
    [
        ({"model": SCENARIO["model"][:1]}, 64, "script_ended", "asked for turn 2", None),
        ({}, 3, "boundary_limit", "not finished after 3 boundaries", None),
        ({"model": [{"say": "Nothing to do."}]}, 64, "completed", "", "without workspace/report"),
        (
            {"model": [{"call": "no_such_tool"}]},
            64,
            "agent_error",
            "no_such_tool",
            None,
        ),
        # Exit 2 with no record is not a completed session, whatever is in the workspace.
        ({"model": [{"call": "bail"}]}, 64, "unknown", "exited 2 without a result", None),
        # Nor is exit 2 with a result for another boundary, or one that is not the driver's.
        *(
            (
                {"model": [{"call": "forge", "arguments": {"record": json.dumps(record)}}]},
                64,
                "unknown",
                "exited 2 without a result",
                None,
            )
            for record in (
                {**RESULT, "boundary": 7},
                {**RESULT, "done": False},
                {"boundary": 1, "done": True},
            )
        ),
    ],
)
def test_sessions_that_do_not_finish_say_why(
    agent, tmp_path, changes, limit, outcome, detail, problem
):
    result = local.run(agent, scenario(**changes), out=tmp_path / "run", max_boundaries=limit)
    assert result.outcome == outcome and not result.ok
    assert detail in result.detail
    if problem:
        assert any(problem in p for p in result.problems)
    assert json.loads((result.directory / "result.json").read_text())["outcome"] == outcome


def test_a_manifest_that_disagrees_with_the_code_points_at_render(agent, tmp_path):
    document = agent.manifest()
    document["tools"] = TOOLS[:3]
    agent.manifest_path.write_text(yaml.safe_dump(document, sort_keys=False))
    result = local.run(agent, scenario(), out=tmp_path / "run")
    assert result.outcome == "agent_error" and result.boundaries[0]["rc"] == 3
    assert "do not match its manifest" in result.detail
    assert result.problems == ["agent.yaml `tools:` differs from the code; run `just render`"]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "must be a mapping"),
        ({"tasks": "x"}, "unknown ['tasks']"),
        ({"mcp": [{"result": "x"}]}, "mcp[0]: missing ['tool']"),
        ({"mcp": [{"tool": "t"}]}, "exactly one of result and error"),
        ({"mcp": [{"tool": "t", "result": 1, "error": "e"}]}, "exactly one of result and error"),
        ({"mcp": [{"tool": "t", "error": 5}]}, "tool, backend and error are text"),
        ({"model": []}, "model has no turns"),
        ({"model": [{"call": "t", "say": "x"}]}, "model[0]: unknown ['call']"),
        ({"model": [{"call": "t", "arguments": "x"}]}, "arguments a mapping"),
        ({"model": [{"speak": "x"}]}, "model[0]: unknown ['speak']"),
        ({"task": {"since": datetime.date(2026, 11, 10)}}, "every value must be JSON"),
        ({"mcp": 5}, "mcp is a list"),
        ({"model": {"call": "list_pods"}}, "model is a list"),
        ({"mcp": [{"tool": "t", "result": {"at": float("nan")}}]}, "every value must be JSON"),
    ],
)
def test_a_scenario_is_checked_before_anything_runs(document, message):
    with pytest.raises(local.ScenarioError) as raised:
        local.parse_scenario(document, "s.yaml")
    assert message in str(raised.value) and str(raised.value).startswith("s.yaml")


@pytest.mark.parametrize(
    ("change", "accepted"),
    [
        ({}, True),
        ({"usage": {"input_tokens": 3, "output_tokens": 0}}, True),
        ({"boundary": True}, False),  # True == 1, and the runner refuses it
        ({"boundary": 2}, False),
        ({"done": 1}, False),
        ({"kind": "other"}, False),
        ({"tool": ""}, False),
        ({"usage": {"input_tokens": -1, "output_tokens": 0}}, False),
        ({"usage": {"input_tokens": 1}}, False),
        ({"summary": "x" * 513}, False),
        ({"extra": 1}, False),
    ],
)
def test_a_step_result_is_read_as_the_session_runner_reads_it(change, accepted):
    printed = json.dumps({**RESULT, **change}).encode()
    assert (local.step_result(printed, 1, 2) is not None) is accepted
    assert local.step_result(printed, 1, 0) is None  # done disagrees with the exit code
    assert local.step_result(b"[]", 1, 2) is None and local.step_result(b"\xff", 1, 2) is None
    assert local.step_result(printed + b" " * local.MAX_OUTPUT, 1, 2) is None


def test_the_forged_result_the_platform_would_take_is_taken_here_too(agent, tmp_path):
    """A tool shares the step's stdout on the platform as it does here: one that prints
    the driver's own result for its boundary and exits 2 completes the session in both
    places. Fidelity, not a defence; the read-only layout is not one either."""
    forged = {"call": "forge", "arguments": {"record": json.dumps(RESULT)}}
    result = local.run(agent, scenario(model=[forged]), out=tmp_path / "run")
    assert result.outcome == "completed" and len(result.boundaries) == 2
    # After an exit code that is not a step's, the runner reads no result, and so not here.
    record = json.dumps({**RESULT, "done": False})
    forged = {"call": "forge", "arguments": {"record": record, "code": 5}}
    result = local.run(agent, scenario(model=[forged]), out=tmp_path / "other")
    assert result.outcome == "protocol_error" and len(result.boundaries) == 1


def test_runs_that_cannot_start_are_refused_without_a_run_directory(agent, tmp_path, monkeypatch):
    monkeypatch.delenv(local_step.MODEL_URL, raising=False)
    out = tmp_path / "run"

    def refused(scenario_, **kwargs):
        with pytest.raises(local.ScenarioError) as raised:
            local.run(agent, scenario_, out=out, **kwargs)
        assert not out.exists()
        return str(raised.value)

    assert "no scenario nightly" in refused("nightly")
    assert "give the scenario a `model:` script, or pass --live" in refused(scenario(model=None))
    assert f"--live needs {local_step.MODEL_URL}" in refused(scenario(), live=True)
    assert "no task" in refused(scenario(task=None))
    assert "larger than 16 KiB" in refused(scenario(task="x" * 17000))
    assert "max boundaries is 1 to 64" in refused(scenario(), max_boundaries=65)
    (tmp_path / "file").write_text("not a directory")
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    for taken in ("file", "dangling"):
        with pytest.raises(local.ScenarioError, match="is not a directory"):
            local.run(agent, scenario(), out=tmp_path / taken)
    (agent.root / "deploy" / "agent.yaml").rename(agent.root / "agent-cr.yaml")
    assert "probe: missing deploy/agent.yaml" in refused(scenario())
    (agent.root / "agent-cr.yaml").rename(agent.root / "deploy" / "agent.yaml")
    document = agent.manifest()
    agent.manifest_path.write_text(yaml.safe_dump({**document, "model": None}))
    # What `check` refuses is refused here too, in its words.
    assert "model: null requires an explicit empty model grant list" in refused(scenario())
    agent.manifest_path.write_text(yaml.safe_dump(document))
    (agent.root / "scenarios" / "binary.yaml").write_bytes(b"task: \xff\n")
    assert "not UTF-8" in refused("binary")

    cr_path = agent.root / "deploy" / "agent.yaml"
    written = cr_path.read_text()

    def with_capabilities(**changes):
        cr = yaml.safe_load(written)
        cr["spec"]["capabilities"].update(changes)
        cr_path.write_text(yaml.safe_dump(cr))
        return refused(scenario())

    # The grants a local session is held to are the ones a release compiles, so an Agent
    # the contracts refuse is not run on a more lenient reading of it.
    pods = {"backend": "kubernetes", "effect": "remote_read", "tools": ["pods_list"]}
    assert "MCP backend kubernetes declared twice" in with_capabilities(mcp=[pods, pods])
    # Nor is one the cluster would refuse to store: the CRD's schema is read first.
    crd = "deploy/agent.yaml is outside the Agent CRD: spec."
    assert f"{crd}capabilities.mcp[0].effect is required" in with_capabilities(
        mcp=[{"backend": "kubernetes", "tools": ["pods_list"]}]
    )
    assert f'{crd}capabilities.mcp[0].tools: "pods_list" is not an array' in with_capabilities(
        mcp=[{**pods, "tools": "pods_list"}]
    )
    assert f"{crd}capabilities.mcp[0].backend: null is not a string" in with_capabilities(
        mcp=[{**pods, "backend": None}]
    )
    assert f'{crd}capabilities.models: "abc" is not an array' in with_capabilities(models="abc")
    budget = {"backend": "llm-main", "models": ["platform-model"]}
    for value, why in ((0, "0 is less than 1"), ("100", '"100" is not an integer')):
        assert f"models[0].budget.tokensPerSession: {why}" in with_capabilities(
            models=[{**budget, "budget": {"tokensPerSession": value}}]
        )
    cr_path.write_text("spec: [unclosed\n")
    assert "malformed" in refused(scenario())

    def with_boundaries(value, **kwargs):
        cr = yaml.safe_load(written)
        cr["spec"]["runtime"] = {"maxBoundaries": value}
        cr_path.write_text(yaml.safe_dump(cr))
        return refused(scenario(), **kwargs)

    for value, why in (
        (65, "65 is more than 64"),
        (0, "0 is less than 1"),
        (True, "true is not an integer"),
        ("3", '"3" is not an integer'),
        (1.5, "1.5 is not an integer"),
        (float("inf"), "is not an integer"),
    ):
        assert f"{crd}runtime.maxBoundaries: " in (said := with_boundaries(value)) and why in said
    assert "max boundaries is 1 to 4, this Agent's spec" in with_boundaries(4, max_boundaries=5)
    cr_path.write_text(written)
    agent.manifest_path.write_text(yaml.safe_dump({**document, "model": None}))
    with_capabilities(models=[])
    assert "declares no model" in refused(scenario())
    cr_path.write_text(written)
    agent.manifest_path.write_text(yaml.safe_dump(document))
    out.mkdir()
    (out / "kept").write_text("earlier run")
    with pytest.raises(local.ScenarioError, match="never reused"):
        local.run(agent, scenario(), out=out)
    assert [p.name for p in out.iterdir()] == ["kept"]


def test_a_conformance_fixture_is_refused(agent, tmp_path):
    fixture = agent.root.with_name("conformance-checkpoint")
    agent.root.rename(fixture)
    with pytest.raises(local.ScenarioError, match="runs only in its own image"):
        local.run(layout.AgentDir(fixture), scenario(), out=tmp_path / "run")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only mode")
def test_agent_code_gets_a_read_only_image_and_not_the_callers_environment(
    agent, tmp_path, monkeypatch
):
    monkeypatch.setenv("REGISTRY_PASSWORD", "hunter2")  # pragma: allowlist secret
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "t")
    turns = [{"call": "poke"}, REPORT, {"say": "Done."}]
    result = local.run(agent, scenario(model=turns), out=tmp_path / "run")
    assert result.ok, (result.outcome, result.detail, result.problems)
    seen = json.loads(json.loads(result.transcript.read_text())["messages"][3]["content"])
    assert (seen["app"], seen["cwd"], seen["workspace"]) == (False, False, True)
    assert {"AGENT_APP_DIR", "AGENT_SANDBOX_DIR", "AGENT_TOKENS_PER_SESSION"} <= {
        *seen["environment"]
    }
    assert not {"REGISTRY_PASSWORD", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"} & {*seen["environment"]}
    shutil.rmtree(result.directory)  # writable again once the session is over
    assert not result.directory.exists()


def test_the_session_budget_is_the_smallest_grant_as_the_pod_template_sets_it(
    agent, tmp_path, monkeypatch
):
    def budget():
        return local.compiled(agent)["budgets"]["tokens_per_session"]

    assert budget() == 100
    cr = agent.root / "deploy" / "agent.yaml"
    document = yaml.safe_load(cr.read_text())
    models = document["spec"]["capabilities"]["models"]
    models.insert(0, {"backend": "llm-fast", "models": ["m"]})
    cr.write_text(yaml.safe_dump(document))
    assert budget() == 100  # an undeclared budget is the default, 200000
    models.append({"backend": "llm-small", "models": ["s"], "budget": {"tokensPerSession": 5}})
    cr.write_text(yaml.safe_dump(document))
    assert budget() == 5
    with chat_stub(monkeypatch):  # each reply reports 10 tokens used
        result = local.run(agent, scenario(), live=True, out=tmp_path / "run")
    assert result.outcome == "budget" and not result.ok
    assert len(result.boundaries) == 1
    # That was the SDK's advisory check. The gateway's reservation, which is what holds a
    # platform session, is not modelled: a scripted model reports no usage to count.
    assert local.run(agent, scenario(), out=tmp_path / "scripted").outcome == "completed"


def test_a_session_stops_at_the_agents_own_boundary_limit(agent, tmp_path):
    cr_path = agent.root / "deploy" / "agent.yaml"
    cr = yaml.safe_load(cr_path.read_text())
    cr["spec"]["runtime"] = {"maxBoundaries": 3}
    cr_path.write_text(yaml.safe_dump(cr))
    result = local.run(agent, "smoke", out=tmp_path / "run")  # five boundaries when allowed
    assert result.outcome == "boundary_limit" and len(result.boundaries) == 3


def test_the_session_is_held_to_the_compiled_bundle(agent, tmp_path, monkeypatch):
    """Not to a second reading of deploy/agent.yaml: whatever the contracts compile is
    what answers MCP calls and bounds the session."""
    bundle = local.compiled(agent)
    assert bundle["mcp"]["kubernetes"]["tools"] == ["events_list", "pods_list"]
    bundle["mcp"]["kubernetes"]["tools"] = ["events_list"]
    bundle["runtime"]["max_boundaries"] = 2
    monkeypatch.setattr(local, "compiled", lambda _agent: bundle)
    result = local.run(agent, "smoke", out=tmp_path / "run")
    assert result.outcome == "boundary_limit" and len(result.boundaries) == 2
    assert [(c["tool"], c["outcome"]) for c in result.mcp] == [("pods_list", "denied")]


def _tar_is_gnu() -> bool:
    try:
        version = subprocess.run(["tar", "--version"], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    return b"GNU tar" in version


def _fill(sandbox, case: str) -> None:
    """One awkward thing for tar to store, under an empty workspace/ and state/."""
    workspace, state = sandbox / "workspace", sandbox / "state"
    workspace.mkdir(parents=True)
    state.mkdir()
    under = workspace / ("d" * 50)  # with a file name below, a member name near 100 bytes
    kind, _, number = case.partition("-")
    if kind == "name":  # workspace/ddd…/xxx… is exactly `number` bytes
        under.mkdir()
        (under / ("x" * (int(number) - len(f"workspace/{under.name}/")))).write_bytes(b"x")
    elif kind == "directory":  # the trailing slash tar adds counts toward the 100
        (workspace / ("x" * (int(number) - len("workspace//")))).mkdir()
    elif kind == "target":
        (workspace / "link").symlink_to("t" * int(number))
    elif kind == "linked":  # a second name for a file of `number` bytes behind a long first name
        under.mkdir()
        (under / ("x" * 60)).write_bytes(b"x" * int(number))
        os.link(under / ("x" * 60), workspace / "again")
    elif kind == "relinked":  # tar links a second name of a symbolic link, not of a fifo
        under.mkdir()
        if number == "symlink":
            (under / ("x" * 60)).symlink_to("target")
        else:
            os.mkfifo(under / ("x" * 60))
        os.link(under / ("x" * 60), under / ("y" * 60), follow_symlinks=False)
    elif kind == "sizes":
        for size in (0, 1, 511, 512, 513, 20_000):
            (workspace / f"file-{size}").write_bytes(b"x" * size)
    elif kind == "token":
        (state / "token.json").write_text("not in the snapshot " * 600)
        (state / "state.json").write_text("{}" * 700)
        (workspace / "state").mkdir()
        (workspace / "state" / "token.json").mkdir()  # the exclude is not anchored
        (workspace / "state" / "token.json" / "under").write_bytes(b"x" * 5000)
        (workspace / "token.json").write_bytes(b"x" * 700)  # and this is not excluded
    elif kind == "other":
        os.mkfifo(workspace / "fifo")
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(workspace / "socket"))
        listener.close()
    else:
        assert kind == "empty", case


@pytest.mark.skipif(not _tar_is_gnu(), reason="the sandbox image's tar is GNU tar")
@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "sizes",
        *(f"name-{n}" for n in (99, 100, 101, 150)),
        *(f"directory-{n}" for n in (99, 100, 101)),
        *(f"target-{n}" for n in (99, 100, 101, 150)),
        *(f"linked-{n}" for n in (0, 1, 2000)),
        "relinked-symlink",
        "relinked-fifo",
        "token",
        "other",
    ],
)
def test_the_snapshot_is_measured_as_the_tar_the_platform_takes(tmp_path, case):
    """Entry by entry: tar's 10 KiB records would hide an entry counted wrong, so what is
    compared first is the archive without its padding."""
    _fill(tmp_path, case)
    command = ["tar", "-C", str(tmp_path), "--exclude=state/token.json", "-cf", "-"]
    archive = subprocess.run([*command, "workspace", "state"], capture_output=True, check=False)
    assert archive.returncode == 0 or b"socket ignored" in archive.stderr, archive.stderr
    entries = archive.stdout.rstrip(b"\0")  # no header and no file here ends in a zero byte
    assert local.tar_bytes(tmp_path) == -(-len(entries) // 512) * 512
    assert local.snapshot_size(tmp_path) == len(archive.stdout)


def test_a_tree_that_changes_while_it_is_measured_is_measured_without_what_went(
    tmp_path, monkeypatch
):
    _fill(tmp_path, "sizes")
    whole = local.tar_bytes(tmp_path)
    lstat = pathlib.Path.lstat

    def vanishing(path, *args, **kwargs):
        if path.name == "file-20000":
            raise FileNotFoundError(path)
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "lstat", vanishing)
    assert local.tar_bytes(tmp_path) == whole - 512 - 20_480


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes a read-only tree either way")
@pytest.mark.parametrize(
    ("sent", "code", "said"),
    [(signal.SIGTERM, 143, b""), (signal.SIGINT, 130, b"interrupted")],
)
def test_a_stopped_run_ends_its_step_and_leaves_a_removable_directory(
    agent, tmp_path, sent, code, said
):
    (agent.root / "scenarios" / "nap.yaml").write_text(
        yaml.safe_dump({"task": "Sleep.", "model": [{"call": "nap"}]})
    )
    out = tmp_path / "run"
    # A test runner started in the background hands down an ignored SIGINT, which Python
    # then leaves ignored; the run starts with the default, as it does from a terminal.
    started = (
        "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); "
        "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])"
    )
    command = [sys.executable, "-c", started, "-m", "zetra_agents.cli"]
    running = subprocess.Popen(
        [*command, "--repo", str(agent.root.parent.parent)]
        + ["run", "probe", "--scenario", "nap", "--out", str(out)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pid_file = out / "sandbox" / "workspace" / "pid"
    try:
        deadline = time.monotonic() + 60
        while not pid_file.is_file() or not pid_file.read_text():
            if running.poll() is not None or time.monotonic() > deadline:
                running.kill()  # first, or reading what it said waits for it to say it
                pytest.fail(running.communicate()[1].decode())
            time.sleep(0.05)
        step = int(pid_file.read_text())
        running.send_signal(sent)
        _, stderr = running.communicate(timeout=30)
    finally:
        running.kill()
    assert running.returncode == code and said in stderr and b"Traceback" not in stderr
    with pytest.raises(ProcessLookupError):  # ended and waited for before the run returned
        os.kill(step, 0)
    shutil.rmtree(out)
    assert not out.exists()


def test_a_workspace_past_the_snapshot_limit_is_a_problem(agent, tmp_path, monkeypatch):
    monkeypatch.setattr(local, "MAX_SNAPSHOT", 64)
    result = local.run(agent, "smoke", out=tmp_path / "run")
    assert result.outcome == "completed" and not result.ok
    assert any("the platform snapshots at most 64" in p for p in result.problems)


class ChatStub(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, self.headers.get("Authorization"), body))
        asked = sum(1 for m in body["messages"] if m["role"] == "assistant")
        if asked < 2:
            call = (
                {"name": "poke", "arguments": "{}"}
                if asked == 0
                else {"name": "write_report", "arguments": json.dumps({"content": "Live."})}
            )
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": call}],
            }
        else:
            message = {"role": "assistant", "content": "Report ready."}
        data = json.dumps(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def chat_stub(monkeypatch):
    ChatStub.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ChatStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(local_step.MODEL_URL, f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv(local_step.MODEL_NAME, "local-model")
    monkeypatch.setenv(local_step.MODEL_API_KEY, "local-key")  # pragma: allowlist secret
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    try:
        yield
    finally:
        server.shutdown()


def test_a_live_run_asks_the_endpoint_in_the_environment(agent, tmp_path, monkeypatch):
    with chat_stub(monkeypatch):
        result = local.run(agent, scenario(), live=True, out=tmp_path / "run")
    assert result.ok, (result.outcome, result.detail, result.problems)
    assert result.report.read_text() == "Live.\n"
    document = result.document()
    assert document["model"] == "live" and document["boundaries"] == 5
    assert document["usage"] == {"input_tokens": 21, "output_tokens": 9}
    # The endpoint's key is the shim's: the agent's code never sees it and no file keeps it.
    transcript = json.loads(result.transcript.read_text())["messages"]
    handed = json.loads(transcript[3]["content"])["environment"]
    assert "AGENT_APP_DIR" in handed and not {*handed} & {
        local_step.MODEL_URL,
        local_step.MODEL_NAME,
        local_step.MODEL_API_KEY,
    }
    for path in result.directory.rglob("*"):
        assert path.is_dir() or b"local-key" not in path.read_bytes(), path
    paths, keys, bodies = zip(*ChatStub.requests, strict=True)
    assert set(paths) == {"/v1/chat/completions"} and set(keys) == {"Bearer local-key"}
    assert {b["model"] for b in bodies} == {"local-model"}
    assert [t["function"]["name"] for t in bodies[0]["tools"]] == [t["name"] for t in TOOLS]
    saved = (result.directory / "run.json").read_text()
    assert "local-key" not in saved and json.loads(saved)["model"] is None


def test_the_command_runs_the_smoke_scenario_by_default(agent, tmp_path, capsys, monkeypatch):
    repo = agent.root.parent.parent
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert cli.main(["--repo", str(repo), "run", "probe"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["ok"] and document["tools"] == ["list_pods", "write_report"]
    runs = tmp_path / "state" / "zetra" / "agent-runs" / "probe"
    assert [str(p) for p in runs.iterdir()] == [document["runDirectory"]]
    assert next(runs.iterdir()).stat().st_mode & 0o777 == 0o700
    assert "probe: scripted model," in captured.err and "smoke.yaml" in captured.err
    assert '  0  model  calls list_pods({"namespace": "shop"})' in captured.err
    assert "  4  model  Report ready." in captured.err


def test_the_command_takes_a_task_and_reports_refusals_and_failures(agent, tmp_path, capsys):
    repo = agent.root.parent.parent
    base = ["--repo", str(repo), "run", "probe"]
    out = tmp_path / "run"
    assert cli.main([*base, "--task", "Count again.", "--out", str(out)]) == 0
    capsys.readouterr()
    state = json.loads((out / "sandbox/state/state.json").read_text())
    assert json.loads(state["messages"][1]["content"]) == {"request": "Count again."}
    assert cli.main([*base, "--task-json", '"as given"', "--out", str(tmp_path / "b")]) == 0
    capsys.readouterr()
    state = json.loads((tmp_path / "b/sandbox/state/state.json").read_text())
    assert json.loads(state["messages"][1]["content"]) == "as given"
    assert cli.main([*base, "--task-json", "{", "--out", str(tmp_path / "c")]) == 2
    assert "--task-json is not JSON" in capsys.readouterr().err
    assert cli.main([*base, "--scenario", "missing", "--out", str(tmp_path / "d")]) == 2
    assert "no scenario missing" in capsys.readouterr().err
    (agent.root / "scenarios" / "denied.yaml").write_text(
        yaml.safe_dump(
            {**SCENARIO, "model": [{"call": "read_secret", "arguments": {"name": "x"}}, REPORT]}
        )
    )
    assert cli.main([*base, "--scenario", "denied", "--out", str(tmp_path / "e")]) == 1
    captured = capsys.readouterr()
    assert "problem: probe called MCP kubernetes/secrets_get" in captured.err
    assert json.loads(captured.out)["outcome"] == "script_ended"
    (agent.root / "scenarios" / "smoke.yaml").unlink()
    assert cli.main([*base, "--out", str(tmp_path / "f")]) == 2
    assert "no model" in capsys.readouterr().err
