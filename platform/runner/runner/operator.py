"""Operator-only exact execution inspection and bounded report export."""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import tarfile
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta

from runner.types import NAME_RE, TENANT_RE, SessionInput
from runner.workflow import SessionWorkflow

CLEANUP = {"stop": "OK", "delete": "DELETED", "grant": "OK"}


@dataclass(frozen=True)
class ExecutionReceipt:
    namespace: str
    tenant: str
    agent: str
    release: str
    session_id: str
    worker_pool: str
    workflow_id: str
    run_id: str
    version: int = 1

    @classmethod
    def from_input(cls, namespace, inp, run_id):
        return cls.parse(
            dict(
                namespace=namespace,
                tenant=inp.tenant,
                agent=inp.agent,
                release=inp.release,
                session_id=inp.session_id,
                worker_pool=inp.worker_pool,
                workflow_id=inp.workflow_id,
                run_id=run_id,
                version=1,
            )
        )

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("invalid receipt fields")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("invalid receipt version")
        if any(not isinstance(v, str) for k, v in value.items() if k != "version"):
            raise ValueError("invalid receipt values")
        obj = cls(**value)
        request = obj.input_identity()
        request.validate()
        if (
            not TENANT_RE.fullmatch(obj.tenant)
            or not NAME_RE.fullmatch(obj.agent)
            or not NAME_RE.fullmatch(obj.release)
        ):
            raise ValueError("noncanonical receipt identity")
        try:
            if str(uuid.UUID(obj.run_id)) != obj.run_id:
                raise ValueError("invalid run ID")
        except (ValueError, AttributeError):
            raise ValueError("invalid run ID") from None
        if obj.namespace != f"agents-{obj.tenant}" or obj.workflow_id != request.workflow_id:
            raise ValueError("receipt identity mismatch")
        return obj

    def input_identity(self):
        return SessionInput(
            tenant=self.tenant,
            agent=self.agent,
            release=self.release,
            session_id=self.session_id,
            worker_pool=self.worker_pool,
            grant_handle=None,
            task=None,
        )


async def bind_execution(client, receipt, *, expected_input=None, rpc_timeout):
    receipt = ExecutionReceipt.parse(asdict(receipt))
    if client.namespace != receipt.namespace:
        raise ValueError("namespace mismatch")
    handle = client.get_workflow_handle_for(
        SessionWorkflow.run, receipt.workflow_id, run_id=receipt.run_id
    )
    desc = await handle.describe(rpc_timeout=rpc_timeout)
    if (
        desc.id != receipt.workflow_id
        or desc.run_id != receipt.run_id
        or desc.workflow_type != "SessionWorkflow"
        or desc.task_queue != receipt.input_identity().task_queue
    ):
        raise ValueError("execution identity mismatch")
    history = await handle.fetch_history(rpc_timeout=rpc_timeout)
    if not history.events or not history.events[0].HasField(
        "workflow_execution_started_event_attributes"
    ):
        raise ValueError("start history unavailable")
    attrs = history.events[0].workflow_execution_started_event_attributes
    if (
        attrs.workflow_type.name != "SessionWorkflow"
        or attrs.task_queue.name != desc.task_queue
        or attrs.continued_execution_run_id
        or attrs.original_execution_run_id not in ("", receipt.run_id)
        or attrs.first_execution_run_id not in ("", receipt.run_id)
        or attrs.attempt > 1
        or attrs.cron_schedule
        or attrs.HasField("retry_policy")
    ):
        raise ValueError("unsupported execution lineage")
    values = await client.data_converter.decode(attrs.input.payloads, [SessionInput])
    if len(values) != 1:
        raise ValueError("invalid execution input")
    original = values[0]
    original.validate()
    if ExecutionReceipt.from_input(client.namespace, original, receipt.run_id) != receipt:
        raise ValueError("original input identity mismatch")
    if expected_input is not None and original != expected_input:
        raise ValueError("duplicate belongs to another request")
    return handle


@dataclass
class WaitResult:
    receipt: ExecutionReceipt
    workflow_status: str = "UNKNOWN"
    outcome: str | None = None
    cleanup: str = "NOT_CONFIRMED"
    exit_code: int = 1
    snapshot: str | None = None

    def envelope(self):
        return dict(
            receipt=asdict(self.receipt),
            workflow_status=self.workflow_status,
            outcome=self.outcome,
            cleanup=self.cleanup,
        )


