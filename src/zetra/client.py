"""Thin Temporal SDK client; execution belongs to independently running workers."""

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path

from temporalio.client import WorkflowFailureError, WorkflowQueryFailedError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError

from ._runtime.artifacts import Position, Ref
from ._runtime.records import canonical_bytes
from ._runtime.workspace import Workspace, validate_snapshot
from .manifest import Release
from .session import SessionHandle, SessionRequest, load_execution


class SessionClient:
    """Submit and inspect executions through a caller-configured Temporal client."""

    def __init__(self, temporal, *, task_queue, runtime_id, artifacts):
        if not task_queue or not runtime_id:
            raise ValueError("task_queue and runtime_id are required")
        self.temporal = temporal
        self.task_queue = task_queue
        self.runtime_id = runtime_id
        self.artifacts = artifacts

    async def submit(self, release: Release, *, session: str, message: str, workspace=None):
        release = Release.model_validate(release.model_dump())
        release.agent.validate_policy()
        if workspace is None:
            snapshot = await asyncio.to_thread(
                self.artifacts.put, canonical_bytes({"version": 1, "files": [], "directories": []})
            )
        else:
            snapshot = await asyncio.to_thread(Workspace(Path(workspace), self.artifacts).capture)
        request = SessionRequest(
            session=session,
            message=message,
            position=Position(workspace=snapshot),
            release=release,
            runtime_id=self.runtime_id,
            task_queue=self.task_queue,
        )
        start = request.start()
        try:
            execution = await self.temporal.start_workflow(
                "SessionWorkflow",
                start,
                id=session,
                task_queue=self.task_queue,
                execution_timeout=timedelta(minutes=30),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            run_id = execution.first_execution_run_id
        except WorkflowAlreadyStartedError as exc:
            run_id = exc.run_id
            if not run_id:
                raise ValueError("duplicate Workflow did not identify its exact run") from exc
            handle = self._handle(session, run_id)
            verify_start(await load_execution(self.temporal, handle), start)
            return handle
        if not run_id:
            raise ValueError("Workflow start did not return an execution run ID")
        return self._handle(session, run_id)

    def _handle(self, workflow_id, run_id):
        return SessionHandle(
            namespace=self.temporal.namespace,
            workflow_id=workflow_id,
            run_id=run_id,
            runtime_id=self.runtime_id,
        )

    async def _execution(self, handle):
        handle = SessionHandle.model_validate(handle)
        if handle.namespace != self.temporal.namespace:
            raise ValueError("handle namespace does not match Temporal client")
        if handle.runtime_id != self.runtime_id:
            raise ValueError("handle runtime does not match client")
        start = await load_execution(self.temporal, handle)
        if start["task_queue"] != self.task_queue:
            raise ValueError("handle task queue does not match client")
        return self.temporal.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)

    async def run(self, release: Release, *, session: str, message: str, workspace=None):
        handle = await self.submit(release, session=session, message=message, workspace=workspace)
        return await self.wait(handle)

    async def wait(self, handle):
        execution = await self._execution(handle)
        try:
            result = dict(await execution.result(follow_runs=False))
            if result.get("cleanup") != "DELETED":
                cleanup = await self._cleanup_result(SessionHandle.model_validate(handle))
                if cleanup is not None:
                    result.update({k: v for k, v in cleanup.items() if k.startswith("cleanup")})
            return normalized_result(result)
        except WorkflowFailureError:
            return await self.reconcile(handle)

    async def status(self, handle):
        execution = await self._execution(handle)
        state = (await execution.describe()).status.name
        result = {
            "handle": SessionHandle.model_validate(handle).model_dump(),
            "workflow_status": state,
            "progress": None,
        }
        if state == "RUNNING":
            try:
                result["progress"] = await execution.query(
                    "progress", rpc_timeout=timedelta(seconds=3)
                )
            except (WorkflowQueryFailedError, RPCError):
                result["progress_available"] = False
        else:
            if state == "COMPLETED":
                outcome = dict(await execution.result(follow_runs=False))
            else:
                outcome = {
                    "status": state,
                    "cleanup": "NOT_CONFIRMED",
                    "effect_status": "OUTCOME_UNKNOWN",
                }
            if outcome.get("cleanup") != "DELETED":
                cleanup = await self._cleanup_result(SessionHandle.model_validate(handle))
                if cleanup is not None:
                    outcome.update({k: v for k, v in cleanup.items() if k.startswith("cleanup")})
            if state != "COMPLETED" and outcome.get("cleanup") != "DELETED":
                outcome.update(status="REMEDIATION_REQUIRED", execution_status=state)
            result["result"] = normalized_result(outcome)
        return result

    async def cancel(self, handle):
        execution = await self._execution(handle)
        await execution.cancel()
        return {
            "handle": SessionHandle.model_validate(handle).model_dump(),
            "status": "CANCELLATION_REQUESTED",
            "cleanup": "NOT_CONFIRMED",
        }

    async def reconcile(self, handle):
        handle = SessionHandle.model_validate(handle)
        execution = await self._execution(handle)
        state = (await execution.describe()).status.name
        if state == "RUNNING":
            raise ValueError("cannot reconcile a running Workflow")
        if state == "COMPLETED":
            result = dict(await execution.result(follow_runs=False))
            if result.get("cleanup") == "DELETED":
                return normalized_result(result)
        else:
            result = {
                "status": state,
                "cleanup": "NOT_CONFIRMED",
                "effect_status": "OUTCOME_UNKNOWN",
            }
        cleanup_id = _cleanup_id(handle)
        try:
            try:
                cleanup = await self.temporal.start_workflow(
                    "CleanupWorkflow",
                    handle.model_dump(),
                    id=cleanup_id,
                    task_queue=self.task_queue,
                    # Only DELETED completes this Workflow. Temporal atomically rejects
                    # replacement of successful proof, including racing clients.
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                    execution_timeout=timedelta(minutes=15),
                )
            except WorkflowAlreadyStartedError as exc:
                if not exc.run_id:
                    raise ValueError("duplicate cleanup did not identify its exact run") from exc
                cleanup = self.temporal.get_workflow_handle(cleanup_id, run_id=exc.run_id)
                await _verify_cleanup(self.temporal, cleanup, exc.run_id, handle, self.task_queue)
            cleanup_result = await cleanup.result(follow_runs=False)
            # A cleanup Workflow can report deletion, never certainty about effects.
            result.update({k: v for k, v in cleanup_result.items() if k.startswith("cleanup")})
        except WorkflowFailureError as exc:
            cause = exc.cause
            if isinstance(cause, ApplicationError) and cause.type == "CleanupNotConfirmed":
                result["cleanup"] = "NOT_CONFIRMED"
            result["cleanup_error_type"] = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - retain visible cleanup uncertainty
            result["cleanup_error_type"] = type(exc).__name__
        if state != "COMPLETED" and result["cleanup"] != "DELETED":
            result.update(status="REMEDIATION_REQUIRED", execution_status=state)
        return normalized_result(result)

    async def _cleanup_result(self, handle):
        """Read the latest cleanup, then pin its run and validate its original target."""
        try:
            latest = self.temporal.get_workflow_handle(_cleanup_id(handle))
            description = await latest.describe()
            if description.status.name != "COMPLETED":
                return None
            execution = self.temporal.get_workflow_handle(
                _cleanup_id(handle), run_id=description.run_id
            )
            await _verify_cleanup(
                self.temporal, execution, description.run_id, handle, self.task_queue
            )
            return await execution.result(follow_runs=False)
        except (RPCError, ValueError, WorkflowFailureError):
            return None

    async def read_files(self, handle):
        result = await self.wait(handle)
        if result.get("status") != "SUCCEEDED" or result.get("cleanup") != "DELETED":
            raise ValueError("final files require successful execution and confirmed deletion")
        data = await asyncio.to_thread(
            self.artifacts.get, Ref.model_validate(result["position"]["workspace"])
        )
        _, files = validate_snapshot(data)
        return {path.as_posix(): content for path, content in files}


