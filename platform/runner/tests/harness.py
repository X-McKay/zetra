"""Run SessionWorkflow against fake activities on two backends.

`temporal`: the real SDK worker on WorkflowEnvironment.start_time_skipping(). The test server
binary is downloaded on first use, or taken from TEMPORAL_TEST_SERVER_PATH; when neither is
possible the backend skips.

`inproc`: the same workflow code on a plain event loop with execute_activity, wait, time and
is_replaying replaced, including Temporal's error wrapping (ActivityError with the activity's
ApplicationError as cause) and RetryPolicy attempts. It proves the workflow's logic, not
determinism or the SDK's cancellation delivery; the `temporal` backend covers those.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError, TimeoutType

from runner import workflow as wf
from runner.types import (
    BoundaryIn,
    BoundaryOut,
    GenIn,
    SessionInput,
    SessionResult,
    SnapshotIn,
    Usage,
)

TENANT = "tenant-a"


def session_input(grant: str | None = "gh_test", max_boundaries: int = 64) -> SessionInput:
    return SessionInput(
        TENANT,
        "investigator",
        "investigator-2026-10-29-a93557ad",
        str(uuid.uuid4()),
        grant,
        {"goal": "list pods"},
        max_boundaries,
    )


def out(n: int, done: bool = False) -> BoundaryOut:
    return BoundaryOut(
        n,
        "tool" if n % 2 else "model",
        done,
        "pods_list" if n % 2 else None,
        Usage(100, 10),
        f"boundary {n}",
    )


class Hang(Exception):
    """Raised by a plan to simulate a start-to-close timeout of the activity."""


class FakeActivities:
    """Scripted activities. Plans receive (generation, boundary) and return or raise."""

    def __init__(
        self,
        done_at: int = 2,
        boundary_plan=None,
        snapshot_plan=None,
        delete_plan=None,
        stop_error=None,
        grant_error=None,
        preflight_error=None,
        block_at: int | None = None,
    ):
        self.log: list[tuple] = []
        self.executed: list[tuple[int, int]] = []
        self.restores: dict[int, str | None] = {}
        self.done_at = done_at
        self.boundary_plan = boundary_plan
        self.snapshot_plan = snapshot_plan
        self.delete_plan = delete_plan
        self.stop_error = stop_error
        self.grant_error = grant_error
        self.preflight_error = preflight_error
        self.block_at = block_at
        self.blocked = asyncio.Event()
        self.real_timeouts = False

    def all(self):
        return [
            self.preflight,
            self.set_generation,
            self.create_sandbox,
            self.wait_ready,
            self.run_boundary,
            self.snapshot_workspace,
            self.delete_sandbox,
            self.stop_session,
            self.delete_grant,
        ]

    @activity.defn(name="preflight")
    async def preflight(self, inp: SessionInput) -> None:
        self.log.append(("preflight",))
        if self.preflight_error:
            raise self.preflight_error

    @activity.defn(name="set_generation")
    async def set_generation(self, g: GenIn) -> None:
        self.log.append(("set_generation", g.generation))

    @activity.defn(name="create_sandbox")
    async def create_sandbox(self, g: GenIn) -> str:
        self.log.append(("create_sandbox", g.generation))
        self.restores[g.generation] = g.restore
        return f"uid-{g.generation}"

    @activity.defn(name="wait_ready")
    async def wait_ready(self, g: GenIn) -> None:
        self.log.append(("wait_ready", g.generation))
        assert self.restores[g.generation] == g.restore

    @activity.defn(name="run_boundary")
    async def run_boundary(self, b: BoundaryIn) -> BoundaryOut:
        self.log.append(("run_boundary", b.generation, b.boundary))
        self.executed.append((b.generation, b.boundary))
        if self.block_at == b.boundary:
            self.blocked.set()
            while True:
                if activity.in_activity():
                    activity.heartbeat()
                await asyncio.sleep(0.05)
        if self.boundary_plan:
            try:
                res = self.boundary_plan(b.generation, b.boundary)
            except Hang:
                if not self.real_timeouts:
                    raise
                await asyncio.sleep(3600)  # no heartbeat: the server times it out
            if res is not None:
                return res
        return out(b.boundary, done=b.boundary == self.done_at)

    @activity.defn(name="snapshot_workspace")
    async def snapshot_workspace(self, s: SnapshotIn) -> str:
        self.log.append(("snapshot_workspace", s.generation, s.boundary))
        if self.snapshot_plan:
            self.snapshot_plan(s.generation, s.boundary)
        return f"snap-g{s.generation}-b{s.boundary}"

    @activity.defn(name="delete_sandbox")
    async def delete_sandbox(self, g: GenIn) -> str:
        self.log.append(("delete_sandbox", g.generation))
        return self.delete_plan(g.generation) if self.delete_plan else "DELETED"

    @activity.defn(name="stop_session")
    async def stop_session(self, inp: SessionInput) -> str:
        self.log.append(("stop_session",))
        if self.stop_error:
            raise self.stop_error
        return "OK"

    @activity.defn(name="delete_grant")
    async def delete_grant(self, inp: SessionInput) -> str:
        self.log.append(("delete_grant",))
        if self.grant_error:
            raise self.grant_error
        return "OK"

    def names(self) -> list[str]:
        return [e[0] for e in self.log]


@dataclass
class Run:
    outcome: str  # SessionResult.outcome, or CANCELLED
    result: SessionResult | None
    status: dict = field(default_factory=dict)


# --- in-process backend --------------------------------------------------------------------------


def _activity_error(name: str, cause: BaseException) -> ActivityError:
    err = ActivityError(
        "activity task failed",
        scheduled_event_id=0,
        started_event_id=0,
        identity="inproc",
        activity_type=name,
        activity_id="0",
        retry_state=None,
    )
    err.__cause__ = cause
    return err


def _patch(monkeypatch, fake: FakeActivities) -> None:
    by_name = {f.__temporal_activity_definition.name: f for f in fake.all()}

    async def execute_activity(fn, arg, **opts):
        name = fn.__temporal_activity_definition.name
        policy = opts.get("retry_policy")
        attempts = (policy.maximum_attempts if policy else 0) or 10
        for attempt in range(1, attempts + 1):
            try:
                return await by_name[name](arg)
            except Hang:
                raise _activity_error(
                    name,
                    TimeoutError(
                        "activity timeout",
                        type=TimeoutType.START_TO_CLOSE,
                        last_heartbeat_details=[],
                    ),
                )
            except ApplicationError as e:
                final = e.non_retryable or (
                    policy is not None and e.type in (policy.non_retryable_error_types or [])
                )
                if final or attempt == attempts:
                    raise _activity_error(name, e)

    monkeypatch.setattr(workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(workflow, "wait", asyncio.wait)
    monkeypatch.setattr(workflow, "time", time.monotonic)
    monkeypatch.setattr(workflow, "now", lambda: datetime.now(UTC))
    monkeypatch.setattr(workflow.unsafe, "is_replaying", staticmethod(lambda: False))


async def run_inproc(monkeypatch, fake, inp, cancel=False) -> Run:
    _patch(monkeypatch, fake)
    instance = wf.SessionWorkflow()
    task = asyncio.create_task(instance.run(inp))
    if cancel:
        await asyncio.wait_for(fake.blocked.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return Run("CANCELLED", None, instance.status())
    result = await asyncio.wait_for(task, 30)
    return Run(result.outcome, result, instance.status())


# --- Temporal backend -----------------------------------------------------------------------------

_SERVER_UNAVAILABLE: str | None = None


async def _environment():
    from temporalio.testing import WorkflowEnvironment

    global _SERVER_UNAVAILABLE
    if _SERVER_UNAVAILABLE:
        pytest.skip(_SERVER_UNAVAILABLE)
    path = os.environ.get("TEMPORAL_TEST_SERVER_PATH")
    try:
        return await WorkflowEnvironment.start_time_skipping(test_server_existing_path=path)
    except RuntimeError as e:
        _SERVER_UNAVAILABLE = f"Temporal test server unavailable: {e}"
        pytest.skip(_SERVER_UNAVAILABLE)


async def run_temporal(fake, inp, cancel=False, unsandboxed=False) -> Run:
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    env = await _environment()
    fake.real_timeouts = True
    async with env:
        kw = {"workflow_runner": UnsandboxedWorkflowRunner()} if unsandboxed else {}
        async with Worker(
            env.client,
            task_queue=inp.task_queue,
            workflows=[wf.SessionWorkflow],
            activities=fake.all(),
            **kw,
        ):
            handle = await env.client.start_workflow(
                wf.SessionWorkflow.run,
                inp,
                id=inp.workflow_id,
                task_queue=inp.task_queue,
                execution_timeout=wf.EXECUTION_TIMEOUT,
                task_timeout=timedelta(seconds=10),
            )
            if cancel:
                await asyncio.wait_for(fake.blocked.wait(), 30)
                await handle.cancel()
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
                status = await handle.query(wf.SessionWorkflow.status)
                return Run("CANCELLED", None, status)
            result = await handle.result()
            status = await handle.query(wf.SessionWorkflow.status)
            return Run(result.outcome, result, status)
