"""One cold, versioned shared-core command. Only the caller selects accepted progress."""

import asyncio
import fcntl
import hashlib
import os
import sys
from importlib.resources import files
from pathlib import Path

from zetra._runtime.dispatch import ExecutionRefused, OutcomeUnknown
from zetra._runtime.records import canonical_bytes, decode_model
from zetra.manifest import AgentSpec, resource_parts

from agent_runtime import confine
from agent_runtime.core_protocol import (
    CoreBinding,
    CoreCommand,
    CoreManifest,
    encode_frame,
    read_frame,
    strict_json,
    yaml_mapping,
)
from agent_runtime.gateway import BudgetExhausted, PolicyDenied
from agent_runtime.identity import env, sandbox_dir

# Keep the lease until interpreter exit, including Python finalizers and threads.
_LEASE_FD = None


def process_guard(event, args):
    """Cooperative core tools cannot create writers outside the boundary lease."""
    if event in {
        "subprocess.Popen",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.exec",
        "os.system",
    }:
        raise RuntimeError("process creation is outside the core tool profile")


def durable_new(path, data):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def real_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("unsafe core directory")
    return path


def lease(root, binding):
    global _LEASE_FD
    state = real_directory(real_directory(root / "state") / "core")
    _LEASE_FD = os.open(state / "lease", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(_LEASE_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise OutcomeUnknown("core execution lease is occupied") from None
    identity = state / "binding.json"
    expected = canonical_bytes(binding.model_dump(exclude={"generation"}))
    try:
        durable_new(identity, expected)
    except FileExistsError:
        fd = os.open(identity, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if stream.read(len(expected) + 1) != expected:
                raise ValueError("retained execution identity differs")
    real_directory(state / "attempts")
    return state


def load_definition(binding, workspace):
    from zetra._runtime.definition import Definition

    from agent_runtime.core_model import GatewayModel

    def bounded_manifest(resource):
        with resource.open("rb") as stream:
            return stream.read(128001)

    raw = bounded_manifest(Path(os.environ.get("AGENT_APP_DIR", "/app")) / "agent.yaml")
    if hashlib.sha256(raw).hexdigest() != binding.manifest_sha256:
        raise ValueError("platform envelope digest differs")
    envelope = CoreManifest.model_validate(yaml_mapping(raw))
    if envelope.name != binding.agent:
        raise ValueError("platform envelope agent differs")
    package, resource = resource_parts(envelope.definition)
    spec = AgentSpec.model_validate(
        yaml_mapping(bounded_manifest(files(package).joinpath(resource.as_posix())))
    ).validate_policy()
    if spec.name != envelope.name:
        raise ValueError("packaged definition agent differs")
    model = GatewayModel(envelope.model)

    async def mcp(backend, tool, arguments):
        from agent_runtime.mcp import call_tool

        try:
            return await call_tool(backend, tool, arguments)
        except PolicyDenied:
            raise ExecutionRefused("policy") from None
        except BudgetExhausted:
            raise ExecutionRefused("budget") from None

    return Definition(
        spec.model_dump(),
        workspace,
        model_binding=(model.name, model),
        mcp_binding=mcp,
        required_tools=envelope.inventory,
    )


def final_text(tape, position):
    from google.adk.models.llm_response import LlmResponse

    for record in reversed(tape.records(position)):
        if record["operation"]["kind"] == "model":
            response = decode_model(LlmResponse, record["outcome"]["value"])
            text = (
                "".join(
                    part.text or "" for part in (response.content.parts or []) if not part.thought
                )
                if response.content
                else ""
            )
            if len(text.encode()) > 65536:
                raise ValueError("final response exceeds control profile")
            return text
    return ""


async def run(command):
    from zetra._runtime.runtime import Runtime

    root = real_directory(sandbox_dir())
    state = lease(root, command.binding)
    cursor = command.position.cursor if command.position is not None else 0
    marker = state / "attempts" / str(cursor)
    if marker.exists():
        # No local candidate or completion marker can authorize another effect.
        raise OutcomeUnknown("operation already attempted")
    real_directory(root / "workspace")
    definition = load_definition(command.binding, root / "workspace")
    runtime = Runtime(
        root,
        session=command.binding.invocation,
        manifest=command.binding.manifest,
        key=os.urandom(32),
        message=command.task,
        definition=definition,
        generation=command.binding.generation,
        objects_root=state / "objects",
    )
    admitted = False
    try:
        position = command.position
        if position is None:
            position = runtime.tape.empty(runtime.workspace.capture())
        if position.workspace is None:
            raise ValueError("accepted position has no workspace")
        internal_binding = {"session": command.binding.invocation}
        prepared = await runtime.handle(
            runtime.authority.sign(
                "advance",
                {
                    "binding": internal_binding,
                    "position": position.model_dump(),
                    "pending": command.operation if command.action == "execute" else None,
                },
            )
        )
        if command.action == "prepare":
            if prepared["status"] == "done":
                return 2, {
                    "status": "done",
                    "position": position.model_dump(),
                    "text": final_text(runtime.tape, position),
                }
            return 0, {**prepared, "position": position.model_dump()}
        if prepared != {"status": "operation", "operation": command.operation}:
            raise ValueError("pinned operation differs from reconstruction")
        durable_new(
            marker,
            canonical_bytes({"position": position.model_dump(), "operation": command.operation}),
        )
        admitted = True
        result = await runtime.handle(
            runtime.authority.sign(
                "execute",
                {
                    "binding": internal_binding,
                    "position": position.model_dump(),
                    "operation": command.operation,
                    "attempt_id": command.operation["operation_id"],
                },
            )
        )
        return 0, {"status": "candidate", "receipt": result["receipt"]}
    except ExecutionRefused:
        raise
    except Exception as exc:
        if admitted and not isinstance(exc, OutcomeUnknown):
            raise OutcomeUnknown("post-admission failure") from exc
        raise
    finally:
        try:
            await runtime.close()
        except Exception as exc:
            if admitted:
                raise OutcomeUnknown("post-admission cleanup failure") from exc
            raise


def quiet_agent():
    """Keep stdout/stderr private through interpreter finalizers; return the control FD."""
    sys.stdout.flush()
    sys.stderr.flush()
    control = os.dup(1)
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    return control


def main():
    control = quiet_agent()
    try:
        command = CoreCommand.model_validate(read_frame(sys.stdin.buffer))
        trusted = CoreBinding.model_validate(strict_json(env("AGENT_CORE_BINDING").encode()))
        if command.binding != trusted:
            raise ValueError("command differs from trusted pod binding")
        sys.addaudithook(process_guard)
        confine.apply()
        code, reply = asyncio.run(run(command))
    except ExecutionRefused as exc:
        code, reply = (
            (3 if exc.reason == "policy" else 4),
            {"status": "refused", "reason": exc.reason},
        )
    except OutcomeUnknown as exc:
        code, reply = 1, {"status": "unknown"}
        if isinstance(exc.__cause__, ExecutionRefused):
            reply["reason"] = exc.__cause__.reason
    except Exception:  # noqa: BLE001 -- never disclose arbitrary agent or credential errors
        code, reply = 5, {"status": "invalid"}
    try:
        with os.fdopen(control, "wb") as output:
            output.write(encode_frame(reply))
            output.flush()
    except Exception:  # noqa: BLE001 -- an unreadable reply must remain uncertain
        # Serialization/output loss cannot turn an execute into a safe retry.
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
