"""Versioned core transport and checkpoint validation; accepted state stays in Temporal."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import threading
from contextlib import contextmanager
from functools import wraps
from dataclasses import asdict
from pathlib import PurePosixPath

from agent_runtime.core_protocol import CoreBinding, CoreCommand, strict_json
from agent_runtime.core_protocol import MAX_CONTROL, decode_frame, encode_frame
from kubernetes.client.exceptions import ApiException
from temporalio import activity
from temporalio.exceptions import ApplicationError
from zetra._runtime.artifacts import Position, Receipt, Ref, Tape
from zetra._runtime.records import canonical_bytes, verify_operation
from zetra._runtime.workspace import validate_snapshot

from runner.types import BoundaryOut, Usage

MAX_SNAPSHOT = 64 * 1024 * 1024
EMPTY_WORKSPACE = canonical_bytes({"version": 1, "files": [], "directories": []})
EMPTY_POSITION = Position(
    workspace=Ref(
        sha256=hashlib.sha256(EMPTY_WORKSPACE).hexdigest(), byte_length=len(EMPTY_WORKSPACE)
    )
)
STATE_DIRS = {"state", "state/core", "state/core/objects", "state/core/attempts"}


# One bounded archive in memory per worker process; the worker otherwise admits
# 16 concurrent Activities and the deployed baseline has a 1 GiB memory limit.
_CHECKPOINT_MEMORY = threading.BoundedSemaphore(1)


@contextmanager
def checkpoint_memory():
    while not _CHECKPOINT_MEMORY.acquire(timeout=1):
        activity.heartbeat()
        if activity.is_cancelled():
            raise ApplicationError(
                "checkpoint canceled", type="CoreSnapshotCancelled", non_retryable=True
            )
    try:
        yield
    finally:
        _CHECKPOINT_MEMORY.release()


def memory_bounded(function):
    @wraps(function)
    def bounded(payload):
        with checkpoint_memory():
            return function(payload)

    return bounded


class ArchiveStore:
    def __init__(self, files):
        self.files = files

    def get(self, ref):
        ref = Ref.model_validate(ref)
        data = self.files.get("state/core/objects/" + ref.sha256)
        if (
            data is None
            or len(data) != ref.byte_length
            or hashlib.sha256(data).hexdigest() != ref.sha256
        ):
            raise ValueError("missing or corrupt checkpoint object")
        return data


def _archive(data):
    if len(data) > MAX_SNAPSHOT:
        raise ValueError("checkpoint exceeds 64 MiB")
    files, directories, seen, size = {}, set(), set(), 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                name = member.name.rstrip("/") if member.isdir() else member.name
                path = PurePosixPath(name)
                if (
                    not name
                    or path.is_absolute()
                    or ".." in path.parts
                    or path.as_posix() != name
                    or "\\" in name
                    or "\x00" in name
                    or name in seen
                    or len(name.encode()) > 1100
                    or len(seen) >= 12000
                ):
                    raise ValueError("unsafe or duplicate checkpoint path")
                seen.add(name)
                if member.isdir():
                    if (
                        name != "workspace"
                        and not name.startswith("workspace/")
                        and name not in STATE_DIRS
                    ):
                        raise ValueError("unexpected checkpoint directory")
                    directories.add(name)
                    continue
                if not member.isreg() or member.sparse is not None:
                    raise ValueError("checkpoint links and special files are forbidden")
                if not (
                    name.startswith("workspace/")
                    or name in {"state/core/binding.json", "state/core/lease"}
                    or re.fullmatch(r"state/core/objects/[a-f0-9]{64}", name)
                    or re.fullmatch(r"state/core/attempts/(0|[1-9][0-9]?)", name)
                ):
                    raise ValueError("unexpected checkpoint file")
                size += member.size
                if member.size < 0 or size > MAX_SNAPSHOT:
                    raise ValueError("expanded checkpoint exceeds limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("checkpoint file unavailable")
                with stream:
                    value = stream.read(member.size + 1)
                if len(value) != member.size:
                    raise ValueError("short checkpoint file")
                files[name] = value
            suffix = data[archive.offset :]
            if len(suffix) < 1024 or any(suffix) or len(data) % 512:
                raise ValueError("checkpoint has missing terminator or trailing data")
    except (tarfile.TarError, OSError, UnicodeError) as exc:
        raise ValueError("invalid checkpoint archive") from exc
    if not STATE_DIRS | {"workspace"} <= directories:
        raise ValueError("missing checkpoint directories")
    return files, directories


def _state(data, binding, position):
    files, directories = _archive(data)
    expected_binding = canonical_bytes(binding.model_dump(exclude={"generation"}))
    if (
        files.get("state/core/binding.json") != expected_binding
        or files.get("state/core/lease") != b""
    ):
        raise ValueError("checkpoint execution binding differs")
    for name, value in files.items():
        if (
            name.startswith("state/core/objects/")
            and name.rsplit("/", 1)[1] != hashlib.sha256(value).hexdigest()
        ):
            raise ValueError("corrupt checkpoint object")
    store = ArchiveStore(files)
    if store.get(EMPTY_POSITION.workspace) != EMPTY_WORKSPACE:
        raise ValueError("checkpoint has no initial workspace")
    tape = Tape(store, manifest=binding.manifest)
    records = tape.records(position)
    nodes, ref = [], position.head
    while ref is not None:
        node = json.loads(store.get(ref))
        workspace = Ref.model_validate(node["workspace"])
        validate_snapshot(store.get(workspace))
        nodes.append((ref, node, workspace))
        ref = Ref.model_validate(node["previous"]) if node["previous"] is not None else None
    positions = [EMPTY_POSITION]
    for index, (head, node, workspace) in enumerate(reversed(nodes)):
        positions.append(
            Position(
                head=head,
                cursor=index + 1,
                prefix_digest=node["prefix_digest"],
                workspace=workspace,
            )
        )
    if positions[-1] != position:
        raise ValueError("checkpoint position differs")
    attempts = {name for name in files if name.startswith("state/core/attempts/")}
    if attempts != {f"state/core/attempts/{index}" for index in range(position.cursor)}:
        raise ValueError("checkpoint attempt set differs from candidate prefix")
    for index, record in enumerate(records):
        op = record["operation"]
        if op["operation_id"] != f"{binding.invocation}:{index:08d}":
            raise ValueError("checkpoint belongs to another invocation")
        marker = strict_json(files[f"state/core/attempts/{index}"])
        if canonical_bytes(marker) != canonical_bytes(
            {"position": positions[index].model_dump(), "operation": op}
        ):
            raise ValueError("checkpoint attempt descriptor differs")
    expected_dirs, expected_files = validate_snapshot(store.get(position.workspace))
    if {
        name.removeprefix("workspace/") for name in directories if name.startswith("workspace/")
    } != {p.as_posix() for p in expected_dirs}:
        raise ValueError("checkpoint workspace directories differ")
    actual_files = {
        name.removeprefix("workspace/"): value
        for name, value in files.items()
        if name.startswith("workspace/")
    }
    if actual_files != {p.as_posix(): value for p, value in expected_files}:
        raise ValueError("checkpoint workspace files differ")
    return tape, positions


def validate_checkpoint(data, command, candidate):
    command = command if isinstance(command, CoreCommand) else CoreCommand.model_validate(command)
    if command.action != "execute":
        raise ValueError("checkpoint requires a pinned execution")
    receipt = Receipt.model_validate(candidate)
    tape, positions = _state(data, command.binding, receipt.position)
    tape.validate_receipt(command.position, command.operation, candidate)
    if positions[-2] != command.position:
        raise ValueError("checkpoint does not extend the exact accepted position")
    op = verify_operation(
        command.operation, manifest=command.binding.manifest, index=command.position.cursor
    )
    usage = None
    if op["kind"] == "model":
        from google.adk.models.llm_response import LlmResponse
        from zetra._runtime.records import decode_model

        response = decode_model(LlmResponse, tape.records(receipt.position)[-1]["outcome"]["value"])
        if response.usage_metadata is not None:
            usage = Usage(
                response.usage_metadata.prompt_token_count or 0,
                response.usage_metadata.candidates_token_count or 0,
            )
    boundary = BoundaryOut(
        command.position.cursor,
        op["kind"],
        False,
        op["executor"] if op["kind"] == "tool" else None,
        usage,
        "accepted core operation",
    )
    return {"position": receipt.position.model_dump(), "boundary": asdict(boundary)}


def validate_restore(data, binding, position):
    binding = binding if isinstance(binding, CoreBinding) else CoreBinding.model_validate(binding)
    position = Position.model_validate(position)
    _state(data, binding, position)


# Separate Activity names preserve the legacy history/protocol path.

CORE_STEP = ["/app/.venv/bin/python", "-m", "agent_runtime.core_step"]
CORE_SNAPSHOT = [
    "/app/.venv/bin/python",
    "-c",
    """
