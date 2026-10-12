"""Worker process for the crash test in ``test_sdk_temporal.py``; not a test module.

Usage: ``python _durable_worker.py <address> <task-queue> <ledger-dir> crash|normal``.
In ``crash`` mode the pager receiver commits through the real SQLite ledger and the process
then exits hard, before the activity result reaches the server, as a worker killed mid-write
would. In ``normal`` mode it serves the same agent against the same ledger.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class CrashAfterCommit:
    """Commit through the wrapped receiver, report the receipt on stdout, then die."""

    def __init__(self, inner):
        self.inner = inner
        self.name = inner.name

    def commit(self, grant, payload):
        receipt = self.inner.commit(grant, payload)
        print(f"committed {receipt.receipt_id}", flush=True)
        os._exit(137)

    def describe(self) -> dict[str, object]:
        return self.inner.describe()


async def serve(address: str, task_queue: str, ledger: Path, mode: str) -> None:
    from zetra.sdk import temporal as t
    from zetra.sdk.assemble import assemble
    from zetra.sdk.bundle import build
    from zetra.sdk.receiver import Receiver, SqliteReceiver
    from zetra.sdk.testing import ScriptedModel, StubGateway, call, final
    from zetra.sdk.tools import Bindings

    bundle = build(REPO / "examples" / "agents" / "alert_responder")
    from alert_responder.contracts import Paged

    pager = SqliteReceiver("pager", ledger / "pager.db")
    receivers: dict[str, Receiver] = {
        "pager": CrashAfterCommit(pager) if mode == "crash" else pager,
        "alerts": SqliteReceiver("alerts", ledger / "alerts.db"),
    }
    model = ScriptedModel().script(
        call("page_oncall", urgency="high", summary="5xx storm"), final(Paged(reason="paged"))
    )
    status = StubGateway("status").stub("read_status", "degraded").stub("recent_deploys", "none")
    agent = assemble(
        bundle,
        Bindings(receivers, tool_timeout=timedelta(seconds=3)),
        model=model.build(),
        durable=True,
        gateway_stubs={"status": status.toolset()},
    )
    entry = t.register(bundle, agent)
    client = await t.connect(address)
    print("ready", flush=True)
    await t.worker(client, task_queue, [entry]).run()


if __name__ == "__main__":
    asyncio.run(serve(sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]))
