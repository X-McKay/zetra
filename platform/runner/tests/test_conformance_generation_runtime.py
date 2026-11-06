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
            "terminal": (
                {"kind": "CANCELED" if case == "fence_only" else "COMPLETED"}
                if state["stage"] == "closed"
                or (case != "fence_only" and state["stage"] != "initial")
                else {}
            ),
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


def test_ops_use_actual_kubernetes_models_and_api_client_without_transport_calls():
    from kubernetes import client
    from kubernetes.client.exceptions import ApiException
    from runner.conformance_generation_runtime import GenerationOps
    from tests.test_conformance_generation import config

    cfg = config()
    sid = cfg["sessions"]["fence_only"]
    api = client.ApiClient()
    sandbox = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name="g1", uid="uid1", labels={"agentplatform.io/generation": "1"}
        ),
        status=client.V1PodStatus(phase="Running"),
    )
    image = cfg["worker_image"]
    worker = client.V1Pod(
        metadata=client.V1ObjectMeta(name="worker", uid="worker-uid"),
        spec=client.V1PodSpec(
            containers=[client.V1Container(name="runner", image=image)],
            service_account_name="session-runner",
        ),
        status=client.V1PodStatus(
            phase="Running",
            container_statuses=[
                client.V1ContainerStatus(
                    name="runner",
                    image=image,
                    image_id="runtime@sha256:" + "a" * 64,
                    ready=True,
                    restart_count=0,
                )
            ],
        ),
    )

    def pods(namespace, **kwargs):
        return client.V1PodList(items=[worker if namespace == "agent-platform" else sandbox])

    core = NS(
        api_client=api, list_namespaced_pod=pods, read_namespaced_pod=lambda *a, **kw: sandbox
    )
    ops = GenerationOps(core, None, NS(config=cfg))
    assert ops.rows(sid) == [
        {
            "name": "g1",
            "uid": "uid1",
            "generation": 1,
            "phase": "Running",
            "deleting": False,
            "finalizers": [],
        }
    ]
    actual = ops.workers()
    assert (
        actual[0]["service_account"] == "session-runner"
        and actual[0]["image"] == image
        and actual[0]["ready"] is True
    )
    assert ops.absent("g1") is False

    def absent(*a, **kw):
        raise ApiException(status=404)

    core.read_namespaced_pod = absent
    assert ops.absent("g1") is True


async def test_pending_gate_uses_actual_temporal_heartbeat_protobuf():
    from temporalio.api.workflow.v1 import PendingActivityInfo
    from runner.conformance_generation_runtime import pending_gates

    converter = DataConverter.default
    sid = str(uuid.uuid4())
    gate = {
        "schema": "t21-gate-v1",
        "case": "blocked_deletion",
        "run_id": str(uuid.uuid4()),
        "session_id": sid,
        "pod_uid": "pod",
        "worker_uid": "worker",
    }
    pending = PendingActivityInfo()
    pending.heartbeat_details.payloads.extend(await converter.encode([gate]))

    async def describe(**kwargs):
        assert kwargs["rpc_timeout"].total_seconds() == 5
        return NS(raw_description=NS(pending_activities=[pending]))

    assert await pending_gates(NS(describe=describe), converter, sid) == [gate]
    assert await pending_gates(NS(describe=describe), converter, "another-session") == []


async def test_history_uses_shared_exact_caps_and_finite_rpc_budget(monkeypatch):
    from runner import conformance_generation_runtime as runtime

    event = HistoryEvent(event_id=1)

    class Pages:
        current_page = None
        next_page_token = None

        async def fetch_next_page(self):
            self.current_page = [event]

    observed = {}

    def fetch(**kwargs):
        observed.update(kwargs)
        return Pages()

    handle = NS(id="workflow", fetch_history_events=fetch)
    called = {}
    real = runtime.bounded_history

    async def inspect(*args, **kwargs):
        called.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(runtime, "bounded_history", inspect)
    assert await runtime.events_for(handle) == [event]
    assert called == {"max_events": 10000, "max_bytes": 8 << 20, "max_pages": 100}
    assert observed["page_size"] == 100 and observed["rpc_timeout"].total_seconds() == 10


