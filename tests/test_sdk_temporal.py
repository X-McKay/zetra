"""Durable path: the generic workflow, the approval Update, receiver commit and replay.

Runs only when a Temporal CLI is on PATH (or named by ZETRA_TEMPORAL_CLI); temporalio would
otherwise download one from temporal.download, which this suite never does.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
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


async def start_env():
    """Start the local dev server; temporalio gives it 5 s, which a loaded host can miss once."""
    from temporalio.testing import WorkflowEnvironment

    from zetra.sdk.temporal import PydanticAIPlugin

    for attempt in (1, 2):
        try:
            return await WorkflowEnvironment.start_local(
                plugins=[PydanticAIPlugin()], dev_server_existing_path=TEMPORAL_CLI
            )
        except RuntimeError as exc:  # pragma: no cover - only under host load
            if attempt == 2 or "did not start" not in str(exc):
                raise
    raise AssertionError("unreachable")


@unittest.skipUnless(HAVE_SDK and TEMPORAL_CLI, "requires the sdk extra and a Temporal CLI")
class DurableRunTests(unittest.TestCase):
    def test_approval_update_is_validated_and_commits_once(self):
        asyncio.run(self._run())

    async def _run(self) -> None:
        from temporalio.client import WorkflowUpdateFailedError
        from temporalio.exceptions import ApplicationError
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
            (  # one model turn proposing two pages: the second keeps the run waiting
                call("page_oncall", urgency="high", summary="5xx storm"),
                call("page_oncall", urgency="low", summary="follow-up"),
            ),
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
        env = await start_env()
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
                first, second = pending
                shown = [
                    Page(service="checkout-api", urgency="high", summary="5xx storm"),
                    Page(service="checkout-api", urgency="low", summary="follow-up"),
                ]
                self.assertEqual(
                    [p.payload for p in pending], [s.model_dump(mode="json") for s in shown]
                )
                self.assertEqual(first.operation_id, f"{handle.id}:c000001")
                self.assertEqual(pager.commits, [])

                def decision(**overrides) -> t.ApprovalDecision:
                    fields: dict = {
                        "operation_id": first.operation_id,
                        "digest": first.digest,
                        "approved": True,
                        "actor": "oncall:bob",
                    }
                    fields.update(overrides)
                    return t.ApprovalDecision(**fields)

                async def send(d: t.ApprovalDecision) -> t.Decided:
                    return await handle.execute_update(t.APPROVE_UPDATE, d, result_type=t.Decided)

                async def rejected(bad: t.ApprovalDecision) -> str:
                    with self.assertRaises(WorkflowUpdateFailedError) as ctx:
                        await send(bad)
                    cause = ctx.exception.cause
                    assert isinstance(cause, ApplicationError)
                    return cause.type or ""

                self.assertEqual(await rejected(decision(digest=second.digest)), "DigestMismatch")
                self.assertEqual(await rejected(decision(operation_id="run:c999999")), "NotPending")
                self.assertEqual(
                    await rejected(decision(actor="policy:reversible")), "InvalidActor"
                )
                self.assertEqual(await rejected(decision(actor="  ")), "InvalidActor")
                self.assertEqual(
                    await handle.query(t.ZetraAgentWorkflow.pending_approvals), pending
                )
                self.assertEqual(await handle.query(t.ZetraAgentWorkflow.decisions), [])

                recorded = await send(decision())
                self.assertEqual((recorded.approved, recorded.actor), (True, "oncall:bob"))
                # a second decision on the same operation is refused, not merged or overwritten
                late = decision(approved=False, actor="oncall:eve", reason="changed my mind")
                self.assertEqual(await rejected(late), "AlreadyDecided")
                self.assertEqual(pager.commits, [], "nothing commits while a proposal is pending")

                deny = decision(
                    operation_id=second.operation_id,
                    digest=second.digest,
                    approved=False,
                    actor="oncall:eve",
                    reason="one page is enough",
                )
                self.assertFalse((await send(deny)).approved)
                result = await asyncio.wait_for(handle.result(), timeout=60)
                history = await handle.fetch_history()
            self.assertEqual(result.output, {"kind": "paged", "reason": "critical and degraded"})
            self.assertEqual(result.decisions[first.operation_id].actor, "oncall:bob")
            self.assertEqual(result.decisions[second.operation_id].reason, "one page is enough")
            self.assertEqual(pager.commits, shown[:1])
            self.assertEqual(alerts.commits, [])
            self.assertEqual(status.calls, [("read_status", {})])
            accepted = [
                e
                for e in history.events
                if e.HasField("workflow_execution_update_accepted_event_attributes")
            ]
            self.assertEqual(len(accepted), 2, "only the two accepted decisions reach the history")
            replayer = Replayer(
                workflows=[t.ZetraAgentWorkflow],
                plugins=[t.PydanticAIPlugin()],
                workflow_runner=t.workflow_runner([bundle]),
            )
            await replayer.replay_workflow(history)

    def test_unanswered_approval_expires_as_a_durable_timer_and_is_denied(self):
        asyncio.run(self._expiry())

    async def _expiry(self) -> None:
        from temporalio.service import RPCError

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
            env = await start_env()
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
                        operation_id=item.operation_id,
                        digest=item.digest,
                        approved=True,
                        actor="oncall:late",
                    )
                    with self.assertRaises(RPCError):  # the run is closed; nothing to decide
                        await handle.execute_update(t.APPROVE_UPDATE, late, result_type=t.Decided)
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

    def test_worker_killed_after_commit_retries_onto_the_stored_receipt(self):
        asyncio.run(self._crash())

    async def _spawn_worker(self, address: str, queue: str, ledger: Path, mode: str):
        helper = Path(__file__).with_name("_durable_worker.py")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(helper),
            address,
            queue,
            str(ledger),
            mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYDANTIC_AI_NO_BANNER": "1"},
        )
        assert proc.stdout is not None
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=90)
            if not line:
                raise AssertionError(f"{mode} worker exited before it was ready")
            if line.strip() == b"ready":
                return proc

    async def _crash(self) -> None:

        from zetra.sdk import temporal as t
        from zetra.sdk.receiver import SqliteReceiver

        alert = {
            "alert_id": "ALR-20261012",
            "service": "checkout-api",
            "severity": "critical",
            "message": "5xx rate 40%",
        }
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp)
            env = await start_env()
            async with env:
                address = env.client.service_client.config.target_host
                queue = f"zetra-crash-{uuid.uuid4().hex[:6]}"
                first = await self._spawn_worker(address, queue, ledger, "crash")
                second = None
                try:
                    request = t.RunRequest(
                        agent="alert-responder", input=alert, principal="test:crash"
                    )
                    handle = await t.start_run(
                        env.client, queue, request, workflow_id=f"test:{uuid.uuid4().hex[:8]}"
                    )
                    pending: list[t.PendingApproval] = []
                    for _ in range(400):
                        pending = await handle.query(t.ZetraAgentWorkflow.pending_approvals)
                        if pending:
                            break
                        await asyncio.sleep(0.05)
                    [item] = pending
                    decision = t.ApprovalDecision(
                        operation_id=item.operation_id,
                        digest=item.digest,
                        approved=True,
                        actor="oncall:bob",
                    )
                    await handle.execute_update(t.APPROVE_UPDATE, decision, result_type=t.Decided)
                    stdout, _ = await asyncio.wait_for(first.communicate(), timeout=60)
                    self.assertEqual(first.returncode, 137, stdout.decode(errors="replace"))
                    match = re.search(r"committed (\w+)", stdout.decode(errors="replace"))
                    assert match is not None, stdout
                    receipt_id = match.group(1)
                    self.assertEqual(SqliteReceiver("pager", ledger / "pager.db").count(), 1)
                    # The activity is orphaned: the server times it out and schedules a retry,
                    # which the second worker must answer with the receipt already written.
                    second = await self._spawn_worker(address, queue, ledger, "normal")
                    result = await asyncio.wait_for(handle.result(), timeout=120)
                    history = await handle.fetch_history()
                finally:
                    for proc in (first, second):
                        if proc is not None and proc.returncode is None:
                            proc.terminate()
                            await proc.wait()
            self.assertEqual(result.output, {"kind": "paged", "reason": "paged"})
            self.assertEqual(SqliteReceiver("pager", ledger / "pager.db").count(), 1)
            retried = [
                e.activity_task_started_event_attributes
                for e in history.events
                if e.HasField("activity_task_started_event_attributes")
                and e.activity_task_started_event_attributes.attempt > 1
            ]
            self.assertEqual(len(retried), 1, "exactly one activity needed a second attempt")
            self.assertTrue(retried[0].last_failure.HasField("timeout_failure_info"))
            completed = [
                e.activity_task_completed_event_attributes.result.payloads[0].data.decode()
                for e in history.events
                if e.HasField("activity_task_completed_event_attributes")
            ]
            self.assertTrue(
                any(receipt_id in json.dumps(json.loads(c)) for c in completed),
                "the retried write must return the receipt the dead worker wrote",
            )

    def test_expected_failure_completes_the_workflow_with_a_typed_error(self):
        asyncio.run(self._failed())

    async def _failed(self) -> None:
        from temporalio.client import WorkflowExecutionStatus

        from zetra.sdk import temporal as t
        from zetra.sdk.assemble import assemble
        from zetra.sdk.bundle import build
        from zetra.sdk.receiver import MemoryReceiver
        from zetra.sdk.testing import ScriptedModel, StubGateway, final
        from zetra.sdk.tools import Bindings

        bundle = build(REPO / "examples" / "agents" / "alert_responder")
        status = StubGateway("status").stub("read_status", "ok").stub("recent_deploys", "none")
        model = ScriptedModel().script(final("Prose instead of a typed output, every time."))
        agent = assemble(
            bundle,
            Bindings({"pager": MemoryReceiver("pager"), "alerts": MemoryReceiver("alerts")}),
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
            principal="test:failed",
        )
        env = await start_env()
        async with env:
            async with t.worker(env.client, "zetra-test", [entry]):
                handle = await t.start_run(
                    env.client, "zetra-test", request, workflow_id=f"test:{uuid.uuid4().hex[:8]}"
                )
                result = await asyncio.wait_for(handle.result(), timeout=60)  # completes, not fails
                description = await handle.describe()
        self.assertEqual(description.status, WorkflowExecutionStatus.COMPLETED)
        self.assertEqual((result.status, result.output), ("failed", None))
        assert result.error is not None
        self.assertEqual(result.error.code, "output_invalid")
        self.assertIn("retries", result.error.message)
