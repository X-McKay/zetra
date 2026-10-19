"""One explicit command surface; durable execution never falls back to host mode."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
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
    for name in ("status", "cancel"):
        cmd = sub.add_parser(name)
        cmd.add_argument("session")
    profile = sub.add_parser("profile")
    profile.add_argument("events")
    qualify = sub.add_parser("qualify")
    qualify.add_argument("--suite", choices=["tests", "latency", "provisioning"], default="tests")
    p.add_argument("--state", default=".zetra/sessions")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from .manifest import load_agent, load_release, source_digest

        if args.command == "init":
            from .scaffold import scaffold

            result = scaffold(args.path, args.name)
        elif args.command == "check":
            spec = load_agent(args.path)
            result = {
                "status": "VALID_METADATA",
                "agent": spec.model_dump(),
                "source_sha256": source_digest(args.path),
                "scope": "static metadata; factory admission occurs inside sandbox",
            }
        elif args.command == "test":
            root = Path(args.path).resolve()
            load_agent(root)
            env = {**os.environ, "PYTHONPATH": str(root / "src")}
            return subprocess.call(
                [sys.executable, "-m", "pytest", str(root / "tests"), "-q"], env=env
            )
        elif args.command in ("build", "release"):
            from .build import build, release_for

            result = (
                build(args.path, tag=args.tag, kind=args.kind)
                if args.command == "build"
                else release_for(args.path, args.image).model_dump()
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
            from .client import SessionClient

            client = SessionClient(
                state=Path(args.state), address=os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
            )
            if args.command == "eval":
                from .evaluation import evaluate

                result = asyncio.run(
                    evaluate(
                        args.path,
                        mode=args.mode,
                        release=load_release(args.release) if args.release else None,
                        client=client,
                    )
                )
            elif args.command == "run":
                result = asyncio.run(
                    client.run(
                        load_release(args.release),
                        session=args.session,
                        message=args.input,
                        workspace=args.workspace,
                    )
                )
            elif args.command == "status":
                result = asyncio.run(client.status(args.session))
            else:
                result = asyncio.run(client.cancel(args.session))
        encoded = json.dumps(result, indent=2) + "\n"
        if getattr(args, "output", None):
            Path(args.output).write_text(encoded)
        else:
            print(encoded, end="")
        if args.command == "run":
            return 0 if result.get("status") == "SUCCEEDED" else 1
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
