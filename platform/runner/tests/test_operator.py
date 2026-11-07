"""Operator boundaries use real Temporal history protobufs and converters."""

import asyncio
import hashlib
import io
import tarfile
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import pytest
from temporalio.api.history.v1 import HistoryEvent, WorkflowExecutionStartedEventAttributes
from temporalio.converter import DataConverter

from runner.types import SessionInput, SessionResult

RUN = "7667ff33-5ee8-40e8-a8e1-7cdd086ed798"
CLEAN = {"stop": "OK", "delete": "DELETED", "grant": "OK"}


def inp(**over):
    values = dict(
        tenant="tenant-a",
        agent="reader",
        release="reader-r1",
        session_id="6f1d2c3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f",
        grant_handle=None,
        task={"request": "read"},
    )
    return SessionInput(**(values | over))


class Client:
    namespace = "agents-tenant-a"
    data_converter = DataConverter.default

    def __init__(self, request=None, state="COMPLETED"):
        self.request = request or inp()
        self.state = state
        self.calls = []
        self.cancelled = 0
        self.cleanup = CLEAN.copy()
        self.query_error = False

    def get_workflow_handle_for(self, fn, workflow_id, *, run_id):
        assert run_id == RUN and workflow_id == self.request.workflow_id
        self.calls.append(run_id)
        return self

    async def fetch_history(self, **kw):
        payloads = await self.data_converter.encode([self.request])
        attrs = WorkflowExecutionStartedEventAttributes(
            attempt=1, first_execution_run_id=RUN, original_execution_run_id=RUN
        )
        attrs.workflow_type.name = "SessionWorkflow"
        attrs.task_queue.name = self.request.task_queue
        attrs.input.payloads.extend(payloads)
        if hasattr(self, "alter"):
            self.alter(attrs)
        return SimpleNamespace(
            events=[HistoryEvent(workflow_execution_started_event_attributes=attrs)]
        )

    async def describe(self, **kw):
        return SimpleNamespace(
            id=self.request.workflow_id,
            run_id=RUN,
            workflow_type="SessionWorkflow",
            task_queue=self.request.task_queue,
            status=SimpleNamespace(name=self.state),
        )

    async def query(self, *args, **kw):
        if self.query_error:
            raise RuntimeError("private task must never escape")
        return {"state": "CLEANED", "cleanup": self.cleanup}

    async def result(self, **kw):
        assert kw["follow_runs"] is False
        return SessionResult("COMPLETED", CLEAN.copy(), snapshot="a" * 64)

    async def cancel(self, **kw):
        self.cancelled += 1
        self.state = "CANCELED"


def receipt(request=None):
    from runner.operator import ExecutionReceipt

    return ExecutionReceipt.from_input("agents-tenant-a", request or inp(), RUN)


async def test_bind_real_history_and_duplicate_full_input():
    from runner.operator import bind_execution

    client = Client()
    assert (
        await bind_execution(
            client, receipt(), expected_input=inp(), rpc_timeout=timedelta(seconds=1)
        )
        is client
    )
    with pytest.raises(ValueError):
        await bind_execution(
            client,
            receipt(),
            expected_input=inp(task={"changed": True}),
            rpc_timeout=timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt", 2),
        ("continued_execution_run_id", RUN),
        ("original_execution_run_id", "other"),
        ("first_execution_run_id", "other"),
        ("cron_schedule", "* * * * *"),
    ],
)
async def test_reject_lineage(field, value):
    from runner.operator import bind_execution

    client = Client()
    client.alter = lambda attrs: setattr(attrs, field, value)
    with pytest.raises(ValueError):
        await bind_execution(
            client, receipt(), expected_input=None, rpc_timeout=timedelta(seconds=1)
        )


async def test_governed_queue_and_receipt_validation():
    from runner.operator import ExecutionReceipt, bind_execution

    request = inp(worker_pool="governed-v1")
    client = Client(request)
    await bind_execution(
        client, receipt(request), expected_input=None, rpc_timeout=timedelta(seconds=1)
    )
    for patch in (
        {"unknown": 1},
        {"namespace": "agents-tenant-b"},
        {"run_id": "bad"},
        {"version": True},
    ):
        with pytest.raises(ValueError):
            ExecutionReceipt.parse(asdict(receipt()) | patch)


async def test_wait_success_keeps_snapshot_out_of_envelope():
    from runner.operator import wait_execution

    result = await wait_execution(
        Client(),
        receipt(),
        timeout=1,
        rpc_timeout=0.1,
        cleanup_timeout=0.1,
        cancel_on_timeout=False,
    )
    assert result.exit_code == 0 and result.snapshot == "a" * 64
    assert "snapshot" not in str(result.envelope())


@pytest.mark.parametrize(
    "cleanup", [{}, CLEAN | {"delete": "NOT_CONFIRMED"}, CLEAN | {"extra": "OK"}]
)
async def test_cleanup_mismatch_is_not_success(cleanup):
    from runner.operator import wait_execution

    client = Client()
    client.cleanup = cleanup
    result = await wait_execution(
        client, receipt(), timeout=1, rpc_timeout=0.1, cleanup_timeout=0.1, cancel_on_timeout=False
    )
    assert result.exit_code == 1 and result.snapshot is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_does_not_cancel_without_policy(cancel):
    from runner.operator import wait_execution

    client = Client(state="RUNNING")
    result = await wait_execution(
        client,
        receipt(),
        timeout=0.01,
        rpc_timeout=0.01,
        cleanup_timeout=0.05,
        cancel_on_timeout=cancel,
    )
    assert result.exit_code == 124 and client.cancelled == int(cancel)
    assert result.envelope()["receipt"]["run_id"] == RUN


