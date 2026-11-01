"""SessionWorkflow (10.5) and CleanupReconcilerWorkflow (10.7)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError

with workflow.unsafe.imports_passed_through():
    from runner import activities as act
    from runner import core
    from runner import records
    from runner.types import (
        BoundaryIn,
        BoundaryOut,
        ClosedSession,
        CoreSessionResult,
        GenIn,
        OrphanPod,
        SessionInput,
        SessionResult,
        SnapshotIn,
    )

ONE_ATTEMPT = RetryPolicy(maximum_attempts=1)
TRANSIENT = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    non_retryable_error_types=["IdentityConflict", "Forbidden", "NotFound"],
)
FEW = RetryPolicy(maximum_attempts=5)
THREE = RetryPolicy(maximum_attempts=3)
# The execution timeout is 8 h 15 min; the difference is reserved for cleanup.
BUSINESS_DEADLINE = timedelta(hours=8)
EXECUTION_TIMEOUT = timedelta(hours=8, minutes=15)
MAX_GENERATIONS = 3
REPLACEABLE = {"SandboxLost", "ExecFailed", "OutcomeUnknown", "SnapshotFailed"}


def _record(event: str, **fields) -> None:
    if not workflow.unsafe.is_replaying():
        records.emit(event, **fields)


async def _value(v: str) -> str:
    return v


@workflow.defn
class SessionWorkflow:
    def __init__(self) -> None:
        self.generation = 0
        self.committed: list[BoundaryOut] = []  # committed only once its snapshot is durable
        self.snapshot: str | None = None
        self.state = "SUBMITTED"
        self.cleanup: dict[str, str] = {}
        self.tokens = 0
        self.core_config = None
        self.core_binding = None
        self.core_position = None
        self.core_reason = None
        self.core_text = None

    @workflow.query
    def status(self) -> dict:
        result = {
            "state": self.state,
            "generation": self.generation,
            "boundaries": len(self.committed),
            "cleanup": self.cleanup,
        }
        if self.core_config is not None:
            result.update(
                runtime_profile="zetra-core-v1",
                position=self.core_position,
                reason=self.core_reason,
                final_text=self.core_text,
            )
        return result

    @workflow.run
    async def run(self, inp: SessionInput) -> SessionResult:
        started = workflow.time()
        _record(
            "session_started",
            tenant=inp.tenant,
            agent=inp.agent,
            release=inp.release,
            session_id=inp.session_id,
            delegated=inp.grant_handle is not None,
            grant_handle=inp.grant_handle,
        )
        # The business deadline races the session as a workflow timer. Cancelling the session
        # task, rather than wrapping it in asyncio.wait_for, keeps TIMED_OUT and CANCELLED
        # distinct however the in-flight activity reports its own cancellation.
        session = asyncio.create_task(self._session(inp))
        cancelled: asyncio.CancelledError | None = None
        try:
            done, _ = await workflow.wait([session], timeout=BUSINESS_DEADLINE.total_seconds())
            if session in done:
                outcome = _outcome(session)
            else:
                outcome = "TIMED_OUT"
                await _settle(session)
        except asyncio.CancelledError as e:
            cancelled, outcome = e, "CANCELLED"
            await _settle(session)
        if workflow.patched("cleanup-after-session-activation-v1") and self.generation == 0:
            # A refused original-run guard or preflight has not acquired this session.
            # Old histories keep their recorded cleanup commands via the patch marker.
            self.cleanup = {"stop": "NOT_STARTED", "delete": "DELETED", "grant": "NOT_STARTED"}
            self.state = "CLEANED"
        else:
            await self._cleanup(inp)
        _record(
            "session_finished",
            session_id=inp.session_id,
            outcome=outcome,
            boundaries=len(self.committed),
            cumulative_tokens=self.tokens,
            duration_ms=int((workflow.time() - started) * 1000),
        )
        if cancelled is not None:
            raise cancelled  # the workflow closes as cancelled
        result = dict(
            outcome=outcome,
            cleanup=self.cleanup,
            generation=self.generation,
            boundaries=len(self.committed),
            snapshot=self.snapshot,
        )
        if self.core_config is not None:
            return CoreSessionResult(
                **result,
                position=self.core_position,
                reason=self.core_reason,
                final_text=self.core_text,
            )
        return SessionResult(**result)

    async def _session(self, inp: SessionInput) -> str:
        configuration = await workflow.execute_activity(
            act.preflight, inp, start_to_close_timeout=timedelta(seconds=60), retry_policy=THREE
        )
        if configuration is not None:
            self.core_config = configuration
            try:
                self.core_binding = core.binding_for(inp, configuration, workflow.info(), 1)
            except (ValueError, KeyError, TypeError):
                self.core_reason = "invalid_execution_binding"
                return "REJECTED"
            return await self._core_session(inp)
        await self._provision(inp)
        boundary = len(self.committed)
        while boundary < inp.max_boundaries:
            self.state = "RUNNING"
            try:
                out: BoundaryOut = await workflow.execute_activity(
                    act.run_boundary,
                    BoundaryIn(inp, self.generation, boundary),
                    start_to_close_timeout=timedelta(seconds=300),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=ONE_ATTEMPT,
                )
                snapshot = await workflow.execute_activity(
                    act.snapshot_workspace,
                    SnapshotIn(inp, self.generation, boundary),
                    start_to_close_timeout=timedelta(seconds=120),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=THREE,
                )
            except ActivityError as e:
                if not _replaceable(e) or self.generation >= MAX_GENERATIONS:
                    raise
                # Not committed. Effects are read-only outside the sandbox, so the boundary is
                # re-run on a fresh generation restored from the last committed snapshot.
                _record(
                    "sandbox_replaced",
                    session_id=inp.session_id,
                    old_generation=self.generation,
                    reason=_reason(e),
                )
                await self._replace(inp)
                continue
            self.snapshot = snapshot
            self.committed.append(out)  # committed: result and checkpoint are durable
            self.tokens += out.tokens
            _record(
                "boundary_accepted",
                session_id=inp.session_id,
                generation=self.generation,
                boundary=boundary,
                kind=out.kind,
                tool=out.tool,
                input_tokens=out.usage.input_tokens if out.usage else 0,
                output_tokens=out.usage.output_tokens if out.usage else 0,
                cumulative_tokens=self.tokens,
                snapshot=snapshot,
                summary_sha256=records.summary_hash(out.summary),
            )
            if out.done:
                return "COMPLETED"
            boundary += 1
        return "FAILED"  # boundary limit reached

    async def _core_session(self, inp: SessionInput) -> str:
        await self._provision(inp)
        maximum = min(inp.max_boundaries, self.core_config["max_boundaries"])
        while True:
            self.state = "RUNNING"
            request = {
                "schema_version": 1,
                "binding": self.core_binding,
                "task": core.task_text(inp),
                "action": "prepare",
                "position": self.core_position,
                "operation": None,
            }
            try:
                prepared = await workflow.execute_activity(
                    core.prepare_core,
                    request,
                    start_to_close_timeout=timedelta(seconds=300),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=ONE_ATTEMPT,
                )
            except ActivityError as exc:
                if _reason(exc) == "CorePrepareLost" and self.generation < MAX_GENERATIONS:
                    await self._replace(inp)
                    continue
                raise
            if prepared["position"] != (self.core_position or core.EMPTY_POSITION.model_dump()):
                raise ApplicationError(
                    "prepare changed accepted progress", type="CoreIntegrityError"
                )
            self.core_position = prepared["position"]
            if prepared["status"] == "done":
                self.core_text = prepared["text"]
                return "COMPLETED"
            if len(self.committed) >= maximum:
                self.core_reason = "boundary_limit"
                return "FAILED"
            request = {
                **request,
                "action": "execute",
                "position": self.core_position,
                "operation": prepared["operation"],
            }
            try:
                executed = await workflow.execute_activity(
                    core.execute_core,
                    request,
                    start_to_close_timeout=timedelta(seconds=300),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=ONE_ATTEMPT,
                )
            except ActivityError:
                self.core_reason = "execute_unavailable"
                return "OUTCOME_UNKNOWN"
            if executed["status"] != "candidate":
                self.core_reason = executed.get("reason", "execution_unknown")
                if executed["status"] == "refused" and request["operation"]["kind"] == "model":
                    return "DENIED" if self.core_reason == "policy" else "BUDGET_EXCEEDED"
                return "OUTCOME_UNKNOWN"
            try:
                checkpoint = await workflow.execute_activity(
                    core.snapshot_core,
                    {"command": request, "receipt": executed["receipt"]},
                    start_to_close_timeout=timedelta(seconds=120),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=THREE,
                )
                out = BoundaryOut.parse_strict(checkpoint["boundary"])
                if (
                    checkpoint["position"] != executed["receipt"]["position"]
                    or out.boundary != len(self.committed)
                    or out.done
                    or out.kind != request["operation"]["kind"]
                    or checkpoint["position"]["cursor"] != len(self.committed) + 1
                    or not re.fullmatch(r"[a-f0-9]{64}", checkpoint["snapshot"])
                ):
                    raise ValueError("checkpoint differs from candidate")
            except (ActivityError, ValueError, KeyError, TypeError):
                self.core_reason = "checkpoint_unavailable"
                return "OUTCOME_UNKNOWN"
            self.snapshot = checkpoint["snapshot"]
            self.core_position = checkpoint["position"]
            self.committed.append(out)
            self.tokens += out.tokens
            _record(
                "boundary_accepted",
                session_id=inp.session_id,
                generation=self.generation,
                boundary=out.boundary,
                kind=out.kind,
                tool=out.tool,
                cumulative_tokens=self.tokens,
                snapshot=self.snapshot,
                summary_sha256=records.summary_hash(out.summary),
            )

    async def _provision(self, inp: SessionInput) -> None:
        self.generation += 1
        self.state = "PROVISIONING"
        if self.core_binding is not None:
            self.core_binding = {**self.core_binding, "generation": self.generation}
        # Fence first: from now on only this generation is authorized, even if an older pod
        # lingers.
        await workflow.execute_activity(
            act.set_generation,
            GenIn(inp, self.generation, None),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=FEW,
        )
        if self.core_binding is not None:
            payload = {
                "binding": self.core_binding,
                "restore": self.snapshot,
                "position": self.core_position,
            }
            await workflow.execute_activity(
                core.create_core_sandbox,
                payload,
                schedule_to_close_timeout=timedelta(seconds=600),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=TRANSIENT,
            )
            await workflow.execute_activity(
                core.wait_core_ready,
                payload,
                start_to_close_timeout=timedelta(seconds=180),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=THREE,
            )
            self.state = "READY"
            return
        await workflow.execute_activity(
            act.create_sandbox,
            GenIn(inp, self.generation, self.snapshot),
            schedule_to_close_timeout=timedelta(seconds=600),
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=TRANSIENT,
        )
        await workflow.execute_activity(
            act.wait_ready,
            GenIn(inp, self.generation, self.snapshot),
            start_to_close_timeout=timedelta(seconds=180),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=THREE,
        )
        self.state = "READY"

    async def _replace(self, inp: SessionInput) -> None:
        if await self._delete(inp, self.generation) != "DELETED":
            raise ApplicationError(
                "previous generation not confirmed deleted",
                type="DeletionNotConfirmed",
                non_retryable=True,
            )
        await self._provision(inp)

    async def _delete(self, inp: SessionInput, generation: int) -> str:
        if self.core_binding is not None:
            return await asyncio.shield(
                workflow.execute_activity(
                    core.delete_core_sandbox,
                    {**self.core_binding, "generation": generation},
                    schedule_to_close_timeout=timedelta(seconds=600),
                    start_to_close_timeout=timedelta(seconds=120),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=TRANSIENT,
                )
            )
        return await asyncio.shield(
            workflow.execute_activity(
                act.delete_sandbox,
                GenIn(inp, generation, None),
                schedule_to_close_timeout=timedelta(seconds=600),
                start_to_close_timeout=timedelta(seconds=120),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=TRANSIENT,
            )
        )

    async def _cleanup(self, inp: SessionInput) -> None:
        """Every step runs regardless of the others; outcomes are recorded, never raised."""
        self.state = "CLEANING"
        steps = {
            "stop": workflow.execute_activity(
                act.stop_session,
                inp,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=FEW,
            ),
            "delete": self._delete(inp, self.generation) if self.generation else _value("DELETED"),
            "grant": workflow.execute_activity(
                act.delete_grant,
                inp,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=FEW,
            )
            if inp.grant_handle
            else _value("OK"),
        }
        gathered = asyncio.ensure_future(asyncio.gather(*steps.values(), return_exceptions=True))
        while True:
            try:
                results = await asyncio.shield(gathered)
                break
            except asyncio.CancelledError:
                continue  # a repeated cancel never abandons cleanup
        for name, res in zip(steps, results):
            self.cleanup[name] = (
                f"FAILED:{_reason(res)}" if isinstance(res, BaseException) else str(res)
            )
        ok = all(v in ("OK", "DELETED") for v in self.cleanup.values())
        self.state = "CLEANED" if ok else "NOT_CONFIRMED"


async def _settle(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except BaseException:
        pass


def _outcome(task: asyncio.Task) -> str:
    exc = task.exception()
    if exc is None:
        return task.result()
    if isinstance(exc, ApplicationError) and exc.type == "DeletionNotConfirmed":
        return "NOT_CONFIRMED"
    return "FAILED"


def _replaceable(e: ActivityError) -> bool:
    cause = e.cause
    if isinstance(cause, ApplicationError):
        return cause.type in REPLACEABLE
    # A boundary that hung or whose worker died: the outcome is unknown, same contract.
    return isinstance(cause, TimeoutError)


def _reason(e: BaseException) -> str:
    cause = e.cause if isinstance(e, ActivityError) else e
    if isinstance(cause, ApplicationError) and cause.type:
        return cause.type
    return type(cause).__name__


def session_workflow_id(tenant: str, agent: str, session_id: str) -> str:
    return f"session:{tenant}:{agent}:{session_id}"


@workflow.defn
class CleanupReconcilerWorkflow:
    """Run by a Schedule every 5 minutes, overlap skip, one per tenant (10.7)."""

    @workflow.run
    async def run(self, tenant: str) -> dict:
        inputs = await workflow.execute_activity(
            act.list_reconcile_inputs,
            tenant,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=THREE,
        )
        ids = sorted(
            {session_workflow_id(tenant, p.agent, p.session_id) for p in inputs.pods}
            | {session_workflow_id(tenant, s.agent, s.session_id) for s in inputs.sessions}
        )
        closed = (
            set(
                await workflow.execute_activity(
                    act.closed_workflows,
                    ids,
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=THREE,
                )
            )
            if ids
            else set()
        )
        active = {s.session_id: s.generation for s in inputs.sessions if s.generation is not None}
        orphans = []
        for p in inputs.pods:
            if session_workflow_id(tenant, p.agent, p.session_id) in closed:
                orphans.append(OrphanPod(tenant, p, "workflow_closed"))
            elif p.generation < active.get(p.session_id, p.generation):
                orphans.append(OrphanPod(tenant, p, "generation_stale"))
        now = workflow.now()
        for o in orphans:
            since = o.pod.deleting_since
            if since and now - datetime.fromisoformat(since) > timedelta(minutes=5):
                _record(
                    "orphan_persisting",
                    tenant=tenant,
                    pod=o.pod.name,
                    session_id=o.pod.session_id,
                    generation=o.pod.generation,
                    deleting_since=since,
                )
        deleted = await asyncio.gather(
            *[
                workflow.execute_activity(
                    act.delete_orphan,
                    o,
                    schedule_to_close_timeout=timedelta(seconds=600),
                    start_to_close_timeout=timedelta(seconds=120),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=TRANSIENT,
                )
                for o in orphans
            ],
            return_exceptions=True,
        )
        ended = [
            s
            for s in inputs.sessions
            if session_workflow_id(tenant, s.agent, s.session_id) in closed
        ]
        stopped = await asyncio.gather(
            *[
                workflow.execute_activity(
                    act.close_session,
                    ClosedSession(tenant, s),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=FEW,
                )
                for s in ended
            ],
            return_exceptions=True,
        )
        summary = {
            "orphans_deleted": sum(1 for r in deleted if r == "DELETED"),
            "orphans_unconfirmed": sum(1 for r in deleted if r != "DELETED"),
            "sessions_closed": sum(1 for r in stopped if r == "OK"),
        }
        _record("cleanup_reconciled", tenant=tenant, **summary)
        return summary
