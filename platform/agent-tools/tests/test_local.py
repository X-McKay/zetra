"""`zetra-agents run` drives the SDK's own boundary driver through a whole session, with
the scenario standing in for the model and for MCP behind the Agent's grants."""

import http.server
import json
import textwrap
import threading
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

    return {
        "tools": [
            Tool(list_pods, "remote_read"),
            Tool(list_events, "remote_read"),
            Tool(read_secret, "remote_read"),
            report_tool(ctx.workspace),
        ]
    }
'''
TOOLS = [
    {"name": "list_pods", "effect": "remote_read"},
    {"name": "list_events", "effect": "remote_read"},
    {"name": "read_secret", "effect": "remote_read"},
    {"name": "write_report", "effect": "workspace_write"},
]
REPORT = {"call": "write_report", "arguments": {"content": "Two pods, both running."}}
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
    ],
)
def test_a_scenario_is_checked_before_anything_runs(document, message):
    with pytest.raises(local.ScenarioError) as raised:
        local.parse_scenario(document, "s.yaml")
    assert message in str(raised.value) and str(raised.value).startswith("s.yaml")


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
    (agent.root / "deploy" / "agent.yaml").rename(agent.root / "agent-cr.yaml")
    assert "probe: missing deploy/agent.yaml" in refused(scenario())
    (agent.root / "agent-cr.yaml").rename(agent.root / "deploy" / "agent.yaml")
    document = agent.manifest()
    agent.manifest_path.write_text(yaml.safe_dump({**document, "model": None}))
    assert "declares no model" in refused(scenario())
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


class ChatStub(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, self.headers.get("Authorization"), body))
        asked = sum(1 for m in body["messages"] if m["role"] == "assistant")
        if asked == 0:
            call = {"name": "write_report", "arguments": json.dumps({"content": "Live."})}
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


def test_a_live_run_asks_the_endpoint_in_the_environment(agent, tmp_path, monkeypatch):
    ChatStub.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ChatStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(local_step.MODEL_URL, f"http://127.0.0.1:{server.server_port}/v1")
        monkeypatch.setenv(local_step.MODEL_NAME, "local-model")
        monkeypatch.setenv(local_step.MODEL_API_KEY, "local-key")  # pragma: allowlist secret
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        result = local.run(agent, scenario(), live=True, out=tmp_path / "run")
    finally:
        server.shutdown()
    assert result.ok, (result.outcome, result.detail, result.problems)
    assert result.report.read_text() == "Live.\n"
    document = result.document()
    assert document["model"] == "live" and document["boundaries"] == 3
    assert document["usage"] == {"input_tokens": 14, "output_tokens": 6}
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
    assert cli.main([*base, "--task-json", '{"pods": 2}', "--out", str(tmp_path / "b")]) == 0
    capsys.readouterr()
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
