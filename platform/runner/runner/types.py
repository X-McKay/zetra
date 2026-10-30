"""Workflow and activity payloads. Plain dataclasses, so Temporal's JSON converter carries them."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

TENANT_RE = re.compile(r"^tenant-[a-z0-9-]{1,50}$")
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# Section 13.6: a JWT, or a bearer credential.
TOKEN_PATTERNS = (
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"Bearer [A-Za-z0-9._~+/=-]{20,}"),
)
CREDENTIAL_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "password",
    "authorization",
    "client_assertion",
}
MAX_TASK_BYTES = 65536
MAX_BOUNDARIES = 64
MAX_SUMMARY = 512


class InvalidInput(ValueError):
    pass


def looks_like_token(text: str) -> bool:
    return any(p.search(text) for p in TOKEN_PATTERNS)


def _credential_keys(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(k).lower() in CREDENTIAL_KEYS or _credential_keys(v) for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_credential_keys(v) for v in value)
    return False


@dataclass
class SessionInput:
    tenant: str
    agent: str
    release: str
    session_id: str
    grant_handle: str | None
    task: Any
    max_boundaries: int = MAX_BOUNDARIES

    @property
    def workflow_id(self) -> str:
        return f"session:{self.tenant}:{self.agent}:{self.session_id}"

    @property
    def task_queue(self) -> str:
        return f"sessions-{self.tenant}"

    def validate(self) -> None:
        """Section 10.2: identifiers in their fixed formats, no credentials, task at most 64 KiB."""
        if not TENANT_RE.match(self.tenant):
            raise InvalidInput("tenant must match ^tenant-[a-z0-9-]{1,50}$")
        if not NAME_RE.match(self.agent) or not NAME_RE.match(self.release):
            raise InvalidInput("agent and release must be DNS labels")
        try:
            parsed = uuid.UUID(self.session_id)
        except (ValueError, AttributeError, TypeError):
            raise InvalidInput("session_id must be a UUID") from None
        if parsed.version != 4 or str(parsed) != self.session_id:
            raise InvalidInput("session_id must be a lower-case UUID v4")
        if self.grant_handle is not None and (
            not isinstance(self.grant_handle, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.grant_handle)
        ):
            raise InvalidInput("grant_handle must be an opaque handle")
        if (
            isinstance(self.max_boundaries, bool)
            or not isinstance(self.max_boundaries, int)
            or not 1 <= self.max_boundaries <= MAX_BOUNDARIES
        ):
            raise InvalidInput(f"max_boundaries must be 1..{MAX_BOUNDARIES}")
        try:
            encoded = json.dumps(self.task, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise InvalidInput("task must be JSON") from None
        if len(encoded.encode("utf-8")) > MAX_TASK_BYTES:
            raise InvalidInput("task exceeds 64 KiB")
        fields_text = " ".join(
            [self.tenant, self.agent, self.release, self.session_id, self.grant_handle or ""]
        )
        if (
            looks_like_token(encoded)
            or looks_like_token(fields_text)
            or _credential_keys(self.task)
        ):
            raise InvalidInput("session input must not carry tokens or credentials")


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class BoundaryOut:
    boundary: int
    kind: str
    done: bool
    tool: str | None
    usage: Usage | None
    summary: str

    @classmethod
    def parse_strict(cls, doc: Any) -> BoundaryOut:
        """Section 10.4 schema. Unknown or missing fields and wrong types are rejected."""
        if not isinstance(doc, dict) or set(doc) != {
            "boundary",
            "kind",
            "done",
            "tool",
            "usage",
            "summary",
        }:
            raise ValueError("boundary output fields")
        if not _is_int(doc["boundary"]) or doc["boundary"] < 0:
            raise ValueError("boundary")
        if doc["kind"] not in ("model", "tool"):
            raise ValueError("kind")
        if not isinstance(doc["done"], bool):
            raise ValueError("done")
        tool = doc["tool"]
        if tool is not None and (not isinstance(tool, str) or not 0 < len(tool) <= 128):
            raise ValueError("tool")
        usage = doc["usage"]
        if usage is not None:
            if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}:
                raise ValueError("usage fields")
            if not all(_is_int(v) and v >= 0 for v in usage.values()):
                raise ValueError("usage values")
            usage = Usage(usage["input_tokens"], usage["output_tokens"])
        if not isinstance(doc["summary"], str) or len(doc["summary"]) > MAX_SUMMARY:
            raise ValueError("summary")
        return cls(doc["boundary"], doc["kind"], doc["done"], tool, usage, doc["summary"])

    @property
    def tokens(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens if self.usage else 0


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass
class SessionResult:
    outcome: str
    cleanup: dict[str, str] = field(default_factory=dict)
    generation: int = 0
    boundaries: int = 0
    snapshot: str | None = None


@dataclass
class GenIn:
    inp: SessionInput
    generation: int
    restore: str | None


@dataclass
class BoundaryIn:
    inp: SessionInput
    generation: int
    boundary: int


@dataclass
class SnapshotIn:
    inp: SessionInput
    generation: int
    boundary: int


@dataclass
class SandboxPod:
    name: str
    agent: str
    session_id: str
    generation: int
    deleting_since: str | None = None


@dataclass
class ApsSession:
    session_id: str
    agent: str
    generation: int | None
    grant_handle: str | None


@dataclass
class ReconcileInputs:
    pods: list[SandboxPod]
    sessions: list[ApsSession]


@dataclass
class OrphanPod:
    tenant: str
    pod: SandboxPod
    reason: str


@dataclass
class ClosedSession:
    tenant: str
    session: ApsSession
