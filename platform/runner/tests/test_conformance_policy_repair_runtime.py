import asyncio
from types import SimpleNamespace as NS

import pytest
from kubernetes.client.exceptions import ApiException

from runner import conformance_policy_repair_runtime as runtime
from tests.test_conformance_policy_repair import evidence


class Ops:
    tenant = "tenant-test"

    def __init__(self, race=False, fail_receipts=False):
        self.race, self.fail_receipts = race, fail_receipts
        self.reads = 0
        self.removed = 0
        self.data = evidence()

    def gate(self, *args):
        pass

    def controller(self):
        return self.data["controller_before"]

    def snapshot(self):
        self.reads += 1
        if self.reads == 2 and not self.race:
            return {
                "digest": "sha256:drift",
                "uids": {"t28-extra": "extra-uid"},
                "names": ["a", "b", "c", "d", "t28-extra"],
            }
        return {**self.data["baseline"], "uids": {}}

    def create(self, name):
        return self.data["extra"]

    def remove_owned(self, extra):
        self.removed += 1
        return False

    def receipts(self, *args):
        if self.fail_receipts:
            raise RuntimeError("unreadable")
        return self.data["receipts"]

    def pods(self, *args):
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "race", "receipt_error", "submit_error"])
async def test_runtime_preserves_races_and_always_cleans_exact_owned_extra(monkeypatch, fault):
    async def immediate(function, *args):
        return function(*args)

    monkeypatch.setattr(runtime.asyncio, "to_thread", immediate)
    ops = Ops(race=fault == "race", fail_receipts=fault == "receipt_error")
    submitted, cancelled = [], []

    async def result():
        return NS(outcome="FAILED")

    async def cancel():
        cancelled.append(True)

    handle = NS(id="session:tenant-test:conformance:sid", result=result, cancel=cancel)

    async def start(client, inp, **kwargs):
        submitted.append(inp)
        if fault == "submit_error":
            raise RuntimeError("submit failed")
        return handle

    monkeypatch.setattr(runtime, "submit", start)
    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: "sid")
    harness = runtime.PolicyRepairHarness(
        NS(get_workflow_handle=lambda wid: handle), ops, "conformance", "signed"
    )

    async def proof(handle, task):
        await task
        return ops.data["session"]

    monkeypatch.setattr(harness, "session_evidence", proof)
    report = await harness.t28()
    assert ops.removed == 1
    assert report["evidence"]["cleanup"]["extra_absent"] is True
    assert report["result"] == ("pass" if fault is None else "fail")
    assert bool(submitted) is (fault != "race")
    assert report["evidence"]["cleanup"]["operator_deleted"] is False


def test_owned_cleanup_uses_uid_precondition_and_refuses_replacement():
    calls = []
    current = NS(metadata=NS(uid="exact"))
    net = NS(
        read_namespaced_network_policy=lambda *a, **kw: current,
        delete_namespaced_network_policy=lambda *a, **kw: calls.append(kw),
    )
    ops = runtime.PolicyRepairOps(None, net, None, "digest")
    assert ops.remove_owned({"name": "extra", "uid": "exact"}) is True
    assert calls[0]["body"]["preconditions"] == {"uid": "exact"}
    current.metadata.uid = "replacement"
    with pytest.raises(ValueError, match="replaced"):
        ops.remove_owned({"name": "extra", "uid": "exact"})
    assert len(calls) == 1


def test_owned_cleanup_404_is_not_an_operator_deletion():
    def absent(*a, **kw):
        raise ApiException(status=404)

    ops = runtime.PolicyRepairOps(None, NS(read_namespaced_network_policy=absent), None, "digest")
    assert ops.remove_owned({"name": "extra", "uid": "exact"}) is False