async def test_cancel_query_failure_is_uncertain():
    from runner.operator import wait_execution

    client = Client(state="RUNNING")
    client.query_error = True
    result = await wait_execution(
        client,
        receipt(),
        timeout=0.01,
        rpc_timeout=0.01,
        cleanup_timeout=0.05,
        cancel_on_timeout=True,
    )
    assert result.exit_code == 124 and result.cleanup == "NOT_CONFIRMED"
    assert "private" not in str(result.envelope())


def archive(entries):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:") as tar:
        for name, data, kind in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
    return out.getvalue()


class Store:
    def __init__(self, raw):
        self.raw = raw

    def open(self, key):
        return io.BytesIO(self.raw)


def test_report_only_validated_bytes():
    from runner.operator import read_report

    raw = archive(
        [
            ("state/private", b"secret", tarfile.REGTYPE),
            ("workspace/report.md", b"# Report", tarfile.REGTYPE),
        ]
    )
    assert read_report(Store(raw), hashlib.sha256(raw).hexdigest()) == "# Report"
    with pytest.raises(ValueError):
        read_report(Store(raw), "a" * 64)


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [("workspace/report.md", b"x", tarfile.SYMTYPE)],
        [("workspace/report.md", b"x", tarfile.REGTYPE)] * 2,
        [("workspace/report.md", b"x" * 16001, tarfile.REGTYPE)],
        [("workspace/report.md", b"\xff", tarfile.REGTYPE)],
        [("x", b"", tarfile.REGTYPE)] * 257,
    ],
)
def test_reject_invalid_report(entries):
    from runner.operator import read_report

    raw = archive(entries)
    with pytest.raises(ValueError):
        read_report(Store(raw), hashlib.sha256(raw).hexdigest())


async def test_interrupt_requests_cancel_and_keeps_interrupt_status():
    from runner.operator import wait_execution

    client = Client(state="RUNNING")
    task = asyncio.create_task(
        wait_execution(
            client,
            receipt(),
            timeout=10,
            rpc_timeout=0.1,
            cleanup_timeout=0.1,
            cancel_on_timeout=False,
        )
    )
    await asyncio.sleep(0.001)
    task.cancel()
    result = await task
    assert result.exit_code == 130 and result.cleanup == "DELETED" and client.cancelled == 1


async def test_second_interrupt_exits_without_claiming_cleanup():
    from runner.operator import wait_execution

    client = Client(state="RUNNING")

    async def hanging_cancel(**kw):
        await asyncio.sleep(10)

    client.cancel = hanging_cancel
    task = asyncio.create_task(
        wait_execution(
            client,
            receipt(),
            timeout=10,
            rpc_timeout=0.1,
            cleanup_timeout=10,
            cancel_on_timeout=False,
        )
    )
    await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.001)
    task.cancel()
    result = await task
    assert result.exit_code == 130 and result.cleanup == "NOT_CONFIRMED"


def test_malformed_and_compressed_archive_rejected():
    from runner.operator import read_report
    import gzip

    for raw in (
        b"invalid tar",
        gzip.compress(archive([("workspace/report.md", b"x", tarfile.REGTYPE)])),
    ):
        with pytest.raises(ValueError):
            read_report(Store(raw), hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace", "agents-tenant-b"),
        ("workflow_type", "Other"),
        ("task_queue", "other"),
        ("run_id", "other"),
    ],
)
async def test_reject_mismatched_description(field, value):
    from runner.operator import bind_execution

    client = Client()
    original = client.describe
    if field == "namespace":
        client.namespace = value
    else:

        async def altered(**kw):
            result = await original(**kw)
            setattr(result, field, value)
            return result

        client.describe = altered
    with pytest.raises(ValueError):
        await bind_execution(client, receipt(), rpc_timeout=timedelta(seconds=1))


def test_snapshot_byte_bound():
    from runner.operator import read_report

    raw = b"x" * (64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        read_report(Store(raw), hashlib.sha256(raw).hexdigest())


def test_truncated_member_is_not_report():
    from runner.operator import read_report

    raw = archive([("workspace/report.md", b"x" * 16000, tarfile.REGTYPE)])[:1000]
    with pytest.raises(ValueError):
        read_report(Store(raw), hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("state", ["TERMINATED", "TIMED_OUT", "FAILED"])
async def test_hard_terminal_is_not_cleanup_proof(state):
    from runner.operator import wait_execution

    result = await wait_execution(
        Client(state=state),
        receipt(),
        timeout=1,
        rpc_timeout=0.1,
        cleanup_timeout=0.1,
        cancel_on_timeout=False,
    )
    assert result.exit_code == 1 and result.cleanup == "NOT_CONFIRMED"


def test_receipt_rejects_noncanonical_names_before_connection():
    from runner.operator import ExecutionReceipt

    for field in ("tenant", "agent", "release"):
        value = asdict(receipt())
        value[field] += "\n"
        if field == "tenant":
            value["namespace"] += "\n"
            value["workflow_id"] = value["workflow_id"].replace("tenant-a:", "tenant-a\n:")
        if field == "agent":
            value["workflow_id"] = value["workflow_id"].replace(":reader:", ":reader\n:")
        with pytest.raises(ValueError):
            ExecutionReceipt.parse(value)


async def test_blocked_report_interrupt_discards_late_completion(monkeypatch):
    import time
    import runner.operator as operator

    def blocked(*args):
        time.sleep(0.05)
        return "late report"

    monkeypatch.setattr(operator, "read_report", blocked)
    task = asyncio.create_task(operator.read_report_bounded(None, None, timeout=10))
    await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)  # late completion must not set a cancelled Future
