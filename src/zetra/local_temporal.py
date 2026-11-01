"""Explicit trusted-host Temporal development; production workers remain separate."""

import asyncio
import ipaddress
import json
import os
import sys
import tempfile
import uuid
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from temporalio import activity
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, is_cancelled_exception
from temporalio.worker import Worker

from ._runtime.activities import Activities
from ._runtime.artifacts import FileStore, Position, Receipt, Tape
from ._runtime.temporal import SessionWorkflow
from ._runtime.workspace import Workspace, validate_snapshot
from .local import LocalModelConfig, _redact
from .manifest import digest, load_agent, source_digest
from .session import SessionHandle, execution_session


class LocalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["local-temporal-v1"] = "local-temporal-v1"
    session: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    message: str
    position: Position
    runtime_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    task_queue: str = Field(min_length=1, max_length=255)
    pause_at: int | None = Field(default=None, ge=0, le=64)

    @model_validator(mode="after")
    def validate_start(self):
        if not 0 < len(self.message.encode()) <= 32000:
            raise ValueError("Temporal input must contain 1..32000 UTF-8 bytes")
        if (
            self.position != Position(workspace=self.position.workspace)
            or not self.position.workspace
        ):
            raise ValueError("local submission requires an empty accepted tape and snapshot")
        return self


def _real_path(path):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("local Temporal paths cannot contain symlinks")
    return path.resolve()


