"""Explicit local commands; check is static, eval/run execute trusted project code."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import yaml

from .analysis import risk, scan
from .deployment import catalog, qualify, render
from .evidence import evaluate, read_json, validate_evidence
from .loader import construct
from .manifest import ContractError, load
from .profiling import profile
from .runtime import Denied, RuntimeDependencies


def output(value: object, path: str | None = None) -> None:
    text = json.dumps(value, indent=2, allow_nan=False) + "\n"
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    else:
        print(text, end="")


def artifact_target(root: Path, path: str | None) -> None:
    if not path:
        return
    target = Path(path).resolve()
    if target.is_relative_to(root):
        relative = target.relative_to(root)
        if not relative.parts or relative.parts[0] not in {".zetra", "deploy", "build", "dist"}:
            raise ContractError(
                "Generated artifacts inside the package must be under .zetra/, deploy/, build/ or dist/; use /tmp otherwise"
            )


def init(root: Path, name: str) -> dict:
    if root.exists() and any(root.iterdir()):
        raise ContractError("init requires a new or empty directory")
    template = Path(__file__).parent / "templates" / "knowledge"
    shutil.copytree(
        template, root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    raw = yaml.safe_load((root / "agent.yaml").read_text())
    raw["metadata"]["name"] = name
    from .manifest import Manifest

    Manifest.parse(raw)
    (root / "agent.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    for directory in (
        "policy",
        "docs/risk-assessments",
        "docs/threat-model",
        "docs/runbooks",
        "deploy",
        "evals/datasets/smoke",
        "evals/datasets/regression",
        "evals/datasets/capability",
        "evals/datasets/safety",
        "evals/datasets/adversarial",
        "evals/datasets/durability",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "' + name + '"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
    )
    return {
        "status": "initialized",
        "directory": str(root),
        "next": [f"zetra check {root}", f"zetra eval {root} --output /tmp/{name}-evidence.json"],
        "note": "Replace owner and businessFunction; template scenarios are executable local assertions, not production qualification.",
    }


def is_sdk_package(root: Path) -> bool:
    """An SDK package carries a PydanticAI Agent Spec; a legacy manifest has ``apiVersion``."""
    path = root / "agent.yaml"
    if not path.is_file():
        return False
    return "apiVersion:" not in path.read_text(encoding="utf-8", errors="replace")[:4000]


def sdk_subcommands(subs: argparse._SubParsersAction) -> None:
    """SDK commands are parsed here so ``zetra --help`` works without the ``sdk`` extra."""
    x = subs.add_parser("new", help="Scaffold an SDK agent package (Agent Spec, contracts, test)")
    x.add_argument("directory")
    x.add_argument(
        "--write", action="store_true", help="also scaffold one approval-gated write tool"
    )
    x = subs.add_parser("dev", help="Run an SDK agent durably on a local Temporal dev server")
    x.add_argument("directory")
    x.add_argument("--input", required=True, help="JSON object matching the Input contract")
    x.add_argument("--approve", choices=["prompt", "auto", "deny"], default="prompt")
    x.add_argument("--model", help="model override; 'test' also stubs the gateway servers")
    x.add_argument(
        "--timeout", type=float, default=300.0, help="seconds before the run is terminated"
    )
    x = subs.add_parser("worker", help="Serve SDK agents on a Temporal task queue")
    x.add_argument("directories", nargs="+")
    x.add_argument("--address", default="127.0.0.1:7233")
    x.add_argument("--namespace", default="default")
    x.add_argument("--task-queue", default="zetra-agents")
    x.add_argument("--model")


def sdk_main(args: argparse.Namespace) -> int:
    try:
        from .sdk import cli as sdk_cli
    except ImportError as exc:
        print(f"zetra: the SDK extra is not installed ({exc}); install zetra[sdk]", file=sys.stderr)
        return 1
    return sdk_cli.handle(args)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zetra",
        description="Zetra agent toolkit v0.1; local candidates require independent production qualification",
    )
    subs = p.add_subparsers(dest="command", required=True)
    x = subs.add_parser("init", help="Scaffold a runnable offline agent with real scenario tests")
    x.add_argument("directory")
    x.add_argument("--name", required=True)
    for command in ("check", "eval", "render", "catalog", "run"):
        x = subs.add_parser(
            command,
            help={
                "check": "Static manifest/capability check; SDK packages also write zetra.lock",
                "eval": "Execute trusted local unittest scenarios and record candidate evidence",
                "render": "Generate native Kubernetes candidate with replicas=0",
                "catalog": "Generate unsigned catalog candidate; does not publish",
                "run": "Execute trusted agent locally; cooperative controls only",
            }[command],
        )
        x.add_argument("directory")
        if command in {"check", "render", "catalog"}:
            x.add_argument("--evidence", required=command != "check")
        if command == "check":
            x.add_argument(
                "--frozen",
                action="store_true",
                help="SDK packages: fail instead of rewriting a stale zetra.lock",
            )
        if command != "run":
            x.add_argument("--output")
        if command == "render":
            x.add_argument("--image", required=True)
            x.add_argument("--namespace", required=True)
            x.add_argument("--gateway-namespace", required=True)
            x.add_argument("--gateway-label", required=True)
        if command == "run":
            x.add_argument(
                "--input",
                required=True,
                help="JSON object; write receiver intentionally not configured",
            )
            x.add_argument(
                "--approve",
                choices=["prompt", "auto", "deny"],
                default="prompt",
                help="SDK packages: how approval-gated writes are decided",
            )
            x.add_argument(
                "--model", help="SDK packages: model override; 'test' also stubs the gateways"
            )
    sdk_subcommands(subs)
    x = subs.add_parser(
        "profile", help="Normalize scoped recorded-event fixture into review candidate only"
    )
    x.add_argument("events")
    x.add_argument("--agent", required=True)
    x.add_argument("--image", required=True)
    x.add_argument("--output", required=True)
    x = subs.add_parser("qualify", help="Read-only cluster discovery; does not prove enforcement")
    x.add_argument("--context", required=True)
    x.add_argument("--namespace", required=True)
    x.add_argument(
        "--kubeconfig",
        help="Explicit file for isolated validation cluster; otherwise uses normal kubectl resolution",
    )
    x.add_argument("--output", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command in {"new", "dev", "worker"} or (
        args.command in {"check", "run"} and is_sdk_package(Path(args.directory).resolve())
    ):
        return sdk_main(args)
    try:
        if args.command == "init":
            output(init(Path(args.directory).resolve(), args.name))
            return 0
        if args.command == "profile":
            output(profile(Path(args.events), args.agent, args.image), args.output)
            return 0
        if args.command == "qualify":
            report = qualify(args.context, args.namespace, args.kubeconfig)
            output(report, args.output)
            return 0 if all(p["status"] == "observed" for p in report["probes"].values()) else 1
        root = Path(args.directory).resolve()
        if args.command != "run":
            artifact_target(root, args.output)
        manifest = load(root)
        findings = scan(root, manifest)
        if args.command == "check":
            if args.evidence:
                validate_evidence(root, manifest, read_json(Path(args.evidence)))
            output(
                {
                    "agent": manifest.name,
                    "status": "failed" if findings else "passed",
                    "risk": risk(manifest),
                    "findings": findings,
                    "limitations": "Static hints are incomplete; passing check does not prove runtime security or evaluation coverage.",
                },
                args.output,
            )
            return 1 if findings else 0
        if findings:
            raise ContractError(f"Static check failed: {findings}")
        if args.command == "eval":
            report = evaluate(root, manifest)
            output(report, args.output)
            return 0 if report["status"] == "passed" else 1
        if args.command == "run":
            request = json.loads(args.input)
            if not isinstance(request, dict):
                raise ContractError("Input must be a JSON object")
            deps = RuntimeDependencies(
                frozenset(manifest.capabilities),
                manifest.max_steps,
                manifest.max_seconds,
                manifest.max_cost_usd,
                root / ".zetra" / "STOP",
                knowledge={"zetra": "Zetra means ZEro-TRust Agents."},
            )
            result = construct(root, deps).run(request)
            if not isinstance(result, dict):
                raise ContractError("Agent result must be a JSON object")
            output(result)
            return 0
        evidence = read_json(Path(args.evidence))
        if args.command == "catalog":
            output(catalog(root, manifest, evidence), args.output)
            return 0
        docs = render(
            root,
            manifest,
            evidence,
            image=args.image,
            namespace=args.namespace,
            gateway_namespace=args.gateway_namespace,
            gateway_label=args.gateway_label,
        )
        text = yaml.safe_dump_all(docs, sort_keys=False)
        if args.output:
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        else:
            print(text, end="")
        return 0
    except (ContractError, Denied, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"zetra: {exc}", file=sys.stderr)
        return 1
