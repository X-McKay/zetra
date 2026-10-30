"""The one-boundary protocol (10.4, 11.8), driven as the runner drives it: a subprocess."""

import json
import os
import stat
import subprocess
import sys

import pytest

from agent_runtime import step
from agent_runtime.tools import Tool
from tests.fakes import (
    MANIFEST,
    FakeGateway,
    assert_redacted,
    chat_response,
    make_app,
    make_ca,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def sandbox(agent_env, tmp_path):
    ca, cert, key = make_ca(tmp_path)
    gw = FakeGateway(cert, key)
    make_app(tmp_path / "app")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
    env.update(agent_env, AGENT_GATEWAY_URL=gw.url, AGENT_GATEWAY_CA=str(ca))
    env.pop("PYTHONPATH", None)
    yield Sandbox(env, gw, tmp_path)
    gw.close()


class Sandbox:
    def __init__(self, env, gw, root) -> None:
        self.env, self.gw, self.root = env, gw, root
        self.state = root / "sandbox" / "state"
        self.workspace = root / "sandbox" / "workspace"
        self.outputs: list[str] = []

    def raw(self, data: bytes, close: bool = True, timeout: float = 30):
        p = subprocess.Popen(
            [sys.executable, "-m", "agent_runtime.step"],
            cwd=ROOT,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        p.stdin.write(data)
        p.stdin.flush()
        if close:
            p.stdin.close()
        out, err = p.stdout.read(), p.stderr.read()  # stdin stays open when close is False
        rc = p.wait(timeout=timeout)
        if not close:
            p.stdin.close()
        text = out.decode()
        self.outputs += [text, err.decode()]
        assert_redacted(text, err.decode())
        assert len(out) <= 65536
        return rc, text, err.decode()

    def run(self, boundary, task=None, **kw):
        body = json.dumps({"boundary": boundary, "task": task or {"goal": "test"}}).encode()
        rc, text, err = self.raw(b"%d\n" % len(body) + body, **kw)
        out = json.loads(text) if text else None
        if out is not None:
            assert out["boundary"] == boundary
            assert out["done"] == (rc == 2)  # done exactly when the exit code is 2
            assert len(out["summary"]) <= 512
        return rc, out, err

    def seed(self, messages, usage=0, next_=0):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "state.json").write_text(
            json.dumps({"messages": messages, "usage": usage, "next": next_})
        )

    def saved(self) -> dict:
        return json.loads((self.state / "state.json").read_text())


def tool_call_state(name, args):
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        },
    ]


def test_t18_session_model_tool_model_done(sandbox):
    gw = sandbox.gw
    gw.chat_replies = [
        (
            200,
            chat_response(
                tool_calls=[("write_report", {"text": "all good"}), ("get_pod", {"name": "p"})]
            ),
        ),
        (200, chat_response("finished")),
    ]
    rc, out, _ = sandbox.run(0)
    assert (rc, out["kind"], out["tool"], out["usage"]) == (
        0,
        "model",
        None,
        {"input_tokens": 10, "output_tokens": 5},
    )
    req = gw.chat_requests[0]
    assert req["auth"].startswith("Bearer eyJ")
    body = req["body"]
    assert body["model"] == "test-model" and body["parallel_tool_calls"] is False
    assert [t["function"]["name"] for t in body["tools"]] == [t["name"] for t in MANIFEST["tools"]]
    assert sandbox.saved()["messages"][-1]["tool_calls"][0]["function"]["name"] == "write_report"
    assert len(sandbox.saved()["messages"][-1]["tool_calls"]) == 1  # first tool call only

    rc, out, _ = sandbox.run(1)
    assert (rc, out["kind"], out["tool"]) == (0, "tool", "write_report")
    assert (sandbox.workspace / "report.md").read_text() == "all good"
    assert not (sandbox.workspace / "pod-p").exists()

    rc, out, _ = sandbox.run(2)
    assert (rc, out["done"], out["summary"]) == (2, True, "finished")
    assert gw.chat_requests[1]["body"]["messages"][-1]["role"] == "tool"

    # One token request across three boundary processes: the persisted token is reused.
    assert len(gw.token_requests) == 1
    assert gw.token_requests[0]["client_id"] == "agent-tenant-test-conformance"
    for n in range(3):
        assert (sandbox.state / f"{n}.started").exists() and (sandbox.state / f"{n}.done").exists()
    for f in ("state.json", "token.json"):
        assert stat.S_IMODE((sandbox.state / f).stat().st_mode) == 0o600
    assert sandbox.saved()["next"] == 3
    assert_redacted(*sandbox.outputs)


def test_stdin_is_never_read_to_eof(sandbox):
    sandbox.gw.chat_replies = [(200, chat_response("done"))]
    rc, out, _ = sandbox.run(0, close=False)
    assert rc == 2 and out["done"] is True


@pytest.mark.parametrize(
    "data",
    [
        b"",  # no header at all
        b"0\n",  # zero length
        b"abc\n{}",  # not a number
        b"+12\n" + b"x" * 12,  # int() would accept this; the protocol does not
        b"65537\n" + b"x" * 65537,  # over the 64 KiB input bound
        b"99999999999999999999\n",  # oversized header line
        b'20\n{"boundary": 0}',  # fewer bytes than announced, then EOF
        b"9\nnot json!",  # invalid JSON
        b"2\n[]",  # not an object
        b'13\n{"task": {}}',  # no boundary
        b'31\n{"boundary": true, "task": {}}',  # boolean is not a boundary number
    ],
)
def test_t25_malformed_input_exits_5_without_output(sandbox, data):
    rc, text, _ = sandbox.raw(data)
    assert (rc, text) == (5, "")
    assert not sandbox.state.exists() or not any(sandbox.state.glob("*.started"))