def positive_timeout(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be positive and finite")
    return value


async def wait_execution(
    client, receipt, *, timeout, rpc_timeout, cleanup_timeout, cancel_on_timeout
):
    timeout, rpc_timeout, cleanup_timeout = map(
        positive_timeout, (timeout, rpc_timeout, cleanup_timeout)
    )
    result = WaitResult(receipt)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    handle = None

    async def inspect(until):
        nonlocal handle
        while True:
            remaining = until - loop.time()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                rpc = timedelta(seconds=min(rpc_timeout, remaining))
                if handle is None:
                    handle = await bind_execution(client, receipt, rpc_timeout=rpc)
                desc = await handle.describe(rpc_timeout=rpc)
                result.workflow_status = desc.status.name
                if result.workflow_status != "RUNNING":
                    if result.workflow_status not in ("COMPLETED", "CANCELED"):
                        return
                    query = await handle.query(SessionWorkflow.status, rpc_timeout=rpc)
                    query_clean = (
                        query.get("state") == "CLEANED" and query.get("cleanup") == CLEANUP
                    )
                    if result.workflow_status == "CANCELED" and query_clean:
                        result.cleanup = "DELETED"
                    if result.workflow_status == "COMPLETED":
                        value = await handle.result(follow_runs=False, rpc_timeout=rpc)
                        result.outcome = value.outcome
                        if (
                            value.outcome == "COMPLETED"
                            and value.cleanup == CLEANUP
                            and query_clean
                        ):
                            result.cleanup = "DELETED"
                            result.exit_code, result.snapshot = 0, value.snapshot
                        else:
                            result.cleanup = "NOT_CONFIRMED"
                    return
            await asyncio.sleep(min(0.25, max(0, until - loop.time())))

    cancel = False
    try:
        await inspect(deadline)
    except TimeoutError:
        result.exit_code, cancel = 124, cancel_on_timeout
    except asyncio.CancelledError:
        result.exit_code, cancel = 130, True
    except Exception:  # inspection failures never establish cleanup or disclose payloads
        result.cleanup = "NOT_CONFIRMED"
    if cancel:
        code = result.exit_code
        try:
            async with asyncio.timeout(cleanup_timeout):
                if handle is None:
                    handle = await bind_execution(
                        client, receipt, rpc_timeout=timedelta(seconds=rpc_timeout)
                    )
                await handle.cancel(
                    rpc_timeout=timedelta(seconds=min(rpc_timeout, cleanup_timeout))
                )
                await inspect(loop.time() + cleanup_timeout)
        except (Exception, asyncio.CancelledError):
            result.cleanup = "NOT_CONFIRMED"
        result.exit_code, result.snapshot = code, None
    return result


def read_report(store, snapshot):
    if (
        not isinstance(snapshot, str)
        or len(snapshot) != 64
        or any(c not in "0123456789abcdef" for c in snapshot)
    ):
        raise ValueError("invalid snapshot key")
    with store.open(snapshot) as source:
        raw = source.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != snapshot:
        raise ValueError("snapshot bounds or hash mismatch")
    report = None
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            end_of_members = 0
            for index, member in enumerate(archive):
                # tarfile.offset is the next header boundary, including extended
                # headers and padded data; never infer terminators from file bytes.
                end_of_members = archive.offset
                if index >= 256:
                    raise ValueError("archive member limit")
                if member.name != "workspace/report.md":
                    continue
                if report is not None or not member.isfile() or member.size > 16000:
                    raise ValueError("invalid report member")
                with archive.extractfile(member) as source:
                    content = source.read(16001)
                if len(content) != member.size:
                    raise ValueError("short report read")
                report = content.decode("utf-8")
            trailer = raw[end_of_members:]
            if len(trailer) < 1024 or len(trailer) % 512 or any(trailer):
                raise ValueError("missing or invalid archive termination")
    except (tarfile.TarError, UnicodeError, EOFError) as exc:
        raise ValueError("invalid report archive") from exc
    if report is None:
        raise ValueError("report missing")
    return report


async def read_report_bounded(store, snapshot, *, timeout):
    """A stalled operator store must not hold CLI shutdown or publish late output."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def read():
        try:
            value, error = read_report(store, snapshot), None
        except Exception as exc:
            value, error = None, exc

        def deliver():
            if not future.done():
                if error is None:
                    future.set_result(value)
                else:
                    future.set_exception(error)

        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            pass  # caller timed out/interrupted and closed its loop; discard bytes

    threading.Thread(target=read, name="operator-report-read", daemon=True).start()
    return await asyncio.wait_for(future, timeout=positive_timeout(timeout))