def test_ambiguous_create_recovers_only_random_run_owned_uid():
    def timeout(*a, **kw):
        raise TimeoutError()

    current = NS(
        metadata=NS(
            name="t28-run",
            uid="exact",
            resource_version="10",
            labels={"agentplatform.io/conformance": "t28-run"},
        )
    )
    ops = runtime.PolicyRepairOps(
        None,
        NS(
            create_namespaced_network_policy=timeout,
            read_namespaced_network_policy=lambda *a, **kw: current,
        ),
        None,
        "digest",
    )
    assert ops.create("t28-run") == {
        "name": "t28-run",
        "uid": "exact",
        "resource_version": "10",
        "creation_confirmed": False,
    }
    current.metadata.labels.clear()
    with pytest.raises(ValueError, match="unowned"):
        ops.create("t28-run")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["create", "submit"])
async def test_cancellation_settles_mutation_and_cleans_owned_objects(monkeypatch, stage):
    ops = Ops()
    began, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def result():
        return NS(outcome="FAILED")

    async def cancel():
        closed.append(True)

    handle = NS(id="session:tenant-test:conformance:sid", result=result, cancel=cancel)

    async def immediate(function, *args):
        value = function(*args)
        if function.__name__ == stage:
            began.set()
            await release.wait()
        return value

    async def start(client, inp, **kwargs):
        if stage == "submit":
            began.set()
            await release.wait()
        return handle

    monkeypatch.setattr(runtime.asyncio, "to_thread", immediate)
    monkeypatch.setattr(runtime, "submit", start)
    harness = runtime.PolicyRepairHarness(None, ops, "conformance", "signed")
    running = asyncio.create_task(harness.t28())
    await began.wait()
    running.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert ops.removed == 1


@pytest.mark.asyncio
async def test_failed_create_response_and_recovery_read_still_clean_owned_extra(monkeypatch):
    ops = Ops()

    def failed_create(name):
        raise TimeoutError()

    ops.create = failed_create
    ops.recover_owned = lambda name: ops.data["extra"]

    async def immediate(function, *args):
        return function(*args)

    monkeypatch.setattr(runtime.asyncio, "to_thread", immediate)
    report = await runtime.PolicyRepairHarness(None, ops, "conformance", "signed").t28()
    assert report["result"] == "fail"
    assert ops.removed == 1
    assert report["evidence"]["cleanup"]["extra_absent"] is True


@pytest.mark.asyncio
async def test_missing_repair_cleans_at_deadline_without_waiting_for_session(monkeypatch):
    ops = Ops()
    original = ops.snapshot

    def snapshot():
        value = original()
        if ops.reads > 2:
            value["valid"] = False
        return value

    ops.snapshot = snapshot
    ticks = iter([0, 0, 0, 0, 61])
    monkeypatch.setattr(runtime, "time", NS(monotonic=lambda: next(ticks, 61)))

    async def immediate(function, *args):
        return function(*args)

    monkeypatch.setattr(runtime.asyncio, "to_thread", immediate)
    waiting = asyncio.Event()

    async def result():
        return NS(outcome="FAILED")

    async def cancel():
        pass

    handle = NS(id="session:tenant-test:conformance:sid", result=result, cancel=cancel)

    async def start(client, inp, **kwargs):
        return handle

    monkeypatch.setattr(runtime, "submit", start)
    harness = runtime.PolicyRepairHarness(None, ops, "conformance", "signed")

    async def pending_proof(handle, task):
        await waiting.wait()
        raise AssertionError("should not await proof after repair deadline")

    monkeypatch.setattr(harness, "session_evidence", pending_proof)
    report = await harness.t28()
    assert report["result"] == "fail" and report["evidence"]["error_type"] == "TimeoutError"
    assert ops.removed == 1


