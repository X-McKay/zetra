"""Data-only execution identity shared by clients and replaceable workers."""

import hashlib

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._runtime.artifacts import Position
from ._runtime.records import canonical_bytes
from .manifest import Release


class SessionHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    namespace: str = Field(min_length=1, max_length=255)
    workflow_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    run_id: str = Field(min_length=1, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=128)


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    message: str
    position: Position
    release: Release
    runtime_id: str = Field(min_length=1, max_length=128)
    task_queue: str = Field(min_length=1, max_length=255)

    @field_validator("message")
    @classmethod
    def bounded_message(cls, value):
        if not 0 < len(value.encode()) <= 32_000:
            raise ValueError("message must contain 1..32000 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def initial_position(self):
        self.release.agent.validate_policy()
        if (
            self.position != Position(workspace=self.position.workspace)
            or self.position.workspace is None
        ):
            raise ValueError("submission requires an empty accepted tape and workspace checkpoint")
        return self

    def start(self):
        return {**self.model_dump(), "release_id": self.release.identity}


def execution_session(handle: SessionHandle):
    """No name/workspace reuse across namespaces, runs or configured runtimes."""
    return "s-" + hashlib.sha256(canonical_bytes(handle.model_dump())).hexdigest()[:40]


async def load_execution(client, handle: SessionHandle):
    """Resolve only the original execution; never follow a reset or continued run."""
    handle = SessionHandle.model_validate(handle)
    if client.namespace != handle.namespace:
        raise ValueError("Temporal namespace mismatch")
    history = await client.get_workflow_handle(
        handle.workflow_id, run_id=handle.run_id
    ).fetch_history()
    if not history.events or not history.events[0].HasField(
        "workflow_execution_started_event_attributes"
    ):
        raise ValueError("original Workflow history is unavailable")
    attrs = history.events[0].workflow_execution_started_event_attributes
    if attrs.workflow_type.name != "SessionWorkflow":
        raise ValueError("unexpected workflow type")
    if (
        attrs.continued_execution_run_id
        or attrs.original_execution_run_id not in ("", handle.run_id)
        or attrs.first_execution_run_id not in ("", handle.run_id)
        or attrs.attempt > 1
        or attrs.cron_schedule
    ):
        raise ValueError("unsupported Workflow lineage: reset, retry or continuation")
    values = await client.data_converter.decode(attrs.input.payloads, [dict])
    if len(values) != 1:
        raise ValueError("invalid Workflow start")
    raw = dict(values[0])
    release_id = raw.pop("release_id", None)
    request = SessionRequest.model_validate(raw)
    if (
        request.session != handle.workflow_id
        or request.runtime_id != handle.runtime_id
        or request.task_queue != attrs.task_queue.name
        or release_id != request.release.identity
    ):
        raise ValueError("Workflow start identity mismatch")
    return request.start()
