from types import SimpleNamespace

import pytest

from runner.conformance_transport_runtime import transport_gates, valid_workers
from tests.test_transport_control import gate


async def test_gate_decoder_uses_exact_typed_temporal_heartbeat():
    value = {"schema": "t27-gate-v1", "phase": "before_exec", "observed_at": 1793990000.0, **gate()}
    pending = SimpleNamespace(heartbeat_details=SimpleNamespace(payloads=[object()]))

    class Handle:
        async def describe(self):
            return SimpleNamespace(raw_description=SimpleNamespace(pending_activities=[pending]))

    class Converter:
        async def decode(self, _):
            return [value]

    assert await transport_gates(Handle(), Converter(), gate()["session_id"]) == [value]
    value["session_id"] = "other"
    with pytest.raises(ValueError):
        await transport_gates(Handle(), Converter(), gate()["session_id"])


def test_worker_inventory_requires_exact_same_published_digest_and_ready_isolated_worker():
    image = "registry.almckay.io/platform/session-runner:tag@sha256:" + "a" * 64
    rows = [
        {
            "name": "production",
            "uid": "prod",
            "image": image,
            "image_id": image,
            "ready": True,
            "restarts": 2,
            "test_worker": False,
        },
        {
            "name": "transport",
            "uid": "test",
            "image": image,
            "image_id": image,
            "ready": True,
            "restarts": 0,
            "test_worker": True,
        },
    ]
    assert valid_workers(rows, image)
    rows[1]["image_id"] = "different"
    assert not valid_workers(rows, image)


@pytest.mark.parametrize("terminal", ["success", "workflow_error", "cancel", "cleanup_error"])
async def test_session_preserves_real_outcome_and_always_attempts_settled_cleanup(terminal):
    import asyncio

    from temporalio.common import WorkflowIDReusePolicy

    from runner.conformance_transport_runtime import TransportHarness
    from runner.types import SessionResult
    from tests.test_transport_control import config

    submitted, closed = [], []
    result = SessionResult(
        "COMPLETED", {"stop": "OK", "delete": "DELETED", "grant": "OK"}, 1, 1, "a" * 64
    )

    class Handle:
        async def result(self):
            if terminal == "workflow_error":
                raise RuntimeError("no fabricated outcome")
            if terminal == "cancel":
                raise asyncio.CancelledError
            return result

        async def fetch_history_events(self):
            for item in []:
                yield item

    handle = Handle()

    class Client:
        data_converter = None

        async def start_workflow(self, fn, value, **kwargs):
            submitted.append((value, kwargs))
            return handle

    class Cleanup:
        async def _close(self, inp, got_handle, task):
            await asyncio.sleep(0)
            closed.append((inp, got_handle, task.done()))
            if terminal == "cleanup_error":
                raise TimeoutError("Pod deletion not confirmed")

    harness = TransportHarness.__new__(TransportHarness)
    harness.cfg, harness.client = config(), Client()
    harness.base = SimpleNamespace(session_timeout=2)
    harness.cleanup, harness.data = Cleanup(), {"sessions": []}
    if terminal == "success":
        got = await harness.session("transport", {"transport_case": "tls"})
        assert got.outcome == "COMPLETED" and got.snapshot == "a" * 64
    else:
        exc = {
            "workflow_error": RuntimeError,
            "cancel": asyncio.CancelledError,
            "cleanup_error": TimeoutError,
        }[terminal]
        with pytest.raises(exc):
            await harness.session("transport", {"transport_case": "tls"})
    inp, kwargs = submitted[0]
    assert inp.session_id == config()["sessions"]["transport"]
    assert inp.agent == "conformance-transport" and inp.task == {"transport_case": "tls"}
    assert kwargs["task_queue"] == config()["queue"]
    assert kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert closed == [(inp, handle, True)]
    assert harness.data["sessions"][0]["cleanup_confirmed"] == (terminal != "cleanup_error")
    if terminal in {"workflow_error", "cancel"}:
        assert "result" not in harness.data["sessions"][0]


async def test_gate_observer_rejects_changed_phase_time_and_preserves_previous_receipts(
    monkeypatch,
):
    from runner import conformance_transport_runtime as mod
    from runner.activities import pod_name
    from tests.test_transport_control import config, receipt

    cfg = config()
    exact_gate = {**gate(), "pod": pod_name(cfg["sessions"]["t4"], 1)}
    row = {**exact_gate, "schema": "t27-gate-v1", "phase": "before_exec", "observed_at": 1.0}
    worker = {
        "uid": exact_gate["worker_uid"],
        "test_worker": True,
        "restarts": 0,
        "ready": True,
        "image": cfg["worker_image"],
        "image_id": cfg["worker_image"],
    }
    workers = [worker, {**worker, "uid": "production", "test_worker": False}]
    pod = {
        "metadata": {"uid": exact_gate["pod_uid"]},
        "status": {"podIP": exact_gate["pod_ip"]},
        "spec": {"nodeName": exact_gate["node"]},
    }
    start = {**receipt(), **exact_gate}

    async def decoded(*_):
        return [row]

    monkeypatch.setattr(mod, "transport_gates", decoded)
    harness = mod.TransportHarness.__new__(mod.TransportHarness)
    harness.cfg = cfg
    harness.client = SimpleNamespace(data_converter=None)
    harness.ops = SimpleNamespace(workers=lambda _: workers, pods=lambda _: [pod])
    harness.control = SimpleNamespace(get=lambda key: start if key == "capture-start" else None)
    harness.data, harness.notified = {"workers_before": workers}, set()
    await harness.observe_gate(None, exact_gate["session_id"])
    assert harness.data["capture_start"] == start
    row["observed_at"] = 2.0
    with pytest.raises(ValueError, match="timestamp changed"):
        await harness.observe_gate(None, exact_gate["session_id"])
    assert harness.data["gate_times"] == {"before_exec": 1.0}
    assert harness.data["capture_start"] == start