def test_t25_boundary_mismatch_exits_5_and_changes_nothing(sandbox):
    rc, out, _ = sandbox.run(3)
    assert rc == 5 and out["summary"] == "boundary mismatch" and out["done"] is False
    assert (
        not (sandbox.state / "3.started").exists() and not (sandbox.state / "state.json").exists()
    )


def test_t25_replayed_boundary_is_a_mismatch(sandbox):
    sandbox.gw.chat_replies = [(200, chat_response(tool_calls=[("boom", {})]))]
    assert sandbox.run(0)[0] == 0
    rc, _, _ = sandbox.run(0)
    assert rc == 5 and len(sandbox.gw.chat_requests) == 1


@pytest.mark.parametrize("kind", ["string", "list", "nested", "wide"])
def test_huge_tool_results_are_truncated_structurally(sandbox, kind):
    sandbox.seed(tool_call_state("huge", {"kind": kind}))
    rc, out, _ = sandbox.run(0)
    assert rc == 0 and out["kind"] == "tool"
    content = sandbox.saved()["messages"][-1]["content"]
    assert len(content.encode()) <= step.MAX_TOOL_RESULT
    json.loads(content)  # still valid JSON


def test_tool_exception_is_data_for_the_model(sandbox):
    sandbox.seed(tool_call_state("boom", {}))
    rc, _, _ = sandbox.run(0)
    assert rc == 0
    assert json.loads(sandbox.saved()["messages"][-1]["content"]) == {"error": "KeyError"}


@pytest.mark.parametrize("name,args", [("nope", {}), ("write_report", "not-a-dict")])
def test_unknown_tool_or_bad_arguments_exit_3(sandbox, name, args):
    messages = tool_call_state(name, {})
    messages[-1]["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
    sandbox.seed(messages)
    rc, out, _ = sandbox.run(0)
    assert rc == 3 and out["kind"] == "tool" and out["done"] is False


def test_tools_not_matching_the_manifest_exit_3(sandbox):
    (sandbox.root / "sandbox").mkdir(exist_ok=True)
    (sandbox.root / "sandbox" / "extra_tool").write_text("1")
    rc, out, _ = sandbox.run(0)
    assert rc == 3 and "do not match" in out["summary"]
    assert sandbox.gw.chat_requests == [] and not (sandbox.state / "0.started").exists()


def test_manifest_declaring_remote_write_exits_3(sandbox):
    manifest = {
        **MANIFEST,
        "tools": [*MANIFEST["tools"], {"name": "delete_pod", "effect": "remote_write"}],
    }
    make_app(sandbox.root / "app", manifest)
    rc, out, _ = sandbox.run(0)
    assert rc == 3 and "not admitted" in out["summary"]


@pytest.mark.parametrize("status,rc", [(403, 3), (429, 4)])
def test_model_403_is_agent_error_and_429_is_budget(sandbox, status, rc):
    sandbox.gw.chat_replies = [(status, {"error": {"message": "denied"}})]
    got, out, _ = sandbox.run(0)
    assert got == rc and out["done"] is False and len(sandbox.gw.chat_requests) == 1


def test_advisory_budget_exits_4_after_the_call(sandbox):
    sandbox.env["AGENT_TOKENS_PER_SESSION"] = "10"
    sandbox.gw.chat_replies = [(200, chat_response(tool_calls=[("boom", {})], usage=(8, 4)))]
    rc, out, _ = sandbox.run(0)
    assert rc == 4 and out["done"] is False and sandbox.saved()["usage"] == 12


def test_gateway_with_another_ca_is_refused(sandbox, tmp_path):
    (tmp_path / "other").mkdir()
    other, _, _ = make_ca(tmp_path / "other")
    sandbox.env["AGENT_GATEWAY_CA"] = str(other)
    rc, out, err = sandbox.run(0)
    assert rc == 1 and out is None and "unexpected APIConnectionError" in err  # unknown outcome
    assert sandbox.gw.token_requests == []


# In-process units


def test_clip_keeps_valid_json_and_bounds():
    v = step.bounded({"a": "x" * 100_000, "b": list(range(100_000))}, 65536)
    assert len(json.dumps(v)) <= 65536
    assert step.bounded(float("nan"), 100) == "nan"


def test_trim_context_terminates_when_tool_results_are_already_dropped():
    big = "x" * (step.MAX_CONTEXT_BYTES + 1)
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": big},
        {"role": "tool", "content": "r"},
    ]
    assert step.trim_context(msgs)[2]["content"] == step.DROPPED


def test_emit_bounds_and_redacts(capsys):
    from tests.fakes import agent_token

    with pytest.raises(SystemExit) as e:
        step.emit(
            {
                "boundary": 1,
                "kind": "tool",
                "tool": "t" * 10_000,
                "usage": None,
                "summary": f"Bearer {agent_token()} " + "é" * 10_000,
            },
            0,
        )
    out = json.loads(capsys.readouterr().out)
    assert e.value.code == 0 and out["done"] is False
    assert len(out["tool"]) == 128 and len(out["summary"]) == 512
    assert_redacted(out["summary"])


def test_remote_write_tools_are_not_admitted():
    async def delete_pod(name):
        """Delete."""

    with pytest.raises(ValueError, match="not admitted"):
        Tool(delete_pod, "remote_write")


def test_sync_tools_are_rejected():
    def sync_tool():
        """Sync."""

    with pytest.raises(ValueError, match="async"):
        Tool(sync_tool, "workspace_read")
