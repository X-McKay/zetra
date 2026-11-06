from __future__ import annotations

import functools
import asyncio
import threading
from types import SimpleNamespace

import pytest

from temporalio import activity
from temporalio.testing import ActivityEnvironment
from temporalio.exceptions import ApplicationError
from temporalio.client import WorkflowExecutionStatus

from runner import activities as act
from runner import workflow as wf
from runner.types import (
    ApsSession,
    BoundaryIn,
    BoundaryOut,
    ClosedSession,
    GenIn,
    OrphanPod,
    ReconcileInputs,
    SandboxPod,
)
from tests.fakes import SESSION, TENANT, FakeAPS, FakeCore, FakeCustom, FakeNet, FakeStream
from tests.harness import FakeActivities, _environment, _patch, out, session_input

S1, S2, S3 = (f"{n}0000000-0000-4000-8000-000000000000" for n in (1, 2, 3))


class FakeReconciler:
    def __init__(self, inputs: ReconcileInputs, closed: set[str], delete_result="DELETED"):
        self.inputs, self.closed, self.delete_result = inputs, closed, delete_result
        self.deleted: list[tuple[str, str]] = []
        self.stopped: list[str] = []

    def all(self):
        return [
            self.list_reconcile_inputs,
            self.closed_workflows,
            self.delete_orphan,
            self.close_session,
        ]

    @activity.defn(name="list_reconcile_inputs")
    async def list_reconcile_inputs(self, tenant: str) -> ReconcileInputs:
        return self.inputs

    @activity.defn(name="closed_workflows")
    async def closed_workflows(self, ids: list[str]) -> list[str]:
        return [i for i in ids if i.rsplit(":", 1)[1] in self.closed]

    @activity.defn(name="delete_orphan")
    async def delete_orphan(self, o: OrphanPod) -> str:
        self.deleted.append((o.pod.name, o.reason))
        return self.delete_result

    @activity.defn(name="close_session")
    async def close_session(self, c: ClosedSession) -> str:
        self.stopped.append(c.session.session_id)
        return "OK"


async def test_reconciler_deletes_orphans_and_closes_ended_sessions(monkeypatch):
    inputs = ReconcileInputs(
        pods=[
            SandboxPod("p-closed", "investigator", S1, 1),  # workflow closed
            SandboxPod("p-stale", "investigator", S2, 1),  # generation 2 is active
            SandboxPod("p-live", "investigator", S2, 2),
            SandboxPod("p-other", "investigator", S3, 1),  # running, current
        ],
        sessions=[
            ApsSession(S1, "investigator", 1, "gh_1"),
            ApsSession(S2, "investigator", 2, None),
        ],
    )
    fake = FakeReconciler(inputs, closed={S1})
    _patch(monkeypatch, fake)
    summary = await wf.CleanupReconcilerWorkflow().run(TENANT)
    assert sorted(fake.deleted) == [
        ("p-closed", "workflow_closed"),
        ("p-stale", "generation_stale"),
    ]
    assert fake.stopped == [S1]
    assert summary == {"orphans_deleted": 2, "orphans_unconfirmed": 0, "sessions_closed": 1}


async def test_reconciler_counts_unconfirmed_orphans(monkeypatch):
    inputs = ReconcileInputs(
        pods=[SandboxPod("p", "investigator", S1, 1, "2026-01-01T00:00:00+00:00")], sessions=[]
    )
    fake = FakeReconciler(inputs, closed={S1}, delete_result="NOT_CONFIRMED")
    _patch(monkeypatch, fake)
    summary = await wf.CleanupReconcilerWorkflow().run(TENANT)
    assert summary["orphans_unconfirmed"] == 1


def test_list_reconcile_inputs_reads_pods_and_aps(tmp_path):
    core = FakeCore()
    aps = FakeAPS(
        sessions=[
            {
                "session_id": SESSION,
                "tenant": TENANT,
                "agent": "investigator",
                "generation": 2,
                "grant_handle": "gh_1",
            },
            {"session_id": S3, "tenant": "tenant-b", "agent": "x"},
        ]
    )
    act.configure(
        act.Deps(
            core=core,
            net=FakeNet(),
            custom=FakeCustom(),
            aps=aps,
            store=act.FilesystemStore(tmp_path),
            exec=functools.partial(act.kube_exec, core, stream_fn=FakeStream()),
        )
    )
    env = ActivityEnvironment()
    env.run(act.create_sandbox, GenIn(session_input(), 1, None))
    got = env.run(act.list_reconcile_inputs, TENANT)
    assert [(p.agent, p.generation) for p in got.pods] == [("investigator", 1)]
    assert got.sessions == [ApsSession(SESSION, "investigator", 2, "gh_1")]
    assert ("GET", "/v1/sessions") in aps.calls


async def test_reconciler_stops_closed_session_while_pod_deletion_is_blocked(monkeypatch):
    released = asyncio.Event()

    class BlockedDelete(FakeReconciler):
        @activity.defn(name="delete_orphan")
        async def delete_orphan(self, o: OrphanPod) -> str:
            await released.wait()
            return "NOT_CONFIRMED"

        @activity.defn(name="close_session")
        async def close_session(self, c: ClosedSession) -> str:
            self.stopped.append(c.session.session_id)
            released.set()
            return "OK"

    fake = BlockedDelete(
        ReconcileInputs(
            [SandboxPod("p-terminated", "investigator", S1, 1)],
            [ApsSession(S1, "investigator", 1, "gh_1")],
        ),
        closed={S1},
    )
    _patch(monkeypatch, fake)
    monkeypatch.setattr(wf.workflow, "patched", lambda _: True)
    summary = await asyncio.wait_for(wf.CleanupReconcilerWorkflow().run(TENANT), 0.2)
    assert fake.stopped == [S1]
    assert summary["orphans_unconfirmed"] == 1
    assert summary["sessions_closed"] == 1


