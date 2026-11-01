import asyncio
import hashlib
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from agent_runtime.core_protocol import CoreBinding
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from zetra._runtime.records import canonical_bytes, describe

from runner.core import EMPTY_POSITION
from runner.types import BoundaryOut, SessionInput
from tests.harness import FakeActivities, run_inproc, session_input
from tests.test_core_checkpoint import binding


class CoreActivities(FakeActivities):
    def __init__(
        self, done_after=3, prepare_plan=None, execute_plan=None, checkpoint_plan=None, **kwargs
    ):
        super().__init__(done_at=done_after - 1, **kwargs)
        self.done_after = done_after
        self.prepare_plan = prepare_plan
        self.execute_plan = execute_plan
        self.checkpoint_plan = checkpoint_plan
        self.core_executes = []
        self.checkpoints = []

    def all(self):
        return [
            *super().all(),
            self.create_core_sandbox,
            self.wait_core_ready,
            self.delete_core_sandbox,
            self.prepare_core,
            self.execute_core,
            self.snapshot_core,
        ]

    @activity.defn(name="preflight")
    async def preflight(self, inp: SessionInput) -> dict | None:
        self.log.append(("preflight",))
        b = binding().model_dump()
        release = {k: b[k] for k in ("profile", "image", "manifest_sha256", "policy_sha256")}
        release.update(tenant=inp.tenant, agent=inp.agent, release=inp.release)
        return {"binding": release, "max_boundaries": 64}

    @activity.defn(name="create_core_sandbox")
    async def create_core_sandbox(self, payload: dict) -> str:
        g = payload["binding"]["generation"]
        self.log.append(("create_core", g))
        self.restores[g] = payload["restore"]
        return f"core-{g}"

    @activity.defn(name="wait_core_ready")
    async def wait_core_ready(self, payload: dict) -> None:
        self.log.append(("ready_core", payload["binding"]["generation"]))

    @activity.defn(name="delete_core_sandbox")
    async def delete_core_sandbox(self, payload: dict) -> str:
        self.log.append(("delete_core", payload["generation"]))
        return self.delete_plan(payload["generation"]) if self.delete_plan else "DELETED"

    @activity.defn(name="prepare_core")
    async def prepare_core(self, payload: dict) -> dict:
        g = payload["binding"]["generation"]
        position = payload["position"] or EMPTY_POSITION.model_dump()
        cursor = position["cursor"]
        self.log.append(("prepare_core", g, cursor))
        if self.prepare_plan:
            self.prepare_plan(g, cursor)
        if cursor == self.done_after:
            return {"status": "done", "position": position, "text": "Finished."}
        bound = CoreBinding.model_validate(payload["binding"])
        op = describe(
            "model" if cursor % 2 == 0 else "tool",
            "probe",
            {},
            index=cursor,
            invocation=bound.invocation,
            manifest=bound.manifest,
        )
        return {"status": "operation", "position": position, "operation": op}

    @activity.defn(name="execute_core")
    async def execute_core(self, payload: dict) -> dict:
        g, cursor = payload["binding"]["generation"], payload["position"]["cursor"]
        self.core_executes.append((g, cursor))
        self.log.append(("execute_core", g, cursor))
        if self.block_at == cursor:
            self.blocked.set()
            while True:
                if activity.in_activity():
                    activity.heartbeat()
                await asyncio.sleep(0.05)
        if self.execute_plan:
            result = self.execute_plan(g, cursor)
            if result is not None:
                return result
        previous, op = payload["position"], payload["operation"]
        digest = hashlib.sha256(canonical_bytes(op)).hexdigest()
        position = {
            **previous,
            "cursor": cursor + 1,
            "head": {"sha256": digest, "byte_length": 100},
            "prefix_digest": digest,
        }
        return {
            "status": "candidate",
            "receipt": {
                "previous": previous["head"],
                "operation_id": op["operation_id"],
                "request_digest": op["request_digest"],
                "position": position,
            },
        }

    @activity.defn(name="snapshot_core")
    async def snapshot_core(self, payload: dict) -> dict:
        request = payload["command"]
        g, cursor = request["binding"]["generation"], request["position"]["cursor"]
        self.checkpoints.append((g, cursor))
        self.log.append(("snapshot_core", g, cursor))
        if self.checkpoint_plan:
            self.checkpoint_plan(g, cursor, len(self.checkpoints))
        op = request["operation"]
        out = BoundaryOut(
            cursor, op["kind"], False, "probe" if op["kind"] == "tool" else None, None, "core"
        )
        return {
            "snapshot": hashlib.sha256(canonical_bytes(payload["receipt"])).hexdigest(),
            "position": payload["receipt"]["position"],
            "boundary": asdict(out),
        }


def info(inp, **changes):
    run = "12345678-1234-4234-8234-123456789abc"
    return SimpleNamespace(
        namespace="default",
        workflow_id=inp.workflow_id,
        run_id=run,
        **{
            "attempt": 1,
            "continued_run_id": None,
            "cron_schedule": None,
            "first_execution_run_id": run,
            "original_execution_run_id": run,
            **changes,
        },
    )


