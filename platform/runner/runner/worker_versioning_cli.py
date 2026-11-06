"""Operator-only Worker Versioning commands; no worker deletion or scale API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from runner.types import worker_deployment
from runner.worker_inventory import KubernetesInventory
from runner.worker_operations import WorkerOperations
from runner.worker_schedule import CleanupScheduleOperator
from runner.worker_versioning import GuardError, VersioningOperator

MUTATIONS = {"promote", "rollback", "schedule-route"}


def private_json(path, value):
    with Journal(path) as write:
        write(value)


class Journal:
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, "w")
        return self

    def __call__(self, value):
        self.file.write(json.dumps(value, sort_keys=True) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())

    def __exit__(self, *args):
        self.file.close()


def read_plan(path):
    with Path(path).open("rb") as source:
        raw = source.read((1 << 20) + 1)
    if len(raw) > 1 << 20:
        raise GuardError("operation plan exceeds bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise GuardError("operation plan must be an object")
    return value


async def dispatch(args, operator, client):
    ops = WorkerOperations(operator)
    if args.command == "current":
        return await ops.current(args.role, args.pool, args.image)
    if args.command == "prepare":
        return await operator.prepare(
            worker_deployment(operator.tenant, args.role, "governed-v1"), args.build_id, args.image
        )
    if args.command in {"promote", "rollback"}:
        plan = read_plan(args.plan)
        if (
            plan.get("schema") != "worker-versioning-operation-v1"
            or plan.get("namespace") != operator.namespace
        ):
            raise GuardError("promotion plan belongs to another operation or namespace")
        required = {"deployment", "build_id", "image", "current", "conflict_sha256"}
        if not required <= plan.keys():
            raise GuardError("promotion plan missing required fields")
        operator.role(plan["deployment"])
        return await operator.promote(
            plan["deployment"],
            plan["build_id"],
            plan["image"],
            expected_current=plan["current"],
            expected_conflict_sha256=plan["conflict_sha256"],
        )
    if args.command == "inspect-drain":
        return await ops.drain(
            worker_deployment(operator.tenant, args.role, "governed-v1"), args.build_id
        )
    if args.command == "history-receipt":
        return await ops.history_receipt(
            client,
            worker_deployment(operator.tenant, args.role, "governed-v1"),
            args.build_id,
            args.image,
            args.workflow_id,
            args.run_id,
        )
    if args.command == "replay":
        return await ops.replay(
            client,
            worker_deployment(operator.tenant, args.role, "governed-v1"),
            args.build_id,
            args.workflow_id,
            args.run_id,
        )
    schedule = CleanupScheduleOperator(operator.service, operator)
    if args.command == "schedule-inspect":
        return {
            "schema": "cleanup-schedule-plan-v1",
            "namespace": operator.namespace,
            **await schedule.inspect(),
        }
    if args.command == "schedule-route":
        plan = read_plan(args.plan)
        if (
            plan.get("schema") != "cleanup-schedule-plan-v1"
            or plan.get("namespace") != operator.namespace
        ):
            raise GuardError("Schedule plan belongs to another operation or namespace")
        fields = {"expected_queue", "expected_conflict_sha256", "expected_schedule_sha256"}
        if not fields <= plan.keys():
            raise GuardError("Schedule plan missing required fields")
        return await schedule.route(
            args.pool, build_id=args.build_id, image=args.image, **{k: plan[k] for k in fields}
        )
    raise GuardError("unknown operation")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tenant", required=True)
    p.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    commands = p.add_subparsers(dest="command", required=True)
    for name in (
        "current",
        "prepare",
        "inspect-drain",
        "replay",
        "history-receipt",
        "promote",
        "rollback",
        "schedule-inspect",
        "schedule-route",
    ):
        cmd = commands.add_parser(name)
        cmd.add_argument("--output", type=Path)
        if name in {"current", "prepare", "inspect-drain", "replay", "history-receipt"}:
            cmd.add_argument("--role", choices=["sessions", "cleanup"], required=True)
        if name in {"current", "prepare", "history-receipt"}:
            cmd.add_argument("--image", required=True)
        if name == "current":
            cmd.add_argument("--pool", choices=["legacy", "governed-v1"], required=True)
        if name in {"prepare", "inspect-drain", "replay", "history-receipt"}:
            cmd.add_argument("--build-id", required=True)
        if name in MUTATIONS:
            cmd.add_argument("--plan", type=Path, required=True)
            cmd.add_argument("--journal", type=Path, required=True)
        if name == "schedule-route":
            cmd.add_argument("--pool", choices=["legacy", "governed-v1"], required=True)
            cmd.add_argument("--build-id")
            cmd.add_argument("--image")
        if name in {"replay", "history-receipt"}:
            cmd.add_argument("--workflow-id", required=True)
            cmd.add_argument("--run-id", required=True)
    return p


async def run(args):
    from kubernetes import client as k8s, config
    from runner.worker import connect

    if args.kubeconfig:
        config.load_kube_config(config_file=args.kubeconfig)
    else:
        config.load_incluster_config()
    temporal = await connect(args.tenant)
    if temporal.namespace != f"agents-{args.tenant}":
        raise GuardError("Temporal namespace differs from tenant")
    inventory = KubernetesInventory(k8s.AppsV1Api(), k8s.CoreV1Api(), tenant=args.tenant)
    operator = VersioningOperator(temporal.workflow_service, inventory, tenant=args.tenant)
    if args.command in MUTATIONS:
        with Journal(args.journal) as journal:
            operator.record = journal
            return await dispatch(args, operator, temporal)
    return await dispatch(args, operator, temporal)


def main(argv=None):
    import sys

    args = parser().parse_args(argv)
    try:
        result = asyncio.run(run(args))
        if args.output:
            private_json(args.output, result)
        else:
            print(json.dumps(result, sort_keys=True))
        if result.get("status") == "uncertain":
            return 3
        return 1 if result.get("result") == "fail" else 0
    except GuardError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Raw RPC/Kubernetes errors may contain sensitive request context.
        print(
            f"operation failed: {type(exc).__name__}; retain any pending journal", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
