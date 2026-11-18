"""The real seams. Thin on purpose: each call fetches and returns, and every failure other than
a plain "no such thing" becomes `Unavailable` with fixed text, so nothing of the error (an
address, a path, a token) goes anywhere. Not run by the tests; the decisions are in `service.py`.
"""

from __future__ import annotations

import asyncio
import functools
from datetime import timedelta
from typing import Any

from runner.activities import FilesystemStore
from runner.operator import read_report
from runner.submit import submit
from runner.types import SessionInput
from runner.worker import connect
from runner.workflow import SessionWorkflow
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from session_api.ports import Execution, SessionConflict, Unavailable

RPC_TIMEOUT = timedelta(seconds=10)
# What `runner.operator.bind_execution` raises for an id that exists with other input.
OTHER_INPUT = "duplicate belongs to another request"
LIST_QUERY = 'WorkflowType = "SessionWorkflow" AND ExecutionStatus = "Running"'
GROUP, VERSION = "agentplatform.io", "v1alpha1"


def _guarded(call):
    @functools.wraps(call)
    async def wrapper(*args, **kwargs):
        try:
            return await call(*args, **kwargs)
        except (Unavailable, SessionConflict):
            raise
        except Exception:  # noqa: BLE001
            raise Unavailable("Temporal could not be reached") from None

    return wrapper


def _gone(error: RPCError) -> bool:
    return error.status in (RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION)


class TemporalSessions:
    """`Sessions` over the runner's own client: its mTLS, namespace and payload codec."""

    def __init__(self, tenant: str) -> None:
        self._tenant = tenant
        self._client: Client | None = None
        self._lock = asyncio.Lock()

    async def _connected(self) -> Client:
        async with self._lock:
            if self._client is None:
                self._client = await connect(self._tenant)
            return self._client

    async def _handle(self, workflow_id: str):
        client = await self._connected()
        return client.get_workflow_handle_for(SessionWorkflow.run, workflow_id)

    @_guarded
    async def start(self, inp: SessionInput, memo: dict[str, Any]) -> bool:
        client = await self._connected()
        try:
            await client.get_workflow_handle(inp.workflow_id).describe(rpc_timeout=RPC_TIMEOUT)
            existed = True
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise
            existed = False
        try:
            await submit(client, inp, rpc_timeout=RPC_TIMEOUT, memo=memo)
        except ValueError as error:
            if str(error) == OTHER_INPUT:
                raise SessionConflict from None
            raise
        return not existed

    @_guarded
    async def describe(self, workflow_id: str) -> Execution | None:
        handle = await self._handle(workflow_id)
        try:
            desc = await handle.describe(rpc_timeout=RPC_TIMEOUT)
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                return None
            raise
        status = desc.status.name if desc.status is not None else "UNKNOWN"
        result = None
        if status == "COMPLETED":
            try:
                result = await handle.result(rpc_timeout=RPC_TIMEOUT)
            except Exception:  # noqa: BLE001 - the state is known without it
                result = None
        return Execution(
            workflow_id, status, desc.start_time, desc.close_time, await _memo(desc), result
        )

    @_guarded
    async def query(self, workflow_id: str) -> Any:
        handle = await self._handle(workflow_id)
        return await handle.query(SessionWorkflow.status, rpc_timeout=RPC_TIMEOUT)

    @_guarded
    async def input(self, workflow_id: str) -> SessionInput | None:
        client = await self._connected()
        handle = client.get_workflow_handle(workflow_id)
        first = None
        async for event in handle.fetch_history_events(page_size=1, rpc_timeout=RPC_TIMEOUT):
            first = event
            break
        if first is None or not first.HasField("workflow_execution_started_event_attributes"):
            return None
        payloads = first.workflow_execution_started_event_attributes.input.payloads
        try:
            values = await client.data_converter.decode(payloads, [SessionInput])
        except Exception:  # noqa: BLE001
            return None
        return values[0] if len(values) == 1 else None

    @_guarded
    async def cancel(self, workflow_id: str) -> bool:
        handle = await self._handle(workflow_id)
        try:
            await handle.cancel(rpc_timeout=RPC_TIMEOUT)
        except RPCError as error:
            if _gone(error):
                return False
            raise
        return True

    @_guarded
    async def list_running(self) -> list[Execution]:
        client = await self._connected()
        found = []
        async for ex in client.list_workflows(LIST_QUERY, rpc_timeout=RPC_TIMEOUT):
            found.append(Execution(ex.id, "RUNNING", ex.start_time, None, await _memo(ex)))
        return found


async def _memo(described) -> dict[str, Any]:
    """The memo, or an empty one when it cannot be decoded: the service then reads the input."""
    try:
        return dict(await described.memo())
    except Exception:  # noqa: BLE001
        return {}


class KubeObjects:
    """`Objects` over the Kubernetes API, read only."""

    def __init__(self, api=None) -> None:
        if api is None:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            api = client.CustomObjectsApi()
        self._api = api

    def _get(self, plural: str, namespace: str, name: str) -> dict | None:
        from kubernetes.client.exceptions import ApiException

        try:
            return self._api.get_namespaced_custom_object(GROUP, VERSION, namespace, plural, name)
        except ApiException as error:
            if error.status == 404:
                return None
            raise Unavailable("the Kubernetes API could not be reached") from None
        except Exception:  # noqa: BLE001
            raise Unavailable("the Kubernetes API could not be reached") from None

    def agent(self, namespace: str, name: str) -> dict | None:
        return self._get("agents", namespace, name)

    def release(self, namespace: str, name: str) -> dict | None:
        return self._get("agentreleases", namespace, name)


class StoreReports:
    """`Reports` over the runner's artifact store, read only."""

    def __init__(self, root: str) -> None:
        self._store = FilesystemStore(root)

    def read(self, snapshot: str) -> str:
        return read_report(self._store, snapshot)
