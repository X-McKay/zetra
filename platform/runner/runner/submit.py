"""Start a SessionWorkflow (10.2). Input is validated before anything reaches Temporal."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from dataclasses import asdict

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from runner.types import WORKER_POOLS, InvalidInput, SessionInput, SessionResult
from runner.workflow import EXECUTION_TIMEOUT, SessionWorkflow


async def submit(
    client: Client, inp: SessionInput, *, rpc_timeout: timedelta | None = None
) -> WorkflowHandle[SessionWorkflow, SessionResult]:
    inp.validate()
    if inp.worker_pool != "legacy":
        from runner.types import worker_deployment
        from runner.worker_versioning import UNVERSIONED, GuardError, VersioningOperator

        if client.namespace != f"agents-{inp.tenant}":
            raise GuardError("governed submission namespace differs from tenant")
        operator = VersioningOperator(client.workflow_service, None, tenant=inp.tenant)
        current, _ = await operator.routing(
            worker_deployment(inp.tenant, "sessions", inp.worker_pool)
        )
        if current == UNVERSIONED:
            raise GuardError("governed queue has not been promoted")
    try:
        return await client.start_workflow(
            SessionWorkflow.run,
            inp,
            id=inp.workflow_id,
            task_queue=inp.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            execution_timeout=EXECUTION_TIMEOUT,
            task_timeout=timedelta(seconds=10),
            rpc_timeout=rpc_timeout,
        )
    except WorkflowAlreadyStartedError as exc:
        from runner.operator import ExecutionReceipt, bind_execution

        if not exc.run_id:
            raise ValueError("duplicate did not identify exact run") from None
        receipt = ExecutionReceipt.from_input(client.namespace, inp, exc.run_id)
        return await bind_execution(client, receipt, expected_input=inp, rpc_timeout=rpc_timeout)


def _write_private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        target.write(content)


def main(argv: list[str] | None = None) -> int:
    from runner.operator import (
        ExecutionReceipt,
        positive_timeout,
        read_report_bounded,
        wait_execution,
    )

    parser = argparse.ArgumentParser(description="Submit an agent session")
    parser.add_argument("--tenant")
    parser.add_argument("--agent")
    parser.add_argument("--release")
    parser.add_argument("--session-id")
    parser.add_argument("--grant-handle")
    task = parser.add_mutually_exclusive_group()
    task.add_argument("--task", help="task as a JSON document")
    task.add_argument("--task-file", type=Path)
    parser.add_argument("--max-boundaries", type=int)
    parser.add_argument("--seed")
    parser.add_argument("--worker-pool", choices=WORKER_POOLS)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--wait-timeout", default="600")
    parser.add_argument("--rpc-timeout", default="5")
    parser.add_argument("--cleanup-timeout", default="120")
    parser.add_argument("--cancel-on-timeout", action="store_true")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    try:
        args = parser.parse_args(argv)
        wait_timeout = positive_timeout(args.wait_timeout)
        rpc_timeout = positive_timeout(args.rpc_timeout)
        cleanup_timeout = positive_timeout(args.cleanup_timeout)
        for output in (args.receipt, args.report):
            if output is not None and os.path.lexists(output):
                raise ValueError("output already exists")
        if args.report and (not args.wait or args.artifact_root is None):
            raise ValueError("report requires wait and explicit artifact root")
        if args.cancel_on_timeout and not args.wait:
            raise ValueError("cancel-on-timeout requires wait")
        if args.resume:
            if not args.wait or any(
                getattr(args, key) is not None
                for key in (
                    "tenant",
                    "agent",
                    "release",
                    "session_id",
                    "grant_handle",
                    "task",
                    "task_file",
                    "max_boundaries",
                    "seed",
                    "worker_pool",
                )
            ):
                raise ValueError("resume requires wait and no submission arguments")
            receipt = ExecutionReceipt.parse(json.loads(args.resume.read_text()))
            inp = None
            tenant = receipt.tenant
        else:
            if not all((args.tenant, args.agent, args.release)) or (
                args.task is None and args.task_file is None
            ):
                raise ValueError("tenant, agent, release and task are required")
            raw = args.task_file.read_text() if args.task_file else args.task
            inp = SessionInput(
                tenant=args.tenant,
                agent=args.agent,
                release=args.release,
                session_id=args.session_id or str(uuid.uuid4()),
                grant_handle=args.grant_handle,
                task=json.loads(raw),
                max_boundaries=args.max_boundaries if args.max_boundaries is not None else 64,
                seed=args.seed,
                worker_pool=args.worker_pool or "legacy",
            )
            inp.validate()
            tenant = inp.tenant
            receipt = None
    except SystemExit as exc:
        return int(exc.code)
    except (InvalidInput, ValueError, OSError):
        print("refused: invalid arguments or input", file=sys.stderr)
        return 2

    async def go():
        nonlocal receipt
        from runner.worker import connect

        loop = asyncio.get_running_loop()
        deadline = loop.time() + (wait_timeout if args.wait else rpc_timeout)
        try:
            async with asyncio.timeout(max(0, deadline - loop.time())):
                client = await asyncio.wait_for(connect(tenant), timeout=rpc_timeout)
                if client.namespace != f"agents-{tenant}":
                    raise ValueError("operator namespace differs from tenant")
                if inp is not None:
                    handle = await asyncio.wait_for(
                        submit(client, inp, rpc_timeout=timedelta(seconds=rpc_timeout)),
                        timeout=rpc_timeout,
                    )
                    run_id = handle.first_execution_run_id or handle.run_id
                    receipt = ExecutionReceipt.from_input(client.namespace, inp, run_id)
                # Publish known exact identity before any further remote inspection.
                encoded = json.dumps(asdict(receipt), sort_keys=True)
                print(encoded, file=sys.stderr, flush=True)
                if args.receipt:
                    _write_private(args.receipt, encoded + "\n")
            if not args.wait:
                print(receipt.workflow_id)
                return 0
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            result = await wait_execution(
                client,
                receipt,
                timeout=remaining,
                rpc_timeout=rpc_timeout,
                cleanup_timeout=cleanup_timeout,
                cancel_on_timeout=args.cancel_on_timeout,
            )
            if result.exit_code == 0 and args.report:
                from runner.activities import FilesystemStore

                text = await read_report_bounded(
                    FilesystemStore(args.artifact_root),
                    result.snapshot,
                    timeout=max(0.001, deadline - loop.time()),
                )
                _write_private(args.report, text)
            print(json.dumps(result.envelope(), sort_keys=True))
            return result.exit_code
        except (Exception, asyncio.CancelledError) as exc:
            code = (
                124
                if isinstance(exc, TimeoutError)
                else 130
                if isinstance(exc, asyncio.CancelledError)
                else 1
            )
            envelope = {"error": "operator operation did not complete", "cleanup": "NOT_CONFIRMED"}
            if receipt is not None:
                envelope["receipt"] = asdict(receipt)
            print(json.dumps(envelope, sort_keys=True))
            return code

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        if receipt is not None:
            print(json.dumps({"receipt": asdict(receipt), "cleanup": "NOT_CONFIRMED"}))
        return 130


if __name__ == "__main__":
    sys.exit(main())
