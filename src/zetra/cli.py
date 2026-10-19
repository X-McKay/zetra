"""One explicit command surface; durable execution never falls back to host mode."""

import argparse
import asyncio
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path


def parser():
    p = argparse.ArgumentParser(prog="zetra")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("path")
    init.add_argument("--name", required=True)
    for name in ("check", "test", "build", "release", "eval"):
        cmd = sub.add_parser(name)
        cmd.add_argument("path")
        if name != "test":
            cmd.add_argument("--agent", help="agent name from [tool.zetra.agents]")
        if name == "build":
            cmd.add_argument("--tag", required=True)
            cmd.add_argument(
                "--kind",
                action="store_true",
                help="load only the dedicated zetra-validation cluster",
            )
        if name == "release":
            cmd.add_argument("--image", required=True)
        if name in ("build", "release", "eval"):
            cmd.add_argument("--output")
        if name == "eval":
            cmd.add_argument("--mode", choices=["fixture", "sandbox"], required=True)
            cmd.add_argument("--release")
    run = sub.add_parser("run")
    run.add_argument("release")
    run.add_argument("--session", required=True)
    run.add_argument("--input", required=True)
    run.add_argument("--workspace")
    run.add_argument(
        "--output", help="SDK-owned replaceable export directory for handle, result and workspace"
    )
    for name in ("status", "cancel"):
        cmd = sub.add_parser(name)
        cmd.add_argument("handle", help="path to a SessionHandle JSON file")
    profile = sub.add_parser("profile")
    profile.add_argument("events")
    qualify = sub.add_parser("qualify")
    qualify.add_argument("--suite", choices=["tests", "latency", "provisioning"], default="tests")
    worker = sub.add_parser("worker")
    worker.add_argument("--key-file", required=True)
    worker.add_argument(
        "--release", action="append", default=[], help="approve this release file (repeatable)"
    )
    worker.add_argument(
        "--provider",
        action="append",
        default=[],
        help="native OpenShell provider name (repeatable)",
    )
    worker.add_argument("--graceful-shutdown-seconds", type=grace_seconds, default=20)
    p.add_argument("--artifacts", default=".zetra/objects")
    p.add_argument("--task-queue", default="zetra")
    p.add_argument("--runtime-id", default="local")
    return p


def grace_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("grace must be finite and nonnegative")
    return number


async def _run_worker(worker):
    loop = asyncio.get_running_loop()
    shutdown = None

    def request_shutdown():
        nonlocal shutdown
        if shutdown is None:
            shutdown = asyncio.create_task(worker.shutdown())

    for selected in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(selected, request_shutdown)
    try:
        await worker.run()
    finally:
        if shutdown is not None:
            await shutdown
        for selected in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(selected)


def _write_json(path, value):
    # These are disposable receipts/exports. Temporal remains recovery authority.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(value, indent=2) + "\n")
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


_EXPORT_MARKER = ".zetra-export"
_EXPORT_FORMAT = "zetra-export-v1\n"


def _check_output(path, *, artifacts, workspace):
    output = Path(path)
    marker = output / _EXPORT_MARKER
    if output.is_symlink() or (output / "workspace").is_symlink() or marker.is_symlink():
        raise ValueError("output and workspace must not be symlinks")
    resolved = output.resolve()
    for source in (artifacts, workspace):
        if source is not None:
            backing = Path(source).resolve()
            if resolved == backing or resolved in backing.parents or backing in resolved.parents:
                raise ValueError("export must not overlap input workspace or artifact backing")
    if output.exists() and (
        not output.is_dir()
        or (
            any(output.iterdir()) and (not marker.is_file() or marker.read_text() != _EXPORT_FORMAT)
        )
    ):
        raise ValueError("export directory must be empty or an existing Zetra export")
    return output


def _prepare_output(output):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    # This static marker permits replacing exports, never selecting recovery progress.
    (output / _EXPORT_MARKER).write_text(_EXPORT_FORMAT)
    (output / "result.json").unlink(missing_ok=True)
    workspace = output / "workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    return output


def open_gateway():
    from openshell import SandboxClient, TlsConfig

    tls = Path(os.environ["OPENSHELL_TLS_DIR"])
    return SandboxClient(
        os.environ["OPENSHELL_ADDRESS"],
        tls=TlsConfig(ca_path=tls / "ca.crt", cert_path=tls / "tls.crt", key_path=tls / "tls.key"),
    )


