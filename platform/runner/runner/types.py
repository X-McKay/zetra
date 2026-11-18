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
WORKER_POOLS = ("legacy", "governed-v1")
MAX_TASK_BYTES = 65536
MAX_BOUNDARIES = 64
MAX_SUMMARY = 512
# Skills contract section 10: the record of a skill_run boundary. The vocabularies are the
# contract's (sections 2, 3, 5, 9.3, 9.4); the runner does not import agent_runtime.
SCRIPT_RUN_KEYS = frozenset(
    {
        "skill",
        "script",
        "digest",
        "argument_count",
        "arguments_sha256",
        "exit_status",
        "limits_hit",
        "refused",
        "duration_ms",
    }
)
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # section 2, at most 64 characters
SCRIPT_PATH_RE = re.compile(
    r"^scripts/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$"
)  # section 5, at most 128
SKILL_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")  # section 3
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
SCRIPT_LIMITS = ("memory", "output", "processes", "time")  # section 9.4
SCRIPT_REFUSALS = (  # section 9.3
    "unknown_skill",
    "undeclared",
    "unknown_script",
    "path",
    "arguments",
    "interpreter",
    "digest",
    "workspace",
    "unsupported",
)
SKILL_RUN_TOOL = "skill_run"


class InvalidInput(ValueError):
    pass


def worker_queue(tenant: str, role: str, pool: str = "legacy") -> str:
    if not isinstance(tenant, str) or not TENANT_RE.fullmatch(tenant):
        raise InvalidInput("invalid worker tenant")
    if role not in ("sessions", "cleanup") or pool not in WORKER_POOLS:
        raise InvalidInput("invalid worker role or pool")
    base = f"{role}-{tenant}"
    return base if pool == "legacy" else f"{base}-{pool}"


def worker_deployment(tenant: str, role: str, pool: str = "legacy") -> str:
    worker_queue(tenant, role, pool)
    base = f"{'session-runner' if role == 'sessions' else 'cleanup-reconciler'}-{tenant}"
    return base if pool == "legacy" else f"{base}-{pool}"


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
    # A workspace snapshot of this tenant's artifact store to restore into generation 1 before
    # the first boundary (the key is content-addressed, so it names one exact archive). The
    # store is per tenant, so a seed never crosses tenants; within a tenant it is the caller's
    # choice, like the task. The conformance harness uses it for T26.
    seed: str | None = None

    # Absent in historical payloads: never redirect replay to a new pool.
    worker_pool: str = "legacy"

    @property
    def workflow_id(self) -> str:
        return f"session:{self.tenant}:{self.agent}:{self.session_id}"

    @property
    def task_queue(self) -> str:
        return worker_queue(self.tenant, "sessions", self.worker_pool)

    def validate(self) -> None:
        """Section 10.2: identifiers in their fixed formats, no credentials, task at most 64 KiB."""
        worker_queue(self.tenant, "sessions", self.worker_pool)
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
        if self.seed is not None and not (
            isinstance(self.seed, str)
            and len(self.seed) == 64
            and all(c in "0123456789abcdef" for c in self.seed)
        ):
            raise InvalidInput("seed must be a SHA-256 hex digest of a workspace snapshot")
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
    # Skills contract section 10: present on a skill_run boundary and on no other.
    script_run: dict[str, Any] | None = None

    @classmethod
    def parse_strict(cls, doc: Any) -> BoundaryOut:
        """Section 10.4 schema. Unknown or missing fields and wrong types are rejected."""
        base = {"boundary", "kind", "done", "tool", "usage", "summary"}
        if not isinstance(doc, dict) or set(doc) not in (base, base | {"script_run"}):
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
        # A null script_run is the same as an absent one: asdict() of a record writes it.
        script_run = doc.get("script_run")
        if (tool == SKILL_RUN_TOOL) != (script_run is not None):
            raise ValueError("script_run belongs to skill_run boundaries only")
        if script_run is not None:
            _check_script_run(script_run)
        return cls(
            doc["boundary"], doc["kind"], doc["done"], tool, usage, doc["summary"], script_run
        )

    @property
    def tokens(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens if self.usage else 0


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_script_run(run: Any) -> None:
    """Section 10: exactly nine keys, each in its own vocabulary."""
    if not isinstance(run, dict) or set(run) != SCRIPT_RUN_KEYS:
        raise ValueError("script_run fields")
    skill, script, digest = run["skill"], run["script"], run["digest"]
    if skill is not None and not (
        isinstance(skill, str) and len(skill) <= 64 and SKILL_NAME_RE.fullmatch(skill)
    ):
        raise ValueError("script_run skill")
    if script is not None and not (
        isinstance(script, str)
        and len(script) <= 128
        and SCRIPT_PATH_RE.fullmatch(script)
        and not any(part in (".", "..") for part in script.split("/"))
    ):
        raise ValueError("script_run script")
    if digest is not None and not (isinstance(digest, str) and SKILL_DIGEST_RE.fullmatch(digest)):
        raise ValueError("script_run digest")
    if not _is_int(run["argument_count"]) or not 0 <= run["argument_count"] <= 32:
        raise ValueError("script_run argument_count")
    sha = run["arguments_sha256"]
    if not (isinstance(sha, str) and SHA256_HEX_RE.fullmatch(sha)):
        raise ValueError("script_run arguments_sha256")
    status = run["exit_status"]
    if status is not None and not (_is_int(status) and -64 <= status <= 255):
        raise ValueError("script_run exit_status")
    hit = run["limits_hit"]
    if (
        not isinstance(hit, list)
        or not all(isinstance(h, str) and h in SCRIPT_LIMITS for h in hit)
        or hit != sorted(set(hit))
    ):
        raise ValueError("script_run limits_hit")
    refused = run["refused"]
    if refused is not None and not (isinstance(refused, str) and refused in SCRIPT_REFUSALS):
        raise ValueError("script_run refused")
    if not _is_int(run["duration_ms"]) or run["duration_ms"] < 0:
        raise ValueError("script_run duration_ms")


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
