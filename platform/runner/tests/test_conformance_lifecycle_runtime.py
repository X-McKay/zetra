import json
from datetime import UTC, datetime

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.converter import DataConverter

from runner.conformance_lifecycle_runtime import decode_history, validate_grant
from tests.test_conformance_lifecycle_control import configuration


def test_grant_control_is_bound_to_exact_session_and_contains_no_tokens():
    cfg = configuration()
    sid = cfg["sessions"]["success"]
    value = {"session_id": sid, "handle": "gh_opaque"}
    assert validate_grant(value, sid) == "gh_opaque"
    for invalid in (
        {**value, "session_id": "other"},
        {**value, "access_token": "private"},
        {**value, "handle": "eyJtoken.body.signature"},
    ):
        with pytest.raises(ValueError):
            validate_grant(invalid, sid)


async def test_history_allowlists_cleanup_results_and_discards_grant_and_failure_text():
    converter = DataConverter.default
    session = "bound-session"
    arg = {
        "tenant": "tenant-test",
        "agent": "conformance-lifecycle",
        "session_id": session,
        "grant_handle": "sensitive-handle",
        "task": {"lifecycle_case": "success"},
    }
    scheduled = HistoryEvent(event_id=1, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    scheduled.event_time.FromDatetime(datetime.now(UTC))
    scheduled.activity_task_scheduled_event_attributes.activity_type.name = "stop_session"
    scheduled.activity_task_scheduled_event_attributes.input.payloads.extend(
        await converter.encode([arg])
    )
    completed = HistoryEvent(event_id=3, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED)
    completed.event_time.CopyFrom(scheduled.event_time)
    completed.activity_task_completed_event_attributes.scheduled_event_id = 1
    completed.activity_task_completed_event_attributes.result.payloads.extend(
        await converter.encode(["OK"])
    )
    failed = HistoryEvent(event_id=4, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED)
    failed.event_time.CopyFrom(scheduled.event_time)
    failed.activity_task_failed_event_attributes.scheduled_event_id = 1
    failed.activity_task_failed_event_attributes.failure.message = (
        "Bearer private-token-do-not-report"
    )
    failed.activity_task_failed_event_attributes.failure.application_failure_info.type = (
        "APSUnavailable"
    )
    report = await decode_history([scheduled, completed], converter, session)
    assert report["history"][0]["result"] == "OK"
    assert "sensitive-handle" not in json.dumps(report)
    report = await decode_history([scheduled, failed], converter, session)
    assert report["history"][0]["failure_type"] == "APSUnavailable"
    assert "private-token" not in json.dumps(report)
    assert (await decode_history([scheduled, completed], converter, "other"))["history"] == []


async def test_case_retains_partial_evidence_and_runs_cleanup_after_history_failure(monkeypatch):
    from types import SimpleNamespace
    from runner.conformance_lifecycle_runtime import LifecycleHarness

    cfg = configuration()
    cfg["sessions"]["success"] = "00000000-0000-4000-8000-000000000001"
    closed = []

    class Handle:
        first_execution_run_id = "actual-run"

        async def result(self):
            return None

        async def fetch_history_events(self):
            raise RuntimeError("Bearer private-never-report")
            yield

    class Client:
        data_converter = DataConverter.default

        async def start_workflow(self, *args, **kwargs):
            return Handle()

    control = SimpleNamespace(config=cfg)
    harness = LifecycleHarness(Client(), SimpleNamespace(), control, None, cfg["release"])

    async def schedule():
        return None

    async def grant(case):
        return "gh_opaque"

    async def close(*args):
        closed.append(True)

    monkeypatch.setattr(harness, "schedule", schedule)
    monkeypatch.setattr(harness, "grant", grant)
    monkeypatch.setattr(harness.cleanup, "_close", close)
    result = await harness.run_case("success")
    assert result["result"] == "error" and result["error"] == "RuntimeError"
    assert result["evidence"]["session_id"] == cfg["sessions"]["success"]
    assert closed == [True]
    assert "private-never-report" not in json.dumps(result)


@pytest.mark.parametrize(
    ("elapsed", "anchor_offset", "final_offset", "absence_delay", "expected"),
    [
        (1.0, 0.01, 0.1, 0.0, "pass"),
        (650.0, 100.0, 650.0, 0.0, "fail"),
        (650.0, -100.0, 450.0, 0.0, "fail"),
        (590.0, 100.0, 590.0, 20.0, "fail"),
    ],
    ids=["normal", "positive-clock-skew", "negative-clock-skew", "slow-final-404"],
)
async def test_success_case_assembles_independent_api_and_temporal_evidence(
    monkeypatch, elapsed, anchor_offset, final_offset, absence_delay, expected
):
    import time
    from types import SimpleNamespace
    from runner.conformance_lifecycle_runtime import LifecycleHarness

    cfg = configuration()
    sid = cfg["sessions"]["success"]
    converter = DataConverter.default
    now = time.time()
    local_clock = [0.0]
    from runner import conformance_lifecycle_runtime as runtime

    monkeypatch.setattr(
        runtime, "time", SimpleNamespace(time=time.time, monotonic=lambda: local_clock[0])
    )
    inp = {
        "tenant": "tenant-test",
        "agent": "conformance-lifecycle",
        "session_id": sid,
        "grant_handle": "gh_private",
        "task": {"lifecycle_case": "success"},
    }
    history = []

    async def completed(name, arg, value, at):
        scheduled = HistoryEvent(
            event_id=len(history) + 1, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        )
        scheduled.event_time.FromDatetime(datetime.fromtimestamp(at, UTC))
        attrs = scheduled.activity_task_scheduled_event_attributes
        attrs.activity_type.name = name
        attrs.input.payloads.extend(await converter.encode([arg]))
        done = HistoryEvent(
            event_id=len(history) + 2, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED
        )
        done.event_time.FromDatetime(datetime.fromtimestamp(at + 0.001, UTC))
        done.activity_task_completed_event_attributes.scheduled_event_id = scheduled.event_id
        done.activity_task_completed_event_attributes.result.payloads.extend(
            await converter.encode([value])
        )
        history.extend([scheduled, done])

    await completed(
        "run_boundary",
        {"inp": inp, "generation": 1, "boundary": 0},
        {"done": False, "boundary": 0},
        now - 2,
    )
    await completed(
        "snapshot_workspace", {"inp": inp, "generation": 1, "boundary": 0}, "a" * 64, now - 1
    )
    first = list(history)
    for name, value in (
        ("stop_session", "OK"),
        ("delete_grant", "OK"),
        ("delete_sandbox", "DELETED"),
    ):
        arg = {"inp": inp, "generation": 1} if name == "delete_sandbox" else inp
        await completed(name, arg, value, now + anchor_offset)
    done = HistoryEvent(
        event_id=len(history) + 1, event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED
    )
    done.event_time.FromDatetime(datetime.fromtimestamp(now + anchor_offset + 0.01, UTC))
    done.workflow_execution_completed_event_attributes.result.payloads.extend(
        await converter.encode([{"outcome": "COMPLETED", "generation": 1, "boundaries": 2}])
    )
    history.append(done)
    heartbeat = {
        "schema": "t24-gate-v1",
        "case": "success",
        "session_id": sid,
        "stage": "boundary1",
        "generation": 1,
        "worker_uid": "worker-uid",
    }
    gate_payloads = await converter.encode([heartbeat])

    class Handle:
        first_execution_run_id = "actual-run"
        phase = 0

        async def result(self):
            return None

        async def fetch_history_events(self):
            self.phase += 1
            if self.phase > 1:
                local_clock[0] = elapsed
            for event in first if self.phase == 1 else history:
                yield event

        async def describe(self):
            return SimpleNamespace(
                raw_description=SimpleNamespace(
                    pending_activities=[
                        SimpleNamespace(heartbeat_details=SimpleNamespace(payloads=gate_payloads))
                    ]
                )
            )

        async def cancel(self):
            raise AssertionError("completed workflow must not be cancelled")

    handle = Handle()

    class Client:
        data_converter = converter

        async def start_workflow(self, *args, **kwargs):
            assert kwargs["task_queue"] == cfg["queue"]
            assert args[1].grant_handle == "gh_private"
            return handle

    pod = {
        "name": "sandbox",
        "uid": "pod-uid",
        "generation": 1,
        "deleting": False,
        "finalizers": [],
    }
    worker = {
        "name": "worker",
        "uid": "worker-uid",
        "owner_uid": "replicaset-uid",
        "image": cfg["worker_image"],
        "ready": True,
    }

    class Ops:
        poll = 0.01

        def sandbox_rows(self, session):
            return [pod] if handle.phase == 1 else []

        def pods(self, session):
            return self.sandbox_rows(session)

        def workers(self):
            return [worker]

        def absent(self, namespace, pod):
            local_clock[0] += absence_delay
            return {**pod, "http_status": 404}

    class Observer:
        def read(self, session):
            initial = handle.phase == 1
            return {
                "session_id": sid,
                "tenant": "tenant-test",
                "agent": "conformance-lifecycle",
                "observed_at": now if initial else now + final_offset,
                "generation": 1 if initial else 0,
                "generation_present": initial,
                "grant_present": initial,
                "vault_present": initial,
                "refresh_backed": initial,
                "access_current": initial,
                "stopped": not initial,
                "cleanup_pending": False,
                "grant_ttl_seconds": 3600,
                "vault_ttl_seconds": 3600,
            }

    control = SimpleNamespace(
        config=cfg,
        get=lambda key: {"session_id": sid, "handle": "gh_private"}
        if key == "grant-success"
        else None,
    )
    harness = LifecycleHarness(Client(), Ops(), control, Observer(), cfg["release"])

    async def schedule():
        return None

    async def reconciler(*args):
        return []

    harness.schedule = schedule
    harness.reconciler = reconciler
    harness.cleanup.timeout = 0.01
    report = await harness.run_case("success")
    assert report["result"] == expected, report
    assert report["evidence"]["elapsed_monotonic"] == elapsed + absence_delay
    assert "gh_private" not in json.dumps(report)
    if expected == "pass":
        assert report["evidence"]["pod_absence"][0]["http_status"] == 404