async def execute(args, load_release):
    if args.command == "eval" and args.mode == "fixture":
        from .evaluation import evaluate

        return await evaluate(args.path, mode="fixture", agent=args.agent)

    from temporalio.client import Client

    from ._runtime.artifacts import FileStore
    from .client import SessionClient

    temporal = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    artifacts = FileStore(args.artifacts)
    if args.command == "worker":
        from .worker import create_worker

        gateway = open_gateway()
        try:
            worker = create_worker(
                temporal,
                task_queue=args.task_queue,
                runtime_id=args.runtime_id,
                sandbox_client=gateway,
                artifacts=artifacts,
                key=Path(args.key_file).read_bytes(),
                approved_releases={load_release(path).identity for path in args.release}
                if args.release
                else None,
                providers=args.provider,
                graceful_shutdown_timeout=timedelta(seconds=args.graceful_shutdown_seconds),
            )
            await _run_worker(worker)
        finally:
            gateway.close()
        return {"status": "STOPPED"}

    client = SessionClient(
        temporal,
        task_queue=args.task_queue,
        runtime_id=args.runtime_id,
        artifacts=artifacts,
    )
    if args.command == "eval":
        from .evaluation import evaluate

        return await evaluate(
            args.path,
            mode=args.mode,
            agent=args.agent,
            release=load_release(args.release) if args.release else None,
            client=client,
        )
    if args.command == "run":
        from .client import normalized_result

        output = (
            _check_output(args.output, artifacts=args.artifacts, workspace=args.workspace)
            if args.output
            else None
        )
        handle = await client.submit(
            load_release(args.release),
            session=args.session,
            message=args.input,
            workspace=args.workspace,
        )
        # Emit the handle before waiting so an interrupted CLI can resume inspection.
        print(handle.model_dump_json(), file=sys.stderr, flush=True)
        if output:
            _prepare_output(output)
            _write_json(output / "handle.json", handle.model_dump())
        result = normalized_result(await client.wait(handle))
        if output:
            if result.get("status") == "SUCCEEDED" and result.get("cleanup") == "DELETED":
                files = await client.read_files(handle)
                with tempfile.TemporaryDirectory(dir=output) as directory:
                    staged = Path(directory) / "workspace"
                    staged.mkdir(mode=0o700)
                    for name, content in files.items():
                        target = staged / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                    staged.replace(output / "workspace")
            _write_json(output / "result.json", result)
        return {
            "handle": handle.model_dump(),
            "result": {
                "status": result.get("status"),
                "cleanup": result.get("cleanup"),
                "cursor": (result.get("position") or {}).get("cursor"),
                "final_text": (result.get("result") or {}).get("final_text"),
            },
        }
    from .session import SessionHandle

    handle = SessionHandle.model_validate_json(Path(args.handle).read_text())
    if args.command == "status":
        return await client.status(handle)
    return await client.cancel(handle)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from .manifest import load_agent, load_release, source_digest

        if args.command == "init":
            from .scaffold import scaffold

            result = scaffold(args.path, args.name)
        elif args.command == "check":
            spec = load_agent(args.path, args.agent)
            result = {
                "status": "VALID_METADATA",
                "agent": spec.model_dump(),
                "source_sha256": source_digest(args.path),
                "scope": "static metadata; factory admission occurs inside sandbox",
            }
        elif args.command == "test":
            root = Path(args.path).resolve()
            # Project tests may cover multiple indexed agents.
            from .manifest import project_agents

            names = project_agents(root)
            for name in names:
                load_agent(root, name)
            env = {**os.environ, "PYTHONPATH": str(root / "src")}
            return subprocess.call(
                [sys.executable, "-m", "pytest", str(root / "tests"), "-q"], env=env
            )
        elif args.command in ("build", "release"):
            from .build import build, release_for

            result = (
                build(args.path, tag=args.tag, kind=args.kind, agent=args.agent)
                if args.command == "build"
                else release_for(args.path, args.image, agent=args.agent).model_dump()
            )
        elif args.command == "profile":
            from .policy import profile

            result = profile(args.events)
        elif args.command == "qualify":
            script = Path("scripts/kind_test.py")
            if not script.is_file():
                raise ValueError("qualification runs from the Zetra source checkout")
            target = {
                "tests": "tests",
                "latency": "benchmarks/test_latency.py",
                "provisioning": "benchmarks/test_provisioning.py",
            }[args.suite]
            required = {
                "tests": "BRIDGE_LIVE_VLLM",
                "latency": "BRIDGE_LATENCY",
                "provisioning": "BRIDGE_PROVISIONING",
            }[args.suite]
            if os.environ.get(required) != "1":
                raise ValueError(
                    f"full qualification requires {required}=1; skipped cases are not qualification"
                )
            targets = (
                [target, "benchmarks/test_sdk_latency.py"] if args.suite == "latency" else [target]
            )
            return subprocess.call([sys.executable, str(script), *targets, "-x"])
        else:
            result = asyncio.run(execute(args, load_release))
        encoded = json.dumps(result, indent=2) + "\n"
        if getattr(args, "output", None) and args.command != "run":
            Path(args.output).write_text(encoded)
        else:
            print(encoded, end="")
        if args.command == "run":
            return 0 if result["result"].get("status") == "SUCCEEDED" else 1
        return (
            1
            if result.get("status") in ("FAILED", "OUTCOME_UNKNOWN", "REMEDIATION_REQUIRED")
            else 0
        )
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"zetra: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