async def test_hung_history_observation_is_cancelled_with_finite_budget(monkeypatch):
    import asyncio
    from runner import conformance_generation_runtime as runtime

    closed = []

    async def hanging(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(runtime, "bounded_history", hanging)
    monkeypatch.setattr(runtime, "OBSERVATION_TIMEOUT", 0.01)
    with pytest.raises(TimeoutError):
        await runtime.events_for(NS(id="workflow"))
    assert closed == [True]


@pytest.mark.parametrize("fault", ["uid", "restart", "container", "image"])
def test_worker_receipt_rejects_source_change_during_log_read(monkeypatch, fault):
    from kubernetes import client
    from runner import conformance_generation_runtime as runtime
    from tests.test_conformance_generation import config

    cfg = config()
    image = cfg["worker_image"]
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name="worker", uid="worker-uid"),
        spec=client.V1PodSpec(
            containers=[client.V1Container(name="runner", image=image)],
            service_account_name="session-runner",
        ),
        status=client.V1PodStatus(
            phase="Running",
            container_statuses=[
                client.V1ContainerStatus(
                    name="runner",
                    image=image,
                    image_id="image@sha256:" + "a" * 64,
                    container_id="runtime://old",
                    ready=True,
                    restart_count=0,
                )
            ],
        ),
    )
    core = NS(list_namespaced_pod=lambda *a, **kw: client.V1PodList(items=[pod]))
    ops = runtime.GenerationOps(core, None, NS(config=cfg))
    before = ops.workers()[0]

    def log(*args, **kwargs):
        if fault == "uid":
            pod.metadata.uid = "replacement"
        elif fault == "restart":
            pod.status.container_statuses[0].restart_count = 1
        elif fault == "container":
            pod.status.container_statuses[0].container_id = "runtime://replacement"
        else:
            pod.status.container_statuses[0].image_id = "image@sha256:" + "b" * 64
        return "{}"

    monkeypatch.setattr(runtime, "raw_log", log)
    with pytest.raises(ValueError, match="during log read"):
        ops.receipts(before, "fence_only", cfg["sessions"]["fence_only"])


@pytest.mark.parametrize("fault", ["uid", "restart", "container"])
async def test_traffic_does_not_ingest_logs_from_source_changed_after_read(monkeypatch, fault):
    import copy
    from kubernetes import client
    from runner import conformance_generation_runtime as runtime

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name="aps", uid="aps-uid"),
        spec=client.V1PodSpec(
            containers=[client.V1Container(name="aps", image="pinned")], service_account_name="aps"
        ),
        status=client.V1PodStatus(
            phase="Running",
            container_statuses=[
                client.V1ContainerStatus(
                    name="aps",
                    image="pinned",
                    image_id="image@sha256:" + "a" * 64,
                    container_id="runtime://old",
                    ready=True,
                    restart_count=0,
                )
            ],
        ),
    )
    core = NS(read_namespaced_pod=lambda *a, **kw: copy.deepcopy(pod))
    traffic = runtime.Traffic(core, "session", "g1", "a" * 16)
    source = ("agent-platform", "aps", "aps", "aps")
    traffic.sources = [source]
    traffic.replicas = {"aps": "aps-uid"}
    traffic.source_stamps = {source: runtime.source_stamp(pod)}
    ingested = []
    traffic.ingest_aps = lambda *args: ingested.append(args)

    async def immediate(fn, *args, **kw):
        return fn(*args, **kw)

    monkeypatch.setattr(runtime.asyncio, "to_thread", immediate)

    def log(*args, **kwargs):
        if fault == "uid":
            pod.metadata.uid = "replacement"
        elif fault == "restart":
            pod.status.container_statuses[0].restart_count = 1
        else:
            pod.status.container_statuses[0].container_id = "runtime://replacement"
        return "{}"

    monkeypatch.setattr(runtime, "raw_log", log)
    with pytest.raises(ValueError, match="during read"):
        await traffic.sample()
    assert ingested == []