import fcntl, os, sys, tarfile
fd = os.open('/sandbox/state/core/lease', os.O_RDONLY | os.O_NOFOLLOW)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
def safe(member):
    return None if member.name == 'state/token.json' else member
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
    archive.add('/sandbox/workspace', arcname='workspace', filter=safe)
    archive.add('/sandbox/state', arcname='state', filter=safe)
""",
]


def _pod(binding):
    from runner import activities as act

    try:
        pod = act.deps().core.read_namespaced_pod(
            act.pod_name(binding.session_id, binding.generation), binding.tenant
        )
    except ApiException as exc:
        if exc.status == 404:
            raise ApplicationError(
                "core sandbox absent", type="CorePrepareLost", non_retryable=True
            ) from None
        act._kube_error(exc, "read core sandbox")
    labels = pod.metadata.labels or {}
    expected = {
        act.LABEL + "session": binding.session_id,
        act.LABEL + "generation": str(binding.generation),
        act.LABEL + "agent": binding.agent,
        act.LABEL + "release": binding.release,
    }
    containers = pod.spec.containers if pod.spec else []
    if {key: labels.get(key) for key in expected} != expected or len(containers) != 1:
        raise ApplicationError(
            "core pod identity differs", type="IdentityConflict", non_retryable=True
        )
    container = containers[0]
    configured = [item.value for item in container.env or [] if item.name == "AGENT_CORE_BINDING"]
    try:
        valid = (
            len(configured) == 1
            and CoreBinding.model_validate(strict_json(configured[0].encode())) == binding
        )
    except (TypeError, ValueError, AttributeError):
        valid = False
    if container.image != binding.image or not valid:
        raise ApplicationError(
            "core pod execution binding differs", type="IdentityConflict", non_retryable=True
        )
    return pod


def _operation(operation, binding, cursor):
    value = verify_operation(operation, manifest=binding.manifest, index=cursor)
    if value["operation_id"] != f"{binding.invocation}:{cursor:08d}":
        raise ValueError("operation belongs to another execution")
    return value


def _guest(command):
    from runner import activities as act

    _pod(command.binding)
    return act.deps().run_exec(
        command.binding.tenant,
        act.pod_name(command.binding.session_id, command.binding.generation),
        CORE_STEP,
        stdin=encode_frame(command.model_dump()),
        max_output=MAX_CONTROL + 16,
        deadline=act.EXEC_DEADLINE,
        heartbeat=activity.heartbeat,
    )


def _reply(result):
    if result.truncated or result.timed_out:
        raise ValueError("incomplete core reply")
    return decode_frame(result.stdout)


@activity.defn
def prepare_core(payload: dict) -> dict:
    from runner import activities as act

    command = CoreCommand.model_validate(payload)
    if command.action != "prepare":
        raise ApplicationError(
            "invalid preparation command", type="CoreProtocolError", non_retryable=True
        )
    try:
        result = _guest(command)
    except (act.ExecNotStarted, act.ExecLost):
        raise ApplicationError(
            "core preparation unavailable", type="CorePrepareLost", non_retryable=True
        ) from None
    if result.timed_out or result.rc is None:
        raise ApplicationError(
            "core preparation did not finish", type="CorePrepareLost", non_retryable=True
        )
    try:
        reply = _reply(result)
        status = reply["status"]
        expected_fields = {"status", "position", "operation" if status == "operation" else "text"}
        if set(reply) != expected_fields or (result.rc, status) not in {
            (0, "operation"),
            (2, "done"),
        }:
            raise ValueError("invalid prepare reply")
        checked = CoreCommand.model_validate({**payload, "position": reply["position"]})
        if checked.position != (command.position or EMPTY_POSITION):
            raise ValueError("prepare changed the accepted position")
        if status == "operation":
            _operation(reply["operation"], command.binding, checked.position.cursor)
        elif not isinstance(reply["text"], str) or len(reply["text"].encode()) > 65536:
            raise ValueError("invalid completion text")
        return reply
    except (ValueError, KeyError, TypeError):
        raise ApplicationError(
            "invalid core preparation response", type="CoreProtocolError", non_retryable=True
        ) from None


@activity.defn
def execute_core(payload: dict) -> dict:
    command = CoreCommand.model_validate(payload)
    if command.action != "execute":
        raise ApplicationError(
            "invalid execute command", type="CoreProtocolError", non_retryable=True
        )
    _operation(command.operation, command.binding, command.position.cursor)
    try:
        result = _guest(command)
        reply = _reply(result)
        if result.rc in (3, 4) and reply == {
            "status": "refused",
            "reason": "policy" if result.rc == 3 else "budget",
        }:
            return {
                **reply,
                "status": "refused" if command.operation["kind"] == "model" else "unknown",
            }
        if (
            result.rc == 1
            and reply.get("status") == "unknown"
            and set(reply) <= {"status", "reason"}
        ):
            if reply.get("reason") not in (None, "policy", "budget"):
                raise ValueError("invalid refusal reason")
            return reply
        if result.rc != 0 or set(reply) != {"status", "receipt"} or reply["status"] != "candidate":
            raise ValueError("invalid candidate reply")
        receipt = Receipt.model_validate(reply["receipt"])
        if (
            canonical_bytes(receipt.model_dump()) != canonical_bytes(reply["receipt"])
            or receipt.previous != command.position.head
            or receipt.position.cursor != command.position.cursor + 1
            or receipt.position.head is None
            or receipt.position.workspace is None
            or receipt.operation_id != command.operation["operation_id"]
            or receipt.request_digest != command.operation["request_digest"]
        ):
            raise ValueError("candidate metadata differs")
        return reply  # candidate only; no accepted state or artifact-store write here
    except Exception:  # noqa: BLE001 -- any lost/malformed execute outcome is uncertain
        return {"status": "unknown"}


@activity.defn
@memory_bounded
def snapshot_core(payload: dict) -> dict:
    from runner import activities as act

    command = CoreCommand.model_validate(payload["command"])
    if set(payload) != {"command", "receipt"} or command.action != "execute":
        raise ApplicationError(
            "invalid checkpoint command", type="CoreProtocolError", non_retryable=True
        )
    _pod(command.binding)
    output = io.BytesIO()

    def sink(chunk):
        if output.tell() + len(chunk) > MAX_SNAPSHOT:
            raise ValueError("checkpoint exceeds 64 MiB")
        output.write(chunk)

    try:
        result = act.deps().run_exec(
            command.binding.tenant,
            act.pod_name(command.binding.session_id, command.binding.generation),
            CORE_SNAPSHOT,
            max_output=MAX_SNAPSHOT,
            deadline=act.SNAPSHOT_DEADLINE,
            on_stdout=sink,
            heartbeat=activity.heartbeat,
        )
        if result.rc != 0 or result.timed_out or result.truncated:
            raise ValueError("checkpoint process did not finish")
        data = output.getvalue()
        checked = validate_checkpoint(data, command, payload["receipt"])
        key = hashlib.sha256(data).hexdigest()
        output.seek(0)
        act.deps().store.put(key, output)
        with act.deps().store.open(key) as persisted:
            stored = persisted.read(MAX_SNAPSHOT + 1)
        if len(stored) != len(data) or hashlib.sha256(stored).hexdigest() != key:
            raise ValueError("persisted checkpoint differs")
        return {**checked, "snapshot": key}
    except Exception:  # noqa: BLE001 -- snapshot retries never redispatch the effect
        raise ApplicationError(
            "core checkpoint unavailable or invalid", type="CoreSnapshotFailed"
        ) from None


def release_identity(binding):
    return {
        key: getattr(binding, key)
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


def preflight_configuration(deps, inp, release):
    from runner import activities as act

    spec, status = release["spec"], release.get("status") or {}
    try:
        if (
            spec["runtimeProfile"] != "zetra-core-v1"
            or status.get("signatureVerified") is not True
            or (spec.get("approval") or {}).get("approved") is not True
            or status.get("policyDigest") != "sha256:" + spec["policySha256"]
        ):
            raise ValueError("unverified core release")
        for key in ("manifestSha256", "policySha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", spec[key]):
                raise ValueError("invalid release digest")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}", spec["image"]):
            raise ValueError("mutable core image")
        cm = deps.core.read_namespaced_config_map("policy-" + inp.release, inp.tenant)
        bundle = strict_json(cm.data["bundle.json"].encode())
        if hashlib.sha256(canonical_bytes(bundle)).hexdigest() != spec["policySha256"]:
            raise ValueError("core policy digest differs")
        if any(
            bundle.get(k) != v
            for k, v in {
                "tenant": inp.tenant,
                "agent": inp.agent,
                "release": inp.release,
                "image": spec["image"],
            }.items()
        ):
            raise ValueError("core policy identity differs")
        runtime = bundle["runtime"]
        maximum = runtime["max_boundaries"]
        if (
            runtime.get("execution_profile") != "zetra-core-v1"
            or type(maximum) is not int
            or not 1 <= maximum <= 64
        ):
            raise ValueError("core policy runtime differs")
        return {
            "binding": {
                "profile": "zetra-core-v1",
                "tenant": inp.tenant,
                "agent": inp.agent,
                "release": inp.release,
                "image": spec["image"],
                "manifest_sha256": spec["manifestSha256"],
                "policy_sha256": spec["policySha256"],
            },
            "max_boundaries": maximum,
        }
    except ApiException as exc:
        act._kube_error(exc, "core policy")
    except (KeyError, ValueError, TypeError, AttributeError):
        raise act._forbidden("unverified core release configuration") from None


def task_text(inp):
    return inp.task if isinstance(inp.task, str) else canonical_bytes(inp.task).decode()


def binding_for(inp, config, info, generation):
    if (
        info.workflow_id != inp.workflow_id
        or info.attempt != 1
        or info.continued_run_id
        or info.cron_schedule
        or info.first_execution_run_id != info.run_id
        or info.original_execution_run_id != info.run_id
    ):
        raise ValueError("core execution requires its canonical original workflow run")
    return CoreBinding.model_validate(
        {
            **config["binding"],
            "namespace": info.namespace,
            "workflow_id": info.workflow_id,
            "run_id": info.run_id,
            "session_id": inp.session_id,
            "generation": generation,
            "task_sha256": hashlib.sha256(task_text(inp).encode()).hexdigest(),
        }
    ).model_dump()


@activity.defn
def create_core_sandbox(payload: dict) -> str:
    import yaml
    from runner import activities as act

    binding = CoreBinding.model_validate(payload["binding"])
    deps = act.deps()
    name = act.pod_name(binding.session_id, binding.generation)
    try:
        cm = deps.core.read_namespaced_config_map("podtemplate-" + binding.release, binding.tenant)
    except ApiException as exc:
        act._kube_error(exc, "core pod template")
    try:
        pod = yaml.safe_load(cm.data["pod.yaml"])
        containers = pod["spec"]["containers"]
        expected = release_identity(binding)
        configured = [
            entry["value"]
            for entry in containers[0].get("env", [])
            if entry.get("name") == "AGENT_RELEASE_BINDING"
        ]
        if (
            pod["kind"] != "Pod"
            or len(containers) != 1
            or containers[0]["image"] != binding.image
            or len(configured) != 1
            or strict_json(configured[0].encode()) != expected
        ):
            raise ValueError("core template identity differs")
        metadata = pod["metadata"]
        labels = metadata["labels"]
        if (
            labels.get(act.LABEL + "release") != binding.release
            or labels.get(act.LABEL + "agent") != binding.agent
        ):
            raise ValueError("core template labels differ")
        metadata.update(name=name, namespace=binding.tenant)
        labels.update(
            {
                act.LABEL + "session": binding.session_id,
                act.LABEL + "generation": str(binding.generation),
            }
        )
        env = [
            entry
            for entry in containers[0].get("env", [])
            if entry.get("name") not in {"AGENT_CORE_BINDING", "AGENT_SESSION_ID"}
        ]
        containers[0]["env"] = env + [
            {"name": "AGENT_SESSION_ID", "value": binding.session_id},
            {"name": "AGENT_CORE_BINDING", "value": canonical_bytes(binding.model_dump()).decode()},
        ]
    except (KeyError, IndexError, ValueError, TypeError, AttributeError):
        raise act._forbidden("invalid core pod template") from None
    try:
        deps.core.create_namespaced_pod(binding.tenant, pod)
    except ApiException as exc:
        if exc.status != 409:
            act._kube_error(exc, "create core sandbox")
    return _pod(binding).metadata.uid


@activity.defn
def wait_core_ready(payload: dict) -> None:
    import time
    from runner import activities as act

    binding = CoreBinding.model_validate(payload["binding"])
    deadline = time.monotonic() + act.READY_DEADLINE
    while time.monotonic() < deadline:
        activity.heartbeat()
        pod = _pod(binding)
        phase = pod.status.phase if pod.status else None
        if phase in {"Failed", "Succeeded"}:
            raise ApplicationError(
                "core sandbox exited before readiness", type="CorePrepareLost", non_retryable=True
            )
        if phase == "Running":
            key = payload.get("restore")
            if key is not None:
                with checkpoint_memory():
                    try:
                        if not re.fullmatch(r"[a-f0-9]{64}", key):
                            raise ValueError("invalid checkpoint key")
                        with act.deps().store.open(key) as source:
                            data = source.read(MAX_SNAPSHOT + 1)
                        if len(data) > MAX_SNAPSHOT or hashlib.sha256(data).hexdigest() != key:
                            raise ValueError("checkpoint storage differs")
                        validate_restore(data, binding, payload["position"])
                    except (OSError, KeyError, ValueError, TypeError):
                        raise ApplicationError(
                            "invalid core restore checkpoint",
                            type="CoreIntegrityError",
                            non_retryable=True,
                        ) from None
                    act._restore(act.deps(), binding.tenant, pod.metadata.name, key)
            elif payload.get("position") not in (None, EMPTY_POSITION.model_dump()):
                raise ApplicationError(
                    "accepted state has no checkpoint",
                    type="CoreIntegrityError",
                    non_retryable=True,
                )
            return
        time.sleep(act.POLL)
    raise ApplicationError("core sandbox not ready", type="CorePrepareLost")


@activity.defn
def delete_core_sandbox(payload: dict) -> str:
    import time
    from kubernetes.client import V1DeleteOptions, V1Preconditions
    from runner import activities as act

    binding = CoreBinding.model_validate(payload)
    try:
        pod = _pod(binding)
    except ApplicationError as exc:
        if exc.type == "CorePrepareLost":
            return "DELETED"
        raise
    name, uid = pod.metadata.name, pod.metadata.uid
    try:
        act.deps().core.delete_namespaced_pod(
            name,
            binding.tenant,
            body=V1DeleteOptions(grace_period_seconds=30, preconditions=V1Preconditions(uid=uid)),
        )
    except ApiException as exc:
        if exc.status != 404:
            act._kube_error(exc, "delete core sandbox")
    deadline = time.monotonic() + act.DELETE_DEADLINE
    while time.monotonic() < deadline:
        activity.heartbeat()
        try:
            current = act.deps().core.read_namespaced_pod(name, binding.tenant)
        except ApiException as exc:
            if exc.status == 404:
                return "DELETED"
            act._kube_error(exc, "confirm core deletion")
        if current.metadata.uid != uid:
            return "NOT_CONFIRMED"
        time.sleep(act.POLL)
    return "NOT_CONFIRMED"


CORE_ACTIVITIES = [
    prepare_core,
    execute_core,
    snapshot_core,
    create_core_sandbox,
    wait_core_ready,
    delete_core_sandbox,
]