def test_reconciler_grant_delete_runs_even_if_stop_fails(monkeypatch):
    class StopFails(FakeAPS):
        def post(self, path, body):
            super().post(path, body)
            raise ApplicationError("unavailable", type="APSUnavailable")

    aps = StopFails()
    monkeypatch.setattr(act, "deps", lambda: SimpleNamespace(aps=aps))
    with pytest.raises(ApplicationError):
        ActivityEnvironment().run(
            act.close_session, ClosedSession(TENANT, ApsSession(S1, "investigator", 1, "gh_1"))
        )
    assert ("DELETE", "/v1/grants/gh_1") in aps.calls


def test_reconciler_grant_delete_does_not_wait_for_stop(monkeypatch):
    grant_deleted = threading.Event()

    class SlowStop(FakeAPS):
        def post(self, path, body):
            super().post(path, body)
            assert grant_deleted.wait(2), "grant delete was serialized behind stop"

        def delete(self, path):
            super().delete(path)
            grant_deleted.set()

    aps = SlowStop()
    monkeypatch.setattr(act, "deps", lambda: SimpleNamespace(aps=aps))
    assert ActivityEnvironment().run(
        act.close_session, ClosedSession(TENANT, ApsSession(S1, "investigator", 1, "gh_1"))
    ) == "OK"


async def test_closed_workflows_distinguishes_terminated_and_running(monkeypatch):
    class Handle:
        def __init__(self, status):
            self.status = status

        async def describe(self):
            return SimpleNamespace(status=self.status)

    handles = {
        "terminated": Handle(WorkflowExecutionStatus.TERMINATED),
        "running": Handle(WorkflowExecutionStatus.RUNNING),
    }
    monkeypatch.setattr(
        act, "deps", lambda: SimpleNamespace(temporal=SimpleNamespace(get_workflow_handle=handles.get))
    )
    assert await act.closed_workflows(list(handles)) == ["terminated"]


async def test_terminated_session_skips_own_cleanup_and_reconciler_closes_it(monkeypatch):
    from temporalio.worker import Worker

    release = asyncio.Event()

    class ActiveSession(FakeActivities):
        @activity.defn(name="run_boundary")
        async def run_boundary(self, b: BoundaryIn) -> BoundaryOut:
            self.blocked.set()
            await release.wait()
            return out(b.boundary)

    inp = session_input()
    session = ActiveSession()
    cleanup = FakeReconciler(
        ReconcileInputs(
            [SandboxPod("p-terminated", inp.agent, inp.session_id, 1)],
            [ApsSession(inp.session_id, inp.agent, 1, None)],
        ),
        closed=set(),
    )
    async with await _environment() as env:
        monkeypatch.setattr(act, "deps", lambda: SimpleNamespace(temporal=env.client))
        activities = session.all() + [
            cleanup.list_reconcile_inputs,
            act.closed_workflows,
            cleanup.delete_orphan,
            cleanup.close_session,
        ]
        async with Worker(
            env.client,
            task_queue=inp.task_queue,
            workflows=[wf.SessionWorkflow, wf.CleanupReconcilerWorkflow],
            activities=activities,
        ):
            try:
                handle = await env.client.start_workflow(
                    wf.SessionWorkflow.run, inp, id=inp.workflow_id, task_queue=inp.task_queue
                )
                await asyncio.wait_for(session.blocked.wait(), 10)
                await handle.terminate("test hard termination")
                assert (await handle.describe()).status == WorkflowExecutionStatus.TERMINATED
                result = await env.client.execute_workflow(
                    wf.CleanupReconcilerWorkflow.run,
                    TENANT,
                    id="cleanup-terminated-session",
                    task_queue=inp.task_queue,
                )
                assert cleanup.stopped == [inp.session_id]
                assert cleanup.deleted == [("p-terminated", "workflow_closed")]
                assert result == {
                    "orphans_deleted": 1, "orphans_unconfirmed": 0, "sessions_closed": 1
                }
                assert not {"stop_session", "delete_sandbox", "delete_grant"} & set(session.names())
            finally:
                release.set()


@pytest.mark.parametrize("legacy", [False, True])
async def test_reconciler_replays_old_and_independent_cleanup_histories(monkeypatch, legacy):
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

    fake = FakeReconciler(
        ReconcileInputs(
            [SandboxPod("p-terminated", "investigator", S1, 1)],
            [ApsSession(S1, "investigator", 1, "gh_1")],
        ),
        closed={S1},
    )
    original_patch = wf.workflow.patched
    if legacy:
        monkeypatch.setattr(wf.workflow, "patched", lambda _: False)
    async with await _environment() as env:
        async with Worker(
            env.client,
            task_queue="cleanup-replay",
            workflows=[wf.CleanupReconcilerWorkflow],
            activities=fake.all(),
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await env.client.start_workflow(
                wf.CleanupReconcilerWorkflow.run,
                TENANT,
                id="cleanup-replay",
                task_queue="cleanup-replay",
            )
            summary = await handle.result()
            assert summary == {
                "orphans_deleted": 1, "orphans_unconfirmed": 0, "sessions_closed": 1
            }
            history = await handle.fetch_history()
        monkeypatch.setattr(wf.workflow, "patched", original_patch)
        replay = await Replayer(workflows=[wf.CleanupReconcilerWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None
