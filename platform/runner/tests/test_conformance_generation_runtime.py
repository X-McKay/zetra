import json
import uuid
from types import SimpleNamespace as NS

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.converter import DataConverter

from runner.conformance import parse_args
from runner.conformance_generation_runtime import raw_log, removal_timing
from runner.conformance_lifecycle_runtime import decode_history


def test_root_and_harness_clocks_are_never_subtracted():
    root = {
        "clock_boot_id": str(uuid.uuid4()),
        "removal_started_ns": 10**18,
        "removal_acknowledged_ns": 10**18 + 1000,
    }
    measured = removal_timing(root, 10_000_000_000, 12_000_000_000)
    assert measured["reconciler_after_removal_seconds"] == 2
    assert measured["root_removal_clock"] == root
    for anchor, observed in [(None, 1), (-1, 1), (2, 1)]:
        with pytest.raises(ValueError):
            removal_timing(root, anchor, observed)


def test_t21_selection_is_exclusive_and_requires_exact_run_observation():
    base = [
        "--release",
        "signed",
        "--tests",
        "T21",
        "--generation-run-id",
        str(uuid.uuid4()),
        "--observe-generation-faults",
        "--session-timeout",
        "1200",
    ]
    assert parse_args(base).tests == ["T21"]
    for extra in [
        ["--tests", "T21,T4"],
        ["--tenant", "tenant-other"],
        ["--generation-run-id", "bad"],
        ["--session-timeout", "900"],
    ]:
        with pytest.raises(SystemExit):
            parse_args(base + extra)


def test_raw_log_is_lossless_bounded_and_releases_connection():
    raw = b'{"event":"single-json-line"}\n'
    calls = []
    response = NS(
        read=lambda n: raw,
        close=lambda: calls.append("close"),
        release_conn=lambda: calls.append("release"),
    )

    def read(*args, **kwargs):
        assert kwargs["_preload_content"] is False
        return response

    assert raw_log(NS(read_namespaced_pod_log=read), "ns", "pod") == raw.decode()
    assert calls == ["close", "release"]


