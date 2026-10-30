from __future__ import annotations

import functools

from temporalio import activity
from temporalio.testing import ActivityEnvironment

from runner import activities as act
from runner import workflow as wf
from runner.types import (
    ApsSession,
    ClosedSession,
    GenIn,
    OrphanPod,
    ReconcileInputs,
    SandboxPod,
)
from tests.fakes import SESSION, TENANT, FakeAPS, FakeCore, FakeCustom, FakeNet, FakeStream
from tests.harness import _patch, session_input

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
    assert ("GET", "/v1/sessions?older_than=15m") in aps.calls
