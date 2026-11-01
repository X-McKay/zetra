"""Data-only, bounded protocol for the opt-in shared-core execution profile."""

import hashlib
import io
import json
import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from zetra._runtime.artifacts import Position
from zetra._runtime.records import canonical_bytes, digest
from zetra.manifest import resource_parts

PROFILE = "zetra-core-v1"
MAX_CONTROL = 1024 * 1024
NAME = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
SHA = r"^[a-f0-9]{64}$"
UUID = r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$"


def strict_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("non-finite JSON value")

    if len(data) > MAX_CONTROL:
        raise ValueError("control payload too large")
    try:
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
        if not isinstance(result, dict):
            raise ValueError("control payload must be an object")  # noqa: TRY004 -- invalid wire data
        canonical_bytes(result)
        return result
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("invalid control JSON") from exc


def encode_frame(value):
    body = canonical_bytes(value)
    strict_json(body)
    return str(len(body)).encode() + b"\n" + body


def read_frame(stream):
    header = stream.readline(16)
    if not re.fullmatch(rb"[1-9][0-9]{0,6}\n", header):
        raise ValueError("invalid frame header")
    size = int(header)
    if size > MAX_CONTROL:
        raise ValueError("control payload too large")
    body = stream.read(size)
    if len(body) != size:
        raise ValueError("short frame")
    return strict_json(body)


def decode_frame(data):
    stream = io.BytesIO(data)
    result = read_frame(stream)
    if stream.read(1):
        raise ValueError("trailing frame bytes")
    return result


def yaml_mapping(data):
    if len(data) > 128000:
        raise ValueError("manifest too large")

    class Unique(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            if key in result:
                raise ValueError("duplicate manifest key")
            result[key] = loader.construct_object(value_node)
        return result

    Unique.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    value = yaml.load(data, Loader=Unique)
    if not isinstance(value, dict):
        raise ValueError("manifest must be an object")  # noqa: TRY004 -- invalid manifest data
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema version must be an integer")
        return value


class ModelBinding(Strict):
    backend: str = Field(pattern=NAME)
    name: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    temperature: float = Field(default=0, ge=0, le=2)


class ToolDeclaration(Strict):
    name: str = Field(pattern=r"^[A-Za-z_]\w{0,127}$")
    effect: Literal["workspace_read", "workspace_write", "remote_read"]


class CoreManifest(Strict):
    schema_version: Literal[2]
    name: str = Field(pattern=NAME)
    runtime_profile: Literal["zetra-core-v1"]
    definition: str
    model: ModelBinding
    tools: list[ToolDeclaration] = Field(max_length=64)

    @field_validator("definition")
    @classmethod
    def resource(cls, value):
        resource_parts(value)
        return value

    @model_validator(mode="after")
    def unique_tools(self):
        if len(self.inventory) != len(self.tools):
            raise ValueError("duplicate declared tool")
        return self

    @property
    def inventory(self):
        return {tool.name: tool.effect for tool in self.tools}


class CoreBinding(Strict):
    profile: Literal["zetra-core-v1"]
    tenant: str = Field(pattern=NAME)
    agent: str = Field(pattern=NAME)
    release: str = Field(pattern=NAME)
    image: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=SHA)
    policy_sha256: str = Field(pattern=SHA)
    namespace: str = Field(min_length=1, max_length=255)
    workflow_id: str = Field(min_length=1, max_length=255)
    run_id: str = Field(pattern=UUID)
    session_id: str = Field(pattern=UUID)
    generation: int = Field(ge=1, le=4)
    task_sha256: str = Field(pattern=SHA)

    @model_validator(mode="after")
    def canonical_workflow(self):
        if self.workflow_id != f"session:{self.tenant}:{self.agent}:{self.session_id}":
            raise ValueError("noncanonical workflow identity")
        return self

    @property
    def manifest(self):
        return digest(
            {
                key: getattr(self, key)
                for key in (
                    "profile",
                    "tenant",
                    "agent",
                    "release",
                    "image",
                    "manifest_sha256",
                    "policy_sha256",
                )
            }
        )

    @property
    def invocation(self):
        return digest(self.model_dump(exclude={"generation"}))


class CoreCommand(Strict):
    schema_version: Literal[1]
    binding: CoreBinding
    task: str
    action: Literal["prepare", "execute"]
    position: Position | None
    operation: dict | None

    @field_validator("position", mode="before")
    @classmethod
    def exact_position(cls, value):
        if value is not None:
            parsed = Position.model_validate(value)
            if canonical_bytes(value) != canonical_bytes(parsed.model_dump()):
                raise ValueError("accepted position must use its complete canonical schema")
        return value

    @model_validator(mode="after")
    def pinned(self):
        task = self.task.encode()
        if len(task) > 65536 or hashlib.sha256(task).hexdigest() != self.binding.task_sha256:
            raise ValueError("task differs from execution binding")
        if self.action == "execute" and (self.position is None or self.operation is None):
            raise ValueError("execution requires an accepted position and operation")
        if self.action == "prepare" and self.operation is not None:
            raise ValueError("prepare cannot choose an operation")
        return self