async def test_generic_history_keeps_exact_owner_event_order_and_discards_token_material():
    converter = DataConverter.default
    session = str(uuid.uuid4())
    arg = {
        "inp": {
            "tenant": "tenant-test",
            "agent": "conformance-checkpoint",
            "session_id": session,
            "task": {"sensitive": "Bearer private-never-exported"},
        },
        "generation": 1,
    }
    scheduled = HistoryEvent(event_id=1, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    scheduled.activity_task_scheduled_event_attributes.activity_type.name = "delete_sandbox"
    scheduled.activity_task_scheduled_event_attributes.activity_id = "actual-activity"
    scheduled.activity_task_scheduled_event_attributes.input.payloads.extend(
        await converter.encode([arg])
    )
    completed = HistoryEvent(event_id=2, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED)
    completed.activity_task_completed_event_attributes.scheduled_event_id = 1
    completed.activity_task_completed_event_attributes.result.payloads.extend(
        await converter.encode(["NOT_CONFIRMED"])
    )
    decoded = await decode_history(
        [scheduled, completed], converter, session, expected_agent="conformance-checkpoint"
    )
    assert decoded["history"][0]["completed_id"] == 2 and decoded["history"][0]["scheduled_id"] == 1
    assert decoded["history"][0]["activity_id"] == "actual-activity"
    assert "private-never-exported" not in json.dumps(decoded)
    with pytest.raises(ValueError):
        await decode_history([scheduled, completed], converter, session)


@pytest.mark.parametrize("case", ["fence_only", "blocked_deletion", "recovery_control"])
async def test_read_only_orchestration_observes_separate_root_protocol_and_closes_sessions(
    monkeypatch, case
):
    import asyncio
    import copy
    from runner import conformance_generation_runtime as runtime
    from tests.test_conformance_generation import config, data

    cfg = config()
    sid = cfg["sessions"][case]
    seed = data(case)
    seed["initial_pod"]["name"] = runtime.pod_name(sid, 1)
    worker = {"uid": "worker", "name": "worker", "image": cfg["worker_image"]}
    state = {"stage": "initial", "removed": False}
    signals = []
    original_sleep = asyncio.sleep

    async def immediate(fn, *a, **kw):
        return fn(*a, **kw)

    async def sleep(_):
        await original_sleep(0)

    monkeypatch.setattr(runtime.asyncio, "to_thread", immediate)
    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)
    finished = asyncio.Event()

    async def result():
        await finished.wait()
        return NS(outcome=seed.get("outcome", "CANCELLED"))

    async def cancel(**kw):
        state["stage"] = "closed"
        finished.set()

    handle = NS(result=result, cancel=cancel)

    async def start(*a, **kw):
        return handle

    client = NS(start_workflow=start, data_converter=DataConverter.default)
    root_receipt = {
        "run_id": cfg["run_id"],
        "session_id": sid,
        "target_uid": "uid1",
        "finalizer_added": True,
        "finalizer_removed": False,
        "hold_expired": False,
        "operator_errors": [],
    }

    def finalizer_receipt(uid):
        if state["removed"]:
            return dict(
                root_receipt,
                finalizer_removed=True,
                clock_boot_id=str(uuid.uuid4()),
                removal_started_ns=10**18,
                removal_acknowledged_ns=10**18 + 1,
            )
        return root_receipt

    control = NS(config=cfg, read=lambda: None, finalizer_receipt=finalizer_receipt)

    def rows(session):
        if (
            state["stage"] == "closed"
            or state["removed"]
            or (case == "recovery_control" and state["stage"] == "commanded")
        ):
            return []
        pod = copy.deepcopy(seed["initial_pod"])
        if case == "blocked_deletion" and state["stage"] == "commanded":
            pod.update(deleting=True, finalizers=[runtime.FINALIZER])
        return [pod]

    def receipts(*a):
        if state["stage"] == "initial":
            return []
        return [{**r, "session_id": sid} for r in seed.get("worker_receipts", [])]

    ops = NS(
        core=None,
        pods=lambda sid: [],
        rows=rows,
        workers=lambda: [worker],
        receipts=receipts,
        absent=lambda name: state["stage"] == "closed"
        or state["removed"]
        or (case == "recovery_control" and state["stage"] == "commanded"),
    )
    harness = runtime.GenerationHarness(client, ops, control)

    async def validate():
        return worker

    async def schedule():
        return None

    async def history(*a):
        value = {
            "history": seed["history"] if state["stage"] != "initial" else [],
            "terminal": {},
            "progress": {},
        }
        if case != "fence_only" and state["stage"] != "initial":
            value["progress"] = {
                k: seed[k] for k in ("outcome", "generation", "boundaries") if k in seed
            }
        return value

    async def reconciler(*a):
        if state["removed"]:
            return [{**seed["reconciler"][0], "pod_name": seed["initial_pod"]["name"]}]
        return [
            {
                "activity": "delete_orphan",
                "pod_name": seed["initial_pod"]["name"],
                "pending_started": True,
                "completed_at": None,
            }
        ]

    def notify(case, phase, **kw):
        signals.append(phase)
        if phase in {"ready_fence_only", "ready_delete_for_recovery"}:
            state["stage"] = "commanded"
            if case != "fence_only":
                finished.set()
        elif phase == "scheduled_delete_pending_remove_finalizer_now":
            state["removed"] = True

    async def gates(*a):
        return [{"case": case, "run_id": cfg["run_id"], "pod_uid": "uid1", "worker_uid": "worker"}]

    class Traffic:
        replicas = {"aps1": "a", "aps2": "b"}

        def __init__(self, *a):
            pass

        def discover(self):
            pass

        async def sample(self):
            pass

        def allowed(self):
            return True

        def items(self):
            return seed["allowed_requests"] + (
                seed["stale_requests"] if state["stage"] == "commanded" else []
            )

    monkeypatch.setattr(harness, "validate", validate)
    monkeypatch.setattr(harness, "schedule", schedule)
    monkeypatch.setattr(harness, "history", history)
    monkeypatch.setattr(harness, "reconciler", reconciler)
    monkeypatch.setattr(harness, "notify", notify)
    monkeypatch.setattr(runtime, "pending_gates", gates)
    monkeypatch.setattr(runtime, "Traffic", Traffic)
    report = await harness.case(case)
    assert report["result"] == "pass", report
    assert report["evidence"]["cleanup"]["session_closed"] is True
    if case == "blocked_deletion":
        assert "scheduled_delete_pending_remove_finalizer_now" in signals
        assert report["evidence"]["harness_monotonic_bound"] is True
        assert report["evidence"]["outcome"] == "NOT_CONFIRMED"