def normalized_result(result):
    if result.get("status") == "SUCCEEDED" and result.get("cleanup") != "DELETED":
        return {**result, "status": "REMEDIATION_REQUIRED", "execution_status": "SUCCEEDED"}
    return result


def verify_start(original, expected):
    if original != expected:
        raise ValueError("existing Workflow belongs to another session request or release")


async def _verify_cleanup(client, execution, run_id, handle, task_queue):
    history = await execution.fetch_history()
    if not history.events or not history.events[0].HasField(
        "workflow_execution_started_event_attributes"
    ):
        raise ValueError("cleanup Workflow history is unavailable")
    attrs = history.events[0].workflow_execution_started_event_attributes
    if (
        attrs.workflow_type.name != "CleanupWorkflow"
        or attrs.task_queue.name != task_queue
        or attrs.continued_execution_run_id
        or attrs.original_execution_run_id not in ("", run_id)
        or attrs.first_execution_run_id not in ("", run_id)
        or attrs.attempt > 1
        or attrs.cron_schedule
    ):
        raise ValueError("unexpected cleanup Workflow identity or lineage")
    values = await client.data_converter.decode(attrs.input.payloads, [dict])
    if values != [handle.model_dump()]:
        raise ValueError("cleanup Workflow belongs to another execution")


def _cleanup_id(handle):
    identity = canonical_bytes([handle.namespace, handle.workflow_id, handle.run_id])
    return "zetra-cleanup-" + hashlib.sha256(identity).hexdigest()
