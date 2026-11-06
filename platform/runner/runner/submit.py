"""Start a SessionWorkflow (10.2). Input is validated before anything reaches Temporal."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path

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
    except WorkflowAlreadyStartedError:
        # A resubmission with the same session ID is a no-op that returns the original handle.
        return client.get_workflow_handle_for(SessionWorkflow.run, inp.workflow_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit an agent session")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--session-id", default=None, help="UUID v4; generated when omitted")
    parser.add_argument("--grant-handle", default=None)
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="task as a JSON document")
    task.add_argument("--task-file", type=Path)
    parser.add_argument("--max-boundaries", type=int, default=64)
    parser.add_argument("--seed", default=None, help="snapshot key to restore into generation 1")
    parser.add_argument("--worker-pool", choices=WORKER_POOLS, default="legacy")
    args = parser.parse_args(argv)
    try:
        raw = args.task_file.read_text() if args.task_file else args.task
        inp = SessionInput(
            tenant=args.tenant,
            agent=args.agent,
            release=args.release,
            session_id=args.session_id or str(uuid.uuid4()),
            grant_handle=args.grant_handle,
            task=json.loads(raw),
            max_boundaries=args.max_boundaries,
            seed=args.seed,
            worker_pool=args.worker_pool,
        )
        inp.validate()
    except (InvalidInput, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2

    async def go() -> str:
        from runner.worker import connect

        handle = await submit(await connect(inp.tenant), inp)
        return handle.id

    print(asyncio.run(go()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
