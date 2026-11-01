"""Cold subprocess conformance for the versioned shared-core protocol."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.fakes import FakeGateway, chat_response, make_ca

TASK = "Write a report."
MODULE = '''
from google.adk.agents import LlmAgent
from zetra import Tool

def create_agent(ctx):
    async def write_report(text: str, count: int, approved: bool):
        """Write an approved report the requested number of times."""
        assert isinstance(count, int) and isinstance(approved, bool)
        (ctx.workspace / "report.txt").write_text(text * count)
        if text == "spawn":
            import subprocess, sys
            script = "from pathlib import Path; Path(" + repr(str(ctx.workspace / "spawned")) + ").write_text('bad')"
            subprocess.run([sys.executable, "-c", script], check=True)
        if text == "fail-after-write":
            raise RuntimeError("private-agent-error")
        return {"written": count, "approved": approved}
    return LlmAgent(name="probe", model=ctx.model, instruction=ctx.instruction,
        tools=ctx.tools(Tool(write_report, "workspace_write")))
'''


def envelope():
    return {
        "schema_version": 2,
        "name": "probe",
        "runtime_profile": "zetra-core-v1",
        "definition": "coreprobe:agent.yaml",
        "model": {"backend": "llm-main", "name": "test-model", "max_tokens": 64, "temperature": 0},
        "tools": [{"name": "write_report", "effect": "workspace_write"}],
    }


@pytest.fixture
def core_env(agent_env, monkeypatch, tmp_path):
    app = Path(agent_env["AGENT_APP_DIR"])
    package = app / "coreprobe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(MODULE)
    (package / "instructions.md").write_text("Use the approved report tool.")
    (package / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "probe",
                "owner": "tests",
                "entrypoint": "coreprobe:create_agent",
                "model": "fixture",
                "policy": "workspace",
                "instructions": "coreprobe:instructions.md",
            }
        )
    )
    raw = yaml.safe_dump(envelope()).encode()
    (app / "agent.yaml").write_bytes(raw)
    binding = {
        "profile": "zetra-core-v1",
        "tenant": "tenant-test",
        "agent": "probe",
        "release": "probe-v1",
        "image": "example/probe@sha256:" + "a" * 64,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "policy_sha256": "b" * 64,
        "namespace": "default",
        "workflow_id": "session:tenant-test:probe:12345678-1234-1234-1234-123456789abc",
        "run_id": "23456789-1234-1234-1234-123456789abc",
        "session_id": "12345678-1234-1234-1234-123456789abc",
        "generation": 1,
        "task_sha256": hashlib.sha256(TASK.encode()).hexdigest(),
    }
    monkeypatch.setenv("AGENT_CORE_BINDING", json.dumps(binding))
    monkeypatch.setenv("PYTHONPATH", str(app))
    ca, cert, key = make_ca(tmp_path)
    gateway = FakeGateway(cert, key)
    monkeypatch.setenv("AGENT_GATEWAY_URL", gateway.url)
    monkeypatch.setenv("AGENT_GATEWAY_CA", str(ca))
    try:
        yield binding, gateway, app, Path(agent_env["AGENT_SANDBOX_DIR"])
    finally:
        gateway.close()


def command(binding, action="prepare", position=None, operation=None):
    return {
        "schema_version": 1,
        "binding": binding,
        "task": TASK,
        "action": action,
        "position": position,
        "operation": operation,
    }


def child(value):
    from agent_runtime.core_protocol import decode_frame, encode_frame

    completed = subprocess.run(
        [sys.executable, "-m", "agent_runtime.core_step"],
        input=encode_frame(value),
        capture_output=True,
        timeout=35,
        check=False,
        env=os.environ.copy(),
    )
    assert b"private-agent-error" not in completed.stderr + completed.stdout
    return completed.returncode, decode_frame(completed.stdout)


def tool_response(text="ok"):
    return chat_response(
        tool_calls=[("write_report", {"text": text, "count": 2, "approved": True})]
    )


def advance_execute(binding, position=None):
    code, prepared = child(command(binding, position=position))
    assert code == 0 and prepared["status"] == "operation"
    code, executed = child(command(binding, "execute", prepared["position"], prepared["operation"]))
    return prepared, code, executed


def test_cold_model_tool_model_and_effect_free_completion(core_env):
    binding, gateway, _, root = core_env
    gateway.chat_replies = [(200, tool_response()), (200, chat_response("Finished."))]
    p0, code, first = advance_execute(binding)
    assert code == 0 and first["status"] == "candidate"
    assert len(gateway.chat_requests) == 1
    assert first["receipt"]["position"]["cursor"] == 1
    p1, code, second = advance_execute(binding, first["receipt"]["position"])
    assert code == 0 and p1["operation"]["kind"] == "tool"
    assert (root / "workspace/report.txt").read_text() == "okok"
    assert len(gateway.chat_requests) == 1
    _, code, third = advance_execute(binding, second["receipt"]["position"])
    assert code == 0 and third["receipt"]["position"]["cursor"] == 3
    code, done = child(command(binding, position=third["receipt"]["position"]))
    assert code == 2 and done["status"] == "done" and done["text"] == "Finished."
    assert len(gateway.chat_requests) == 2
    assert (root / "state/core/objects").is_dir()
    params = gateway.chat_requests[0]["body"]["tools"][0]["function"]["parameters"]
    assert params["properties"]["count"]["type"] == "integer"
    code, duplicate = child(command(binding, "execute", p0["position"], p0["operation"]))
    assert code == 1 and duplicate["status"] == "unknown"
    assert len(gateway.chat_requests) == 2


@pytest.mark.parametrize("field", ["run_id", "task_sha256", "policy_sha256", "generation", "image"])
def test_wrong_identity_rejected_before_import_or_effect(core_env, field):
    binding, gateway, app, _ = core_env
    (app / "coreprobe/__init__.py").write_text('raise RuntimeError("agent imported")')
    changed = copy.deepcopy(binding)
    changed[field] = 2 if field == "generation" else "wrong"
    code, reply = child(command(changed))
    assert code == 5 and reply["status"] == "invalid"
    assert not gateway.chat_requests and not gateway.token_requests


def test_wrong_operation_does_not_consume_admission(core_env):
    binding, gateway, _, _ = core_env
    gateway.chat_replies = [(200, chat_response("Finished."))]
    _, prepared = child(command(binding))
    altered = copy.deepcopy(prepared["operation"])
    altered["executor"] = "unapproved"
    code, _ = child(command(binding, "execute", prepared["position"], altered))
    assert code == 5 and not gateway.chat_requests
    code, reply = child(command(binding, "execute", prepared["position"], prepared["operation"]))
    assert code == 0 and reply["status"] == "candidate"


def test_tool_exception_after_work_is_unknown_without_redispatch(core_env):
    binding, gateway, _, root = core_env
    gateway.chat_replies = [(200, tool_response("fail-after-write"))]
    _, _, first = advance_execute(binding)
    prepared, code, failed = advance_execute(binding, first["receipt"]["position"])
    assert code == 1 and failed["status"] == "unknown" and "receipt" not in failed
    assert (root / "workspace/report.txt").read_text() == "fail-after-write" * 2
    code, _ = child(command(binding, "execute", prepared["position"], prepared["operation"]))
    assert code == 1 and len(gateway.chat_requests) == 1


@pytest.mark.parametrize("status,code,reason", [(403, 3, "policy"), (429, 4, "budget")])
def test_model_refusal_has_no_candidate_or_retry(core_env, status, code, reason):
    binding, gateway, _, _ = core_env
    gateway.chat_replies = [(status, {"error": {"message": "private-agent-error"}})]
    prepared, actual, reply = advance_execute(binding)
    assert actual == code and reply == {"status": "refused", "reason": reason}
    assert len(gateway.chat_requests) == 1
    duplicate, _ = child(command(binding, "execute", prepared["position"], prepared["operation"]))
    assert duplicate == 1 and len(gateway.chat_requests) == 1


def test_protocol_rejects_duplicate_keys_nonfinite_and_oversize():
    from agent_runtime.core_protocol import MAX_CONTROL, decode_frame

    for body in [b'{"x":1,"x":2}', b'{"x":NaN}', b"[]", b"{}\n{}"]:
        with pytest.raises(ValueError):
            decode_frame(str(len(body)).encode() + b"\n" + body)
    with pytest.raises(ValueError):
        decode_frame(str(MAX_CONTROL + 1).encode() + b"\n")


@pytest.mark.parametrize("violation", ["parallel", "unknown_tool"])
def test_provider_contract_violation_is_unknown_without_fallback(core_env, violation):
    binding, gateway, _, _ = core_env
    response = tool_response()
    calls = response["choices"][0]["message"]["tool_calls"]
    if violation == "parallel":
        calls.append(copy.deepcopy(calls[0]))
    else:
        calls[0]["function"]["name"] = "unregistered"
    gateway.chat_replies = [(200, response)]
    _, code, reply = advance_execute(binding)
    assert code == 1 and reply == {"status": "unknown"}
    assert len(gateway.chat_requests) == 1


def test_factory_output_and_finalizers_cannot_pollute_control_reply(core_env):
    binding, _, app, _ = core_env
    source = app / "coreprobe/__init__.py"
    source.write_text(
        MODULE
        + '\nimport atexit\nprint("private-agent-error")\natexit.register(print, "private-agent-error")\n'
    )
    code, reply = child(command(binding))
    assert code == 0 and reply["status"] == "operation"


def test_changed_envelope_and_factory_inventory_fail_before_model(core_env, monkeypatch):
    binding, gateway, app, _ = core_env
    changed = envelope()
    changed["tools"][0]["effect"] = "remote_read"
    raw = yaml.safe_dump(changed).encode()
    (app / "agent.yaml").write_bytes(raw)
    # First reject a changed envelope even before loading its factory.
    code, reply = child(command(binding))
    assert code == 5 and reply["status"] == "invalid"
    # A correctly hashed envelope still must match the actual registered factory.
    binding["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setenv("AGENT_CORE_BINDING", json.dumps(binding))
    monkeypatch.setenv("AGENT_SANDBOX_DIR", str(app.parent / "another-sandbox"))
    code, reply = child(command(binding))
    assert code == 5 and reply["status"] == "invalid"
    assert not gateway.chat_requests and not gateway.token_requests


def test_fresh_generation_reconstructs_only_supplied_accepted_checkpoint(core_env, monkeypatch):
    import shutil

    binding, gateway, _, root = core_env
    gateway.chat_replies = [(200, tool_response()), (200, chat_response("Finished."))]
    _, _, first = advance_execute(binding)
    restored = root.parent / "restored"
    shutil.copytree(root, restored)
    # A local newer candidate in the original sandbox must not influence replacement.
    _, code, _ = advance_execute(binding, first["receipt"]["position"])
    assert code == 0
    binding = {**binding, "generation": 2}
    monkeypatch.setenv("AGENT_CORE_BINDING", json.dumps(binding))
    monkeypatch.setenv("AGENT_SANDBOX_DIR", str(restored))
    code, prepared = child(command(binding, position=first["receipt"]["position"]))
    assert code == 0 and prepared["operation"]["kind"] == "tool"
    assert not (restored / "workspace/report.txt").exists()
    assert len(gateway.chat_requests) == 1


@pytest.mark.parametrize("field,value", [("schema_version", True), ("position", {"cursor": "0"})])
def test_command_fields_cannot_be_silently_coerced(core_env, field, value):
    from agent_runtime.core_protocol import CoreCommand

    binding, _, _, _ = core_env
    request = command(binding)
    request[field] = value
    with pytest.raises(ValueError):
        CoreCommand.model_validate(request)


def test_unmanaged_child_process_is_outside_core_tool_profile(core_env):
    binding, gateway, _, root = core_env
    gateway.chat_replies = [(200, tool_response("spawn"))]
    _, _, first = advance_execute(binding)
    _, code, reply = advance_execute(binding, first["receipt"]["position"])
    assert code == 1 and reply == {"status": "unknown"}
    assert not (root / "workspace/spawned").exists()
