"""Run against an explicitly started Temporal service and worker; save replay history.

PYTHONPATH=src .venv/bin/python integrations/temporal_smoke.py --history /tmp/history.json
Read-only known-fixture smoke; does not qualify production security or writes.
"""

import argparse
import asyncio
import secrets
from datetime import timedelta
from pathlib import Path

from temporalio.common import WorkflowIDReusePolicy
from temporalio.worker import Replayer

from zetra.temporal_worker import connect
from zetra.temporal_workflow import AgentWorkflow


async def main(args):
    client = await connect(args)
    handle = await client.start_workflow(
        AgentWorkflow.run,
        {"key": "zetra"},
        id="zetra-smoke-" + secrets.token_hex(8),
        task_queue=args.task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        execution_timeout=timedelta(seconds=60),
    )
    result = await asyncio.wait_for(handle.result(), timeout=70)
    if result.get("answer") != "Zetra means ZEro-TRust Agents.":
        raise AssertionError(f"Unexpected read-only result: {result}")
    history = await handle.fetch_history()
    Path(args.history).write_text(history.to_json())
    await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)
    print(f"Read-only workflow completed and replayed: {handle.id}; history: {args.history}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--address", default="127.0.0.1:7233")
    p.add_argument("--namespace", default="default")
    p.add_argument("--task-queue", default="zetra-readonly")
    p.add_argument("--history", required=True)
    for field in ("ca", "cert", "key"):
        p.add_argument(f"--tls-{field}")
    asyncio.run(main(p.parse_args()))
