"""One Linux trusted-host boundary; no Temporal client or Activity completion code."""

import asyncio
import ctypes
import fcntl
import json
import os
import signal
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# Intentionally retained until interpreter exit, including finalizers.
_LEASE_FD = None


def parent_death(expected):
    if sys.platform != "linux" or type(expected) is not int or expected <= 1:
        raise ValueError("invalid local supervisor")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError("cannot supervise local child")
    if os.getppid() != expected:
        raise RuntimeError("local supervisor disappeared")


def process_guard(event, args):
    if event in {
        "subprocess.Popen",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.exec",
        "os.system",
    }:
        raise RuntimeError("process creation is outside the local Temporal tool profile")


async def run(request):
    global _LEASE_FD

    from zetra._runtime.artifacts import Position
    from zetra._runtime.local_host import (
        HostSession,
        read_json,
        safe_path,
        sync_directory,
        verify_inputs,
        write_json,
    )

    host = HostSession(
        request["root"],
        request["objects_root"],
        settings=request["settings"],
        session=request["session"],
        message=request["message"],
    )
    _LEASE_FD = os.open(host.root / "lease", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(_LEASE_FD, fcntl.LOCK_EX)
    command = request["command"]
    host.check_binding(command["binding"], host.state())
    settings = host.settings
    verify_inputs(settings)

    # Project imports and all cooperative tool bodies occur after admission and
    # under the process guard. Native syscalls/hostile Python are not isolated.
    sys.addaudithook(process_guard)
    from zetra._runtime.definition import Definition
    from zetra._runtime.runtime import Runtime
    from zetra.local import LocalModelConfig

    config = settings["model_config"]
    config = LocalModelConfig.model_validate(config) if config is not None else None
    model_binding = (f"openai/{config.model}", config.executor()) if config else None
    work_root = safe_path(host.root / "work")
    work_root.mkdir(mode=0o700, exist_ok=True)
    sys.path.insert(0, str(Path(settings["project_root"]) / "src"))
    with tempfile.TemporaryDirectory(dir=work_root) as temporary:
        definition = Definition(
            settings["agent"], Path(temporary) / "workspace", model_binding=model_binding
        )
        inventory = host.root / "inventory.json"
        if inventory.exists():
            if read_json(inventory) != definition.tools:
                raise ValueError("local tool inventory changed")
        else:
            write_json(inventory, definition.tools)
        runtime = Runtime(
            temporary,
            session=host.session,
            manifest=host.manifest,
            key=os.urandom(32),
            message=host.message,
            definition=definition,
            objects_root=host.objects_root,
            generation=command["binding"]["generation"],
        )
        try:
            position = Position.model_validate(command["position"])
            pending = (
                command.get("operation") if request["action"] == "execute" else command["pending"]
            )
            advance = {
                "binding": command["binding"],
                "position": position.model_dump(),
                "pending": pending,
            }
            result = await runtime.handle(runtime.authority.sign("advance", advance))
            if request["action"] == "execute":
                if result != {"status": "operation", "operation": command["operation"]}:
                    raise ValueError("local pending descriptor changed")
                # This is an uncertainty fence, never accepted progress. A missing
                # completion cannot permit another effect even on a new worker.
                attempts = safe_path(host.root / "attempts")
                attempts.mkdir(mode=0o700, exist_ok=True)
                sync_directory(host.root)
                from zetra.manifest import digest

                marker = attempts / digest(command["operation"]["operation_id"])
                if marker.exists():
                    raise RuntimeError("local operation was already attempted")
                write_json(
                    marker, {"operation": command["operation"], "attempt_id": command["attempt_id"]}
                )
                result = await runtime.handle(runtime.authority.sign("execute", command))
                result = result["receipt"]
            elif result["status"] == "done":
                from google.adk.models.llm_response import LlmResponse

                from zetra._runtime.records import decode_model

                text = ""
                for record in reversed(runtime.tape.records(position)):
                    if record["operation"]["kind"] == "model":
                        response = decode_model(LlmResponse, record["outcome"]["value"])
                        if response.content:
                            text = "".join(
                                part.text or ""
                                for part in response.content.parts or []
                                if not part.thought
                            )
                        break
                result = {"status": "done", "result": {"final_text": text}}
            verify_inputs(settings)
            return result
        finally:
            await runtime.close()
            verify_inputs(settings)


def main():
    try:
        request = json.load(sys.stdin)
        parent_death(request["parent_pid"])
        # Keep project/dependency prints and exception details out of the protocol.
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            result = asyncio.run(run(request))
        print(json.dumps(result, allow_nan=False))
    except BaseException as exc:  # noqa: BLE001 -- child boundary never exposes project exceptions
        print(f"local boundary failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