@pytest.mark.asyncio
async def test_real_history_failure_details_bind_preflight_uid(monkeypatch):
    async def immediate(function, *args):
        return function(*args)

    monkeypatch.setattr(runtime.asyncio, "to_thread", immediate)
    from temporalio.api.history.v1 import HistoryEvent
    from temporalio.converter import DataConverter
    from runner.types import SessionResult

    converter = DataConverter.default
    detail = evidence()["session"]["failure"]["details"]
    scheduled = HistoryEvent(
        event_id=1, event_type=runtime.EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    )
    scheduled.activity_task_scheduled_event_attributes.activity_type.name = "preflight"
    failed = HistoryEvent(event_id=2, event_type=runtime.EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED)
    attrs = failed.activity_task_failed_event_attributes
    attrs.scheduled_event_id = 1
    attrs.failure.message = runtime.MISMATCH
    attrs.failure.application_failure_info.type = "Forbidden"
    attrs.failure.application_failure_info.non_retryable = True
    attrs.failure.application_failure_info.details.payloads.extend(await converter.encode([detail]))

    async def history():
        for event in [scheduled, failed]:
            yield event

    async def result():
        return SessionResult("FAILED")

    handle = NS(id="session:tenant-test:conformance:sid", fetch_history_events=history)
    task = asyncio.create_task(result())
    harness = runtime.PolicyRepairHarness(
        NS(data_converter=converter), Ops(), "conformance", "signed"
    )
    actual = await harness.session_evidence(handle, task)
    assert actual["failure"]["details"] == detail
    assert actual["activities"] == ["preflight"]
    assert actual["outcome"] == "FAILED" and actual["pods_absent"] is True


@pytest.mark.parametrize(
    "mode,required,accepted",
    [
        ("agent", None, True),
        ("on-behalf-of", False, True),
        ("on-behalf-of", True, False),
        ("on-behalf-of", None, False),
        ("unknown", False, False),
    ],
)
def test_gate_accepts_only_release_modes_that_need_no_grant(mode, required, accepted):
    status = {"ready": True, "identityMode": mode}
    if required is not None:
        status["delegationRequired"] = required
    custom = NS(
        get_namespaced_custom_object=lambda *args, **kwargs: {
            "spec": {"agent": "conformance"},
            "status": status,
        }
    )
    core = NS(list_namespaced_pod=lambda *args, **kwargs: NS(items=[]))
    ops = runtime.PolicyRepairOps(core, None, custom, "digest")
    if accepted:
        ops.gate("conformance", "signed")
    else:
        with pytest.raises(ValueError):
            ops.gate("conformance", "signed")


@pytest.mark.asyncio
async def test_hanging_start_cannot_keep_allow_all_policy_past_independent_lease(monkeypatch):
    ops = Ops()
    entered, lease_ready, expire, release_start, removed = [asyncio.Event() for _ in range(5)]

    async def immediate(function, *args):
        return function(*args)

    original_sleep = asyncio.sleep

    async def sleep(seconds):
        if seconds > 10:
            assert seconds <= 60
            lease_ready.set()
            await expire.wait()
        else:
            await original_sleep(seconds)

    def remove(extra):
        ops.removed += 1
        removed.set()
        return ops.removed == 1

    ops.remove_owned = remove

    async def result():
        return NS(outcome="FAILED")

    async def cancel():
        pass

    handle = NS(id="session:tenant-test:conformance:sid", result=result, cancel=cancel)

    async def start(client, inp, **kwargs):
        assert kwargs["rpc_timeout"].total_seconds() == 5
        entered.set()
        await release_start.wait()
        return handle

    monkeypatch.setattr(runtime.asyncio, "to_thread", immediate)
    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)
    monkeypatch.setattr(runtime, "submit", start)
    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: "sid")
    harness = runtime.PolicyRepairHarness(None, ops, "conformance", "signed")

    async def proof(handle, task):
        await task
        return ops.data["session"]

    monkeypatch.setattr(harness, "session_evidence", proof)
    running = asyncio.create_task(harness.t28())
    await entered.wait()
    await lease_ready.wait()
    expire.set()
    await removed.wait()
    assert not running.done()  # cleanup was independent of the still-unsettled start
    release_start.set()
    report = await running
    assert report["result"] == "fail"
    assert report["evidence"]["cleanup"]["operator_deleted"] is True
    assert report["evidence"]["cleanup"]["session_closed"] is True