async def test_case_deadline_cancels_hung_observation_and_still_restores_root_cleanup(monkeypatch):
    import asyncio
    import copy
    from runner import conformance_generation_runtime as runtime
    from tests.test_conformance_generation import config, data

    cfg = config()
    pod = data("blocked_deletion")["initial_pod"]
    worker = {"uid": "worker"}
    state = {"held": False, "closed": False, "reads": 0, "read_cancelled": False}
    signals = []
    cancels = []

    async def immediate(fn, *a, **kw):
        return fn(*a, **kw)

    original_sleep = asyncio.sleep

    async def sleep(_):
        await original_sleep(0)

    monkeypatch.setattr(runtime.asyncio, "to_thread", immediate)
    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)
    monkeypatch.setattr(runtime, "CASE_TIMEOUT", 0.2)
    monkeypatch.setattr(runtime, "CLEANUP_TIMEOUT", 1)

    async def cancel(**kwargs):
        cancels.append(kwargs)

    handle = NS(cancel=cancel)

    async def start(*a, **kw):
        return handle

    def rows(sid):
        if state["closed"]:
            return []
        current = copy.deepcopy(pod)
        current["finalizers"] = [runtime.FINALIZER] if state["held"] else []
        return [current]

    ops = NS(
        core=None,
        pods=lambda sid: [],
        rows=rows,
        receipts=lambda *a: [],
        workers=lambda: [worker],
        absent=lambda name: state["closed"],
    )
    harness = runtime.GenerationHarness(
        NS(start_workflow=start, data_converter=DataConverter.default),
        ops,
        NS(config=cfg, read=lambda: None),
    )

    async def validate():
        return worker

    async def schedule():
        return None

    async def history(*a):
        state["reads"] += 1
        if state["reads"] == 2:
            try:
                await asyncio.Event().wait()
            finally:
                state["read_cancelled"] = True
        return {
            "history": [],
            "terminal": {"kind": "CANCELED"} if state["closed"] else {},
            "progress": {},
        }

    async def gates(*a):
        return [
            {
                "case": "blocked_deletion",
                "run_id": cfg["run_id"],
                "pod_uid": "uid1",
                "worker_uid": "worker",
            }
        ]

    def notify(case, phase, **kwargs):
        signals.append(phase)
        if phase == "ready_delete_for_recovery":
            state["held"] = True
        if phase == "restore_exact_finalizer_now":
            state.update(held=False, closed=True)

    monkeypatch.setattr(harness, "validate", validate)
    monkeypatch.setattr(harness, "schedule", schedule)
    monkeypatch.setattr(harness, "history", history)
    monkeypatch.setattr(harness, "notify", notify)
    monkeypatch.setattr(runtime, "pending_gates", gates)
    report = await harness.case("blocked_deletion")
    assert report["result"] == "fail" and report["evidence"]["error_type"] == "TimeoutError"
    assert state["read_cancelled"] and "restore_exact_finalizer_now" in signals
    assert (
        report["evidence"]["cleanup"]["pods_absent"]
        and report["evidence"]["cleanup"]["session_closed"]
    )
    assert cancels[0]["rpc_timeout"].total_seconds() == 5


@pytest.mark.parametrize(
    "service_account,accepted",
    [
        ("session-runner", True),
        ("session-runner-tenant-test", False),
        ("default", False),
    ],
)
async def test_validate_requires_actual_trusted_worker_service_account(
    monkeypatch, service_account, accepted
):
    from runner import conformance_generation_runtime as runtime
    from tests.test_conformance_generation import config

    cfg = config()
    worker = {
        "ready": True,
        "image": cfg["worker_image"],
        "image_id": "sha256:pinned",
        "service_account": service_account,
    }

    def release(kind, name):
        return {
            "metadata": {"generation": 1},
            "spec": {
                "approval": {"approved": True},
                "agent": "conformance-b"
                if name == cfg["probe_release"]
                else "conformance-checkpoint",
            },
            "status": {
                "ready": True,
                "signatureVerified": True,
                "policyDrift": False,
                "identityMode": "agent",
            },
        }

    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(runtime, "read_io", immediate)
    harness = runtime.GenerationHarness(
        None, NS(object=release, workers=lambda: [worker]), NS(config=cfg, read=lambda: None)
    )
    if accepted:
        assert await harness.validate() == worker
    else:
        with pytest.raises(ValueError, match="isolated trusted worker"):
            await harness.validate()