class LocalTemporal:
    """A Linux development runtime bound to source, settings and private local storage.

    The caller owns the Temporal client/server. Keep this state and exact handles
    for recovery, and keep source/config unchanged while executions are retained.
    Agent code runs only in supervised children, with cooperative in-process tools.
    """

    def __init__(self, root, *, state_dir, agent=None, model_config=None, mcp_fixtures=None):
        from .bindings import MCPFixtures

        if sys.platform != "linux":
            raise ValueError("local Temporal currently requires Linux process supervision")
        self.root = _real_path(root)
        self.spec = load_agent(self.root, agent)
        config = LocalModelConfig.model_validate(model_config) if model_config is not None else None
        if config is None and self.spec.model != "fixture":
            raise ValueError("local inference requires explicit model configuration")
        if config is not None and config.timeout > 30:
            raise ValueError("local Temporal model timeout must be at most 30 seconds")
        self.settings = {
            "project_root": str(self.root),
            "source_sha256": source_digest(self.root),
            "agent": self.spec.model_dump(),
            "model_config": config.model_dump() if config else None,
        }
        self.mcp_fixtures = MCPFixtures(mcp_fixtures) if mcp_fixtures is not None else None
        if self.mcp_fixtures is not None:
            self.settings["mcp_fixtures"] = self.mcp_fixtures.config()
        self.identity = digest(self.settings)
        self.state_dir = _real_path(state_dir)
        if (
            self.state_dir == self.root
            or self.state_dir in self.root.parents
            or self.root in self.state_dir.parents
        ):
            raise ValueError("local Temporal state must be outside the project tree")
        marker = self.state_dir / "local-runtime.json"
        if self.state_dir.exists() and any(self.state_dir.iterdir()) and not marker.is_file():
            raise ValueError("local Temporal state must be new or owned by this runtime")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.stat().st_mode & 0o077:
            raise ValueError("local Temporal state directory must be private (mode 0700)")
        expected = {"schema_version": 1, "identity": self.identity, "settings": self.settings}
        if marker.is_symlink():
            raise ValueError("local Temporal metadata cannot be a symlink")
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if json.loads(marker.read_text()) != expected:
                raise ValueError("retained local Temporal state has a different identity") from None
        else:
            with os.fdopen(fd, "w") as stream:
                json.dump(expected, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
        self.objects = _real_path(self.state_dir / "objects")
        self.objects.mkdir(mode=0o700, exist_ok=True)
        self.store = FileStore(self.objects)
        self.tape = Tape(self.store, manifest=self.identity)

    def worker(self, client, *, task_queue, **options):
        router = LocalRouter(self, client, task_queue)
        return Worker(
            client,
            task_queue=task_queue,
            workflows=[SessionWorkflow],
            activities=router.registered(),
            **options,
        )

    async def submit(
        self, client, message, *, task_queue, session=None, workspace=None, pause_at=None
    ):
        if source_digest(self.root) != self.settings["source_sha256"]:
            raise ValueError("source changed since local runtime initialization")
        with tempfile.TemporaryDirectory(prefix="zetra-temporal-input-") as empty:
            initial = _real_path(workspace) if workspace is not None else Path(empty)
            position = self.tape.empty(Workspace(initial, self.store).capture())
        request = LocalRequest(
            session=session or "local-" + uuid.uuid4().hex,
            message=message,
            position=position,
            runtime_id=self.identity,
            task_queue=task_queue,
            pause_at=pause_at,
        )
        native = await client.start_workflow(
            SessionWorkflow.run,
            request.model_dump(),
            id=request.session,
            task_queue=task_queue,
            execution_timeout=timedelta(minutes=30),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        return SessionHandle(
            namespace=client.namespace,
            workflow_id=request.session,
            run_id=native.first_execution_run_id,
            runtime_id=self.identity,
        )

    async def load(self, client, handle):
        handle = SessionHandle.model_validate(handle)
        if handle.namespace != client.namespace or handle.runtime_id != self.identity:
            raise ValueError("local Temporal execution identity mismatch")
        native = client.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
        history = await native.fetch_history()
        if not history.events or not history.events[0].HasField(
            "workflow_execution_started_event_attributes"
        ):
            raise ValueError("original local Workflow history is unavailable")
        attrs = history.events[0].workflow_execution_started_event_attributes
        if (
            attrs.workflow_type.name != "SessionWorkflow"
            or attrs.continued_execution_run_id
            or attrs.original_execution_run_id not in ("", handle.run_id)
            or attrs.first_execution_run_id not in ("", handle.run_id)
            or attrs.attempt > 1
            or attrs.cron_schedule
        ):
            raise ValueError("unsupported local Workflow lineage")
        values = await client.data_converter.decode(attrs.input.payloads, [dict])
        if len(values) != 1:
            raise ValueError("invalid local Workflow input")
        start = LocalRequest.model_validate(values[0])
        if (
            start.session != handle.workflow_id
            or start.runtime_id != self.identity
            or start.task_queue != attrs.task_queue.name
        ):
            raise ValueError("local Workflow start identity mismatch")
        return start, history

    def host(self, handle, start):
        from ._runtime.local_host import HostSession

        session = execution_session(handle)
        return HostSession(
            self.state_dir / "executions" / session,
            self.objects,
            settings=self.settings,
            session=session,
            message=start.message,
        )

    async def result(self, client, handle):
        from temporalio.client import WorkflowFailureError

        handle = SessionHandle.model_validate(handle)
        start, _ = await self.load(client, handle)
        native = client.get_workflow_handle(handle.workflow_id, run_id=handle.run_id)
        try:
            outcome = await native.result(follow_runs=False)
        except WorkflowFailureError as exc:
            outcome = {"status": "CANCELED" if is_cancelled_exception(exc.cause) else "FAILED"}
        _, history = await self.load(client, handle)
        position = start.position
        cleanup = "NOT_CONFIRMED"
        scheduled = {}
        pending = None
        for event in history.events:
            if event.HasField("activity_task_scheduled_event_attributes"):
                attrs = event.activity_task_scheduled_event_attributes
                scheduled[event.event_id] = attrs.activity_type.name
                if attrs.activity_type.name in {"execute_model", "execute_tool"}:
                    command = (await client.data_converter.decode(attrs.input.payloads, [dict]))[0]
                    pending = command["operation"]
            elif event.HasField("activity_task_completed_event_attributes"):
                attrs = event.activity_task_completed_event_attributes
                name = scheduled.get(attrs.scheduled_event_id)
                if name in {"execute_model", "execute_tool", "close_session"}:
                    value = (await client.data_converter.decode(attrs.result.payloads, [dict]))[0]
                    if name == "close_session":
                        cleanup = value.get("cleanup", "NOT_CONFIRMED")
                    else:
                        position = Receipt.model_validate(value).position
                        pending = None
        status = outcome.get("status", "FAILED")
        if status == "SUCCEEDED" and cleanup != "DELETED":
            status = "REMEDIATION_REQUIRED"
        report = self._report(handle, position, outcome.get("pending") or pending, history)
        report.update(status=status, cleanup=cleanup)
        try:
            changed = source_digest(self.root) != self.settings["source_sha256"]
        except (OSError, ValueError):
            changed = True
        if changed:
            report.update(
                error_type="SourceChanged", integrity_errors=["source_changed_or_unreadable"]
            )
            if status not in {"OUTCOME_UNKNOWN", "CANCELED", "REMEDIATION_REQUIRED"}:
                report["status"] = "FAILED"
        return report

    def _report(self, handle, position, pending, history):
        from google.adk.models.llm_response import LlmResponse

        from ._runtime.records import decode_model
        from .bindings import mcp_provenance

        trajectory, inputs, outputs = [], [], []
        text = ""
        for entry in self.tape.records(position):
            op, outcome = entry["operation"], entry["outcome"]
            row = {
                "index": op["index"],
                "kind": op["kind"],
                "executor": op["executor"],
                "status": outcome["status"],
            }
            if op["kind"] == "tool":
                row.update(
                    arguments=_redact(op["request"]["arguments"]), result=_redact(outcome["value"])
                )
            else:
                response = decode_model(LlmResponse, outcome["value"])
                text = (
                    "".join(p.text or "" for p in (response.content.parts or []) if not p.thought)
                    if response.content
                    else ""
                )
                usage = response.usage_metadata
                inputs.append(usage.prompt_token_count if usage else None)
                outputs.append(usage.candidates_token_count if usage else None)
            trajectory.append(row)
        if pending:
            row = {k: pending[k] for k in ("index", "kind", "executor")}
            row["status"] = "outcome_unknown"
            if row["kind"] == "tool":
                row["arguments"] = _redact(pending["request"]["arguments"])
            trajectory.append(row)
        _, files = validate_snapshot(self.store.get(position.workspace))
        contents, total = {}, 0
        for name, data in files:
            if len(data) <= 65536 and total + len(data) <= 262144:
                try:
                    contents[name.as_posix()] = data.decode("utf-8")
                except UnicodeError:
                    continue
                total += len(data)
        inventory = self.state_dir / "executions" / execution_session(handle) / "inventory.json"
        tools = json.loads(inventory.read_text()) if inventory.is_file() else {}
        model = self.settings["model_config"] or {"model": "fixture"}
        elapsed = (
            history.events[-1].event_time.ToNanoseconds()
            - history.events[0].event_time.ToNanoseconds()
        )
        return {
            "schema_version": 1,
            "mode": "temporal-local" if self.settings["model_config"] else "temporal-fixture",
            "agent": self.spec.name,
            "agent_sha256": digest(self.spec.model_dump()),
            "source_sha256": self.settings["source_sha256"],
            "model_config": model,
            "model_config_sha256": digest(model),
            "mcp_binding": mcp_provenance(self.mcp_fixtures),
            "handle": handle.model_dump(),
            "position": position.model_dump(),
            "tools": tools,
            "trajectory": trajectory,
            "result": {"final_text": text},
            "files": [name.as_posix() for name, _ in files],
            "file_contents": contents,
            "metrics": {
                "steps": position.cursor,
                "duration_ms": elapsed / 1_000_000,
                "input_tokens": sum(inputs) if inputs and None not in inputs else None,
                "output_tokens": sum(outputs) if outputs and None not in outputs else None,
            },
            "timing_scope": "Temporal elapsed time including scheduling and pauses",
            "trust": "local-unsigned",
            "scope": "Linux trusted-host cold reconstruction; not sandbox qualification or production authorization",
        }


class LocalRouter:
    def __init__(self, runtime, client, task_queue):
        self.runtime, self.client, self.task_queue = runtime, client, task_queue

    async def invoke(self, name, command):
        info = activity.info()
        handle = SessionHandle(
            namespace=info.workflow_namespace,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            runtime_id=self.runtime.identity,
        )
        try:
            start, _ = await self.runtime.load(self.client, handle)
            if start.task_queue != self.task_queue:
                raise ValueError("local worker task queue mismatch")
            host = self.runtime.host(handle, start)
            activities = Activities(host, host, self.runtime.tape)
            if name in {"provision_session", "create_session", "close_session", "rebind_session"}:
                supplied = command if name == "provision_session" else command["start"]
                if supplied != start.model_dump():
                    raise ValueError("local lifecycle start differs from accepted history")
            return await getattr(activities, name)(command)
        except ValueError as exc:
            raise ApplicationError(type(exc).__name__, non_retryable=True) from None

    @activity.defn
    async def provision_session(self, command: dict) -> dict:
        return await self.invoke("provision_session", command)

    @activity.defn
    async def rebind_session(self, command: dict) -> dict:
        return await self.invoke("rebind_session", command)

    @activity.defn
    async def create_session(self, command: dict) -> dict:
        return await self.invoke("create_session", command)

    @activity.defn
    async def wait_session_ready(self, command: dict) -> dict:
        return await self.invoke("wait_session_ready", command)

    @activity.defn
    async def advance_runner(self, command: dict) -> dict:
        return await self.invoke("advance_runner", command)

    @activity.defn
    async def execute_model(self, command: dict) -> dict:
        return await self.invoke("execute_model", command)

    @activity.defn
    async def execute_tool(self, command: dict) -> dict:
        return await self.invoke("execute_tool", command)

    @activity.defn
    async def close_session(self, command: dict) -> dict:
        return await self.invoke("close_session", command)

    def registered(self):
        return [
            self.provision_session,
            self.rebind_session,
            self.create_session,
            self.wait_session_ready,
            self.advance_runner,
            self.execute_model,
            self.execute_tool,
            self.close_session,
        ]


def loopback_address(value):
    url = urlsplit("//" + value)
    if (
        not url.hostname
        or not url.port
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError("Temporal address must be an explicit loopback host:port")
    try:
        local = ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        local = url.hostname == "localhost"
    if not local:
        raise ValueError("local Temporal requires a loopback server address")
    return value


async def dev_temporal(
    root,
    message,
    *,
    agent=None,
    model_config=None,
    mcp_fixtures=None,
    timeout=60,
    workspace=None,
    output=None,
    address=None,
    binary=None,
    state_dir=None,
):
    """Run with an owned local server or an explicitly supplied loopback server."""
    from temporalio.client import Client
    from temporalio.testing import WorkflowEnvironment

    from ._runtime.local_host import write_json
    from .local import _output_path

    if type(timeout) not in (int, float) or not 0 < timeout <= 600:
        raise ValueError("timeout must be between zero and 600 seconds")
    if address is not None:
        address = loopback_address(address)
        if binary is not None:
            raise ValueError("an external Temporal address cannot use a local server binary")
    root = _real_path(root)
    state_dir = (
        _real_path(state_dir) if state_dir else Path(tempfile.mkdtemp(prefix="zetra-temporal-"))
    )
    destination = _output_path(root, workspace, output)
    _output_path(root, state_dir, output)
    runtime = LocalTemporal(
        root,
        state_dir=state_dir,
        agent=agent,
        model_config=model_config,
        mcp_fixtures=mcp_fixtures,
    )
    queue = "zetra-local-" + uuid.uuid4().hex
    print(f"zetra: local Temporal state: {state_dir}", file=sys.stderr)

    def record(report):
        report["state_dir"] = str(state_dir)
        write_json(state_dir / "last-report.json", report)
        return report

    async with AsyncExitStack() as stack:
        if address is not None:
            client = await Client.connect(address, namespace="default")
        else:
            startup = asyncio.create_task(
                WorkflowEnvironment.start_local(
                    dev_server_existing_path=binary,
                    dev_server_database_filename=str(state_dir / "temporal.sqlite"),
                    dev_server_download_version="v1.9.1",
                    dev_server_log_level="error",
                    dev_server_extra_args=["--output", "none"],
                )
            )
            try:
                environment = await asyncio.shield(startup)
            except asyncio.CancelledError:
                # Resolve startup ownership before propagating cancellation.
                # An abandoned startup future could leave its server running.
                environment = await startup
                await environment.shutdown()
                raise
            stack.push_async_callback(environment.shutdown)
            client = environment.client
        await stack.enter_async_context(runtime.worker(client, task_queue=queue))
        handle = await runtime.submit(client, message, task_queue=queue, workspace=workspace)
        write_json(state_dir / "last-handle.json", handle.model_dump())
        task = asyncio.create_task(runtime.result(client, handle))
        try:
            try:
                report = await asyncio.wait_for(asyncio.shield(task), timeout)
            except TimeoutError:
                await client.get_workflow_handle(handle.workflow_id, run_id=handle.run_id).cancel()
                report = await asyncio.wait_for(asyncio.shield(task), 60)
                report["deadline_exceeded"] = True
            except asyncio.CancelledError:
                await client.get_workflow_handle(handle.workflow_id, run_id=handle.run_id).cancel()
                record(await asyncio.wait_for(asyncio.shield(task), 60))
                raise
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    record(report)
    if destination:
        destination.mkdir(mode=0o700, parents=True)
        workspace_out = destination / "workspace"
        workspace_out.mkdir(mode=0o700)
        Workspace(workspace_out, runtime.store).restore(
            Position.model_validate(report["position"]).workspace
        )
        write_json(destination / "report.json", report)
    return report
