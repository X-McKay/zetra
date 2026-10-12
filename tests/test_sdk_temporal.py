"""Durable path: the generic workflow, approval signal, receiver commit and replay.

Runs only when a Temporal CLI is on PATH (or named by ZETRA_TEMPORAL_CLI); temporalio would
otherwise download one from temporal.download, which this suite never does.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

try:
    import pydantic_ai  # noqa: F401
    import temporalio  # noqa: F401

    HAVE_SDK = True
except ImportError:  # pragma: no cover - the sdk extra is optional
    HAVE_SDK = False

REPO = Path(__file__).resolve().parents[1]
TEMPORAL_CLI = os.environ.get("ZETRA_TEMPORAL_CLI") or shutil.which("temporal")


@unittest.skipUnless(HAVE_SDK and TEMPORAL_CLI, "requires the sdk extra and a Temporal CLI")
class DurableRunTests(unittest.TestCase):
    def test_approval_signal_commits_once_and_history_replays(self):
        asyncio.run(self._run())

    async def _run(self) -> None:
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Replayer

        from zetra.sdk import temporal as t
        from zetra.sdk.assemble import assemble
        from zetra.sdk.bundle import build
        from zetra.sdk.receiver import MemoryReceiver
        from zetra.sdk.testing import ScriptedModel, StubGateway, call, final
        from zetra.sdk.tools import Bindings

        bundle = build(REPO / "examples" / "agents" / "alert_responder")  # makes it importable
        from alert_responder.contracts import Page, Paged

        pager, alerts = MemoryReceiver("pager"), MemoryReceiver("alerts")
        status = (
            StubGateway("status").stub("read_status", "degraded").stub("recent_deploys", "none")
        )
        model = ScriptedModel().script(
            call("read_status"),
            call("page_oncall", urgency="high", summary="5xx storm"),
            final(Paged(reason="critical and degraded")),
        )
        agent = assemble(
            bundle,
            Bindings({"pager": pager, "alerts": alerts}),
            model=model.build(),
            durable=True,
            gateway_stubs={"status": status.toolset()},
        )
        entry = t.register(bundle, agent)
        request = t.RunRequest(
            agent=bundle.name,
            input={
                "alert_id": "ALR-20261012",
                "service": "checkout-api",
                "severity": "critical",
                "message": "5xx rate 40%",
            },
            principal="test:durable",
        )
        env = await WorkflowEnvironment.start_local(
            plugins=[t.PydanticAIPlugin()], dev_server_existing_path=TEMPORAL_CLI
        )
        async with env:
            async with t.worker(env.client, "zetra-test", [entry]):
                handle = await t.start_run(
                    env.client,
                    "zetra-test",
                    request,
                    workflow_id=f"test:{uuid.uuid4().hex[:8]}",
                )
                pending: list[t.PendingApproval] = []
                for _ in range(200):
                    pending = await handle.query(t.ZetraAgentWorkflow.pending_approvals)
                    if pending:
                        break
                    await asyncio.sleep(0.05)
                [item] = pending
                expected = Page(service="checkout-api", urgency="high", summary="5xx storm")
                self.assertEqual(item.payload, expected.model_dump(mode="json"))
                self.assertEqual(item.operation_id, f"{handle.id}:c000001")
                self.assertEqual(pager.commits, [])
                decision = t.ApprovalDecision(
                    tool_call_id=item.tool_call_id, approved=True, actor="oncall:bob"
                )
                await handle.signal(t.ZetraAgentWorkflow.approve, decision)
                await handle.signal(t.ZetraAgentWorkflow.approve, decision)  # duplicate is ignored
                result = await asyncio.wait_for(handle.result(), timeout=60)
                history = await handle.fetch_history()
            self.assertEqual(result.output, {"kind": "paged", "reason": "critical and degraded"})
            self.assertEqual(result.decisions[item.operation_id].actor, "oncall:bob")
            self.assertEqual(pager.commits, [expected])
            self.assertEqual(alerts.commits, [])
            self.assertEqual(status.calls, [("read_status", {})])
            replayer = Replayer(
                workflows=[t.ZetraAgentWorkflow],
                plugins=[t.PydanticAIPlugin()],
                workflow_runner=t.workflow_runner([bundle]),
            )
            await replayer.replay_workflow(history)
            self.assertGreater(len(history.events), 20)

    def test_unanswered_approval_expires_as_a_durable_timer_and_is_denied(self):
        asyncio.run(self._expiry())

    async def _expiry(self) -> None:
        from temporalio.service import RPCError
        from temporalio.testing import WorkflowEnvironment

        from zetra.sdk import temporal as t
        from zetra.sdk.assemble import assemble
        from zetra.sdk.bundle import build
        from zetra.sdk.cli import new
        from zetra.sdk.receiver import MemoryReceiver
        from zetra.sdk.testing import ScriptedModel, call, final
        from zetra.sdk.tools import Bindings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sdk_expiry"
            new(root, write=True)
            tool = root / "tools" / "record_note.py"
            source = tool.read_text(encoding="utf-8").replace(
                "@approval\n", '@approval(ttl="PT1S")\n'
            )
            tool.write_text(source, encoding="utf-8")
            bundle = build(root)
            self.assertEqual([d.ttl for d in bundle.tools], ["PT1S"])
            [reply] = bundle.output_members  # the scaffold's Output contract
            notes = MemoryReceiver("notes")
            model = ScriptedModel().script(
                call("record_note", text="remember this"),
                final(reply.model_validate({"text": "nobody answered"})),
            )
            agent = assemble(bundle, Bindings({"notes": notes}), model=model.build(), durable=True)
            entry = t.register(bundle, agent)
            request = t.RunRequest(agent=bundle.name, input={"text": "hi"}, principal="test:expiry")
            env = await WorkflowEnvironment.start_local(
                plugins=[t.PydanticAIPlugin()], dev_server_existing_path=TEMPORAL_CLI
            )
            async with env:
                async with t.worker(env.client, "zetra-test", [entry]):
                    handle = await t.start_run(
                        env.client,
                        "zetra-test",
                        request,
                        workflow_id=f"test:{uuid.uuid4().hex[:8]}",
                    )
                    pending: list[t.PendingApproval] = []
                    for _ in range(200):
                        pending = await handle.query(t.ZetraAgentWorkflow.pending_approvals)
                        if pending:
                            break
                        await asyncio.sleep(0.05)
                    [item] = pending
                    self.assertEqual(item.payload, {"text": "remember this"})
                    result = await asyncio.wait_for(handle.result(), timeout=60)  # nobody answers
                    self.assertEqual(await handle.query(t.ZetraAgentWorkflow.pending_approvals), [])
                    history = await handle.fetch_history()
                    late = t.ApprovalDecision(
                        tool_call_id=item.tool_call_id, approved=True, actor="oncall:late"
                    )
                    with self.assertRaises(
                        RPCError
                    ):  # the run is closed; a late approval has no target
                        await handle.signal(t.ZetraAgentWorkflow.approve, late)
                record = result.decisions[item.operation_id]
                self.assertEqual(
                    (record.approved, record.actor, record.reason),
                    (False, "none", "approval expired"),
                )
                self.assertEqual(result.output, {"kind": "reply", "text": "nobody answered"})
                self.assertEqual(notes.commits, [])
                fired = [e for e in history.events if e.HasField("timer_fired_event_attributes")]
                self.assertEqual(
                    len(fired), 1, "expiry must be a durable timer, not a worker sleep"
                )
