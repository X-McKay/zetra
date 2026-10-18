"""Hash-verified immutable data. Only a caller-supplied accepted Position selects progress."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .records import canonical_bytes, digest, verify_operation


class IntegrityError(ValueError):
    pass


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0, le=268435456)


class ArtifactStore(Protocol):
    """Caller-owned blocking immutable storage; implementations verify bytes against Ref."""

    def put(self, data: bytes) -> Ref: ...

    def get(self, ref: Ref) -> bytes: ...


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    head: Ref | None = None
    cursor: int = Field(default=0, ge=0, le=64)
    prefix_digest: str = Field(default=hashlib.sha256(b"").hexdigest(), pattern=r"^[0-9a-f]{64}$")
    workspace: Ref | None = None


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    previous: Ref | None
    operation_id: str
    request_digest: str
    position: Position


class FileStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> Ref:
        ref = Ref(sha256=hashlib.sha256(data).hexdigest(), byte_length=len(data))
        path = self.root / ref.sha256
        fd, tmp = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
            self.get(ref)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(tmp)
        return ref

    def get(self, ref: Ref) -> bytes:
        ref = Ref.model_validate(ref)
        try:
            fd = os.open(self.root / ref.sha256, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as f:
                data = f.read(ref.byte_length + 1)
        except OSError as exc:
            raise IntegrityError("missing or unsafe artifact") from exc
        if len(data) != ref.byte_length or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise IntegrityError("artifact hash/size mismatch")
        return data


class Tape:
    def __init__(self, store, *, manifest, max_bytes=8388608):
        self.store = store
        self.manifest = manifest
        self.max_bytes = max_bytes

    def empty(self, workspace=None):
        return Position(workspace=workspace)

    def _pack(self, value):
        data = canonical_bytes(value)
        if len(data) > 65536:
            return {"ref": self.store.put(data).model_dump()}
        return {"inline": value}

    def _unpack(self, envelope, account):
        if set(envelope) == {"inline"}:
            return envelope["inline"]
        if set(envelope) != {"ref"}:
            raise IntegrityError("invalid payload envelope")
        data = self.store.get(Ref.model_validate(envelope["ref"]))
        account(len(data))
        return json.loads(data)

    def append_candidate(self, previous, operation, outcome, workspace=None):
        # Validate accepted predecessor before extending it, including its budget.
        entries = self.load(previous)
        if (
            entries
            and operation["operation_id"].rsplit(":", 1)[0]
            != entries[0]["operation"]["operation_id"].rsplit(":", 1)[0]
        ):
            raise ValueError("invocation mismatch")
        operation = verify_operation(operation, manifest=self.manifest, index=previous.cursor)
        if set(outcome) != {"status", "value"} or outcome["status"] not in (
            "succeeded",
            "known_error",
        ):
            raise ValueError("only definitive outcomes can become candidates")
        workspace = workspace if workspace is not None else previous.workspace
        semantic = {
            "operation": operation,
            "outcome": outcome,
            "workspace": workspace.model_dump() if workspace else None,
        }
        prefix = digest({"previous_prefix": previous.prefix_digest, **semantic})
        node = {
            "previous": previous.head.model_dump() if previous.head else None,
            "operation": self._pack(operation),
            "outcome": self._pack(outcome),
            "workspace": semantic["workspace"],
            "prefix_digest": prefix,
        }
        head = self.store.put(canonical_bytes(node))
        position = Position(
            head=head, cursor=previous.cursor + 1, prefix_digest=prefix, workspace=workspace
        )
        self.load(position)  # Reject over-budget candidate before returning a receipt.
        return Receipt(
            previous=previous.head,
            operation_id=operation["operation_id"],
            request_digest=operation["request_digest"],
            position=position,
        )

    def load(self, position):
        position = Position.model_validate(position)
        total = 0

        def account(size):
            nonlocal total
            total += size
            if total > self.max_bytes:
                raise IntegrityError("tape exceeds aggregate budget")

        nodes = []
        seen = set()
        ref = position.head
        try:
            while ref:
                if ref.sha256 in seen or len(nodes) >= 64:
                    raise IntegrityError("cycle or excessive tape depth")
                seen.add(ref.sha256)
                data = self.store.get(ref)
                account(len(data))
                node = json.loads(data)
                if set(node) != {"previous", "operation", "outcome", "workspace", "prefix_digest"}:
                    raise IntegrityError("invalid tape node")
                node["operation"] = self._unpack(node["operation"], account)
                node["outcome"] = self._unpack(node["outcome"], account)
                nodes.append(node)
                ref = Ref.model_validate(node["previous"]) if node["previous"] else None
            prefix = self.empty().prefix_digest
            entries = []
            for index, node in enumerate(reversed(nodes)):
                operation = verify_operation(node["operation"], manifest=self.manifest, index=index)
                outcome = node["outcome"]
                if set(outcome) != {"status", "value"} or outcome["status"] not in (
                    "succeeded",
                    "known_error",
                ):
                    raise IntegrityError("uncertain tape outcome")
                prefix = digest(
                    {
                        "previous_prefix": prefix,
                        "operation": operation,
                        "outcome": outcome,
                        "workspace": node["workspace"],
                    }
                )
                if prefix != node["prefix_digest"]:
                    raise IntegrityError("prefix mismatch")
                if (
                    entries
                    and operation["operation_id"].rsplit(":", 1)[0]
                    != entries[0]["operation"]["operation_id"].rsplit(":", 1)[0]
                ):
                    raise IntegrityError("invocation mismatch")
                entries.append({"operation": operation, "outcome": outcome["value"]})
            if len(entries) != position.cursor or prefix != position.prefix_digest:
                raise IntegrityError("position mismatch")
            if nodes and nodes[0]["workspace"] != (
                position.workspace.model_dump() if position.workspace else None
            ):
                raise IntegrityError("workspace mismatch")
            return entries
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityError(str(exc)) from exc