async def run_core(monkeypatch, fake, inp=None, *, changes=None, cancel=False):
    inp = inp or session_input()
    monkeypatch.setattr(workflow, "info", lambda: info(inp, **(changes or {})))
    monkeypatch.setattr(workflow, "patched", lambda name: True)
    return await run_inproc(monkeypatch, fake, inp, cancel=cancel)


async def test_core_accepts_only_after_snapshot_and_completes_without_extra_effect(monkeypatch):
    fake = CoreActivities(done_after=1)
    result = await run_core(monkeypatch, fake, session_input(max_boundaries=1))
    assert result.outcome == "COMPLETED"
    assert fake.core_executes == fake.checkpoints == [(1, 0)]
    assert result.result.boundaries == 1
    assert result.result.runtime_profile == "zetra-core-v1"
    assert result.result.final_text == "Finished."
    assert result.result.position["cursor"] == 1
    assert not fake.executed


async def test_prepare_loss_restores_accepted_prefix_after_confirmed_deletion(monkeypatch):
    def prepare(g, cursor):
        if g == 1 and cursor == 1:
            raise ApplicationError(
                "synthetic lost prepare", type="CorePrepareLost", non_retryable=True
            )

    fake = CoreActivities(prepare_plan=prepare)
    result = await run_core(monkeypatch, fake)
    assert result.outcome == "COMPLETED" and result.result.generation == 2
    assert fake.core_executes == [(1, 0), (2, 1), (2, 2)]
    assert fake.restores[2] is not None
    assert fake.log.index(("delete_core", 1)) < fake.log.index(("create_core", 2))


@pytest.mark.parametrize("mode", ["unknown", "activity_error"])
async def test_execute_loss_is_terminal_without_replacement_or_redispatch(monkeypatch, mode):
    def execute(g, cursor):
        if mode == "activity_error":
            raise ApplicationError("lost result", type="SyntheticLost", non_retryable=True)
        return {"status": "unknown"}

    fake = CoreActivities(execute_plan=execute)
    result = await run_core(monkeypatch, fake)
    assert result.outcome == "OUTCOME_UNKNOWN" and result.result.boundaries == 0
    assert fake.core_executes == [(1, 0)] and fake.checkpoints == []
    assert list(fake.restores) == [1]


@pytest.mark.parametrize("succeed", [False, True])
async def test_checkpoint_retries_never_repeat_the_effect(monkeypatch, succeed):
    def checkpoint(g, cursor, count):
        if not succeed or count < 3:
            raise ApplicationError("store unavailable", type="CoreSnapshotFailed")

    fake = CoreActivities(done_after=1, checkpoint_plan=checkpoint)
    result = await run_core(monkeypatch, fake)
    assert result.outcome == ("COMPLETED" if succeed else "OUTCOME_UNKNOWN")
    assert result.result.boundaries == (1 if succeed else 0)
    assert fake.core_executes == [(1, 0)] and fake.checkpoints == [(1, 0)] * 3
    assert list(fake.restores) == [1]


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt": 2},
        {"continued_run_id": "old"},
        {"cron_schedule": "* * * * *"},
        {"first_execution_run_id": "old"},
        {"original_execution_run_id": "old"},
    ],
)
async def test_nonoriginal_core_run_cannot_activate_or_clean_up_another_execution(
    monkeypatch, changes
):
    fake = CoreActivities()
    result = await run_core(monkeypatch, fake, changes=changes)
    assert result.outcome == "REJECTED"
    assert fake.log == [("preflight",)]
    assert not fake.core_executes


async def test_core_cancellation_still_waits_for_cleanup(monkeypatch):
    fake = CoreActivities(block_at=0)
    result = await run_core(monkeypatch, fake, cancel=True)
    assert result.outcome == "CANCELLED"
    assert ("delete_core", 1) in fake.log
    assert result.status["cleanup"]["delete"] == "DELETED"


async def test_prepare_replacement_requires_confirmed_deletion(monkeypatch):
    def prepare(g, cursor):
        if cursor == 1:
            raise ApplicationError("lost prepare", type="CorePrepareLost", non_retryable=True)

    fake = CoreActivities(prepare_plan=prepare, delete_plan=lambda generation: "NOT_CONFIRMED")
    result = await run_core(monkeypatch, fake)
    assert result.outcome == "NOT_CONFIRMED"
    assert fake.core_executes == [(1, 0)] and list(fake.restores) == [1]


@pytest.mark.parametrize("reason,outcome", [("policy", "DENIED"), ("budget", "BUDGET_EXCEEDED")])
async def test_model_refusal_is_terminal_without_an_accepted_boundary(monkeypatch, reason, outcome):
    fake = CoreActivities(execute_plan=lambda g, n: {"status": "refused", "reason": reason})
    result = await run_core(monkeypatch, fake)
    assert result.outcome == outcome and result.result.boundaries == 0
    assert result.result.reason == reason and fake.checkpoints == []


async def test_tool_refusal_preserves_prior_acceptance_and_uncertainty(monkeypatch):
    fake = CoreActivities(
        execute_plan=lambda g, n: {"status": "unknown", "reason": "policy"} if n == 1 else None
    )
    result = await run_core(monkeypatch, fake)
    assert result.outcome == "OUTCOME_UNKNOWN" and result.result.boundaries == 1
    assert result.result.reason == "policy" and fake.core_executes == [(1, 0), (1, 1)]
