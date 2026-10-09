"""Live local Temporal SDK test-server check: Activity retry, completion and replay.

Downloads the official test server into /tmp when absent. This is not a
Kubernetes, authentication, sandbox, write-adapter or production qualification.
"""

import argparse
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from zetra.temporal_worker import AgentActivities
from zetra.temporal_workflow import AgentWorkflow


async def run(args):
    root = Path(args.root).resolve()
    delegate = AgentActivities(root, {"zetra": "Zetra means ZEro-TRust Agents."})
    calls = []

    @activity.defn(name="zetra.run_agent")
    def retry_once(request: dict) -> dict:
        calls.append(activity.info().attempt)
        if activity.info().attempt == 1:
            raise RuntimeError("Injected transient failure before read-only agent execution")
        return delegate.run_agent(request)

    Path("/tmp/zetra-temporal-bin").mkdir(parents=True, exist_ok=True)
    async with await WorkflowEnvironment.start_time_skipping(
        download_dest_dir="/tmp/zetra-temporal-bin"
    ) as env:
        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(
                env.client,
                task_queue="zetra-local-test",
                workflows=[AgentWorkflow],
                activities=[retry_once],
                activity_executor=executor,
            ):
                handle = await env.client.start_workflow(
                    AgentWorkflow.run,
                    {"key": "zetra"},
                    id="zetra-live-local-retry",
                    task_queue="zetra-local-test",
                )
                result = await handle.result()
                assert result["answer"] == "Zetra means ZEro-TRust Agents.", result
                assert calls == [1, 2], calls
                history = await handle.fetch_history()
                Path(args.history).write_text(history.to_json())
                await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)
    report = {
        "status": "passed",
        "scope": "local-real-temporal-test-server",
        "activityAttempts": calls,
        "checks": [
            "Transient first attempt failed",
            "Second attempt succeeded",
            "Exact known-fixture result",
            "Recorded history replayed",
        ],
        "history": args.history,
        "createdAt": datetime.now(UTC).isoformat(),
        "limitations": [
            "Read-only agent only",
            "Not Kubernetes or sandbox qualification",
            "No production approval/write adapter",
        ],
    }
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", default="examples/knowledge")
    p.add_argument("--history", required=True)
    p.add_argument("--output", required=True)
    asyncio.run(run(p.parse_args()))
