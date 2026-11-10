"""Session activities (10.3). Synchronous: the worker runs them in a thread pool.

Clients are injected through `configure()`; nothing touches the cluster at import time.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Protocol
from urllib.parse import quote

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream.stream import _websocket_request
from kubernetes.stream import ws_client
from temporalio import activity
from temporalio.exceptions import ApplicationError

from runner import records
from runner.memory_audit import MemoryAudit
from runner.aps import APS
from runner.types import (
    ApsSession,
    BoundaryIn,
    BoundaryOut,
    ClosedSession,
    GenIn,
    MAX_BOUNDARIES,
    MAX_TASK_BYTES,
    InvalidInput,
    OrphanPod,
    ReconcileInputs,
    SandboxPod,
    SessionInput,
    SnapshotIn,
)

GROUP, VERSION = "agentplatform.io", "v1alpha1"
LABEL = "agentplatform.io/"
EXPECTED_POLICIES = {
    "default-deny-all",
    "sandbox-egress-gateway-only",
    "sandbox-ingress-none",
    "tenant-no-other-egress",
}
MAX_OUTPUT = 65536
MAX_INPUT = MAX_TASK_BYTES + 64  # the task plus the {"boundary": n, "task": ...} envelope
MAX_SNAPSHOT = 64 * 1024 * 1024
STEP_CMD = ["/app/.venv/bin/python", "-m", "agent_runtime.step"]
# The agent access token under state/ is a credential and never enters an artifact (13.9).
SNAPSHOT_CMD = [
    "tar",
    "-C",
    "/sandbox",
    "--exclude=state/token.json",
    "-cf",
    "-",
    "workspace",
    "state",
]
# The exec channel has no end-of-input signal, so the archive is length-prefixed too.
RESTORE_CMD = ["sh", "-c", 'head -c "$1" | tar -x -C /sandbox -f -', "restore"]
# A seed (10.2) is another session's final snapshot: only its workspace enters the new sandbox.
# Its state/ belongs to the donor (state.json names the donor's next boundary), and restoring
# it makes the seeded session's first boundary a "boundary mismatch" (step rc 5).
SEED_RESTORE_CMD = ["sh", "-c", 'head -c "$1" | tar -x -C /sandbox -f - workspace', "restore"]
EXEC_DEADLINE = 290.0  # inside the 300 s start-to-close of run_boundary
SNAPSHOT_DEADLINE = 110.0  # inside the 120 s start-to-close of snapshot_workspace
READY_DEADLINE = 170.0
DELETE_DEADLINE = 110.0
POLL = 2.0
STDIN_CHUNK = 1 << 20


# --- artifact store ---------------------------------------------------------------------------


class ArtifactStore(Protocol):
    """Write-once objects named by SHA-256 (10.6): the S3 bucket agent-artifacts-<tenant>."""

    def put(self, key: str, src: BinaryIO) -> None: ...

    def open(self, key: str) -> BinaryIO: ...


class FilesystemStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("artifact key must be a SHA-256 hex digest")
        return self.root / key[:2] / key

    def put(self, key: str, src: BinaryIO) -> None:
        dest = self._path(key)
        if dest.exists():
            return  # content-addressed: the same key is the same bytes
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := src.read(STDIN_CHUNK):
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(tmp, dest)  # write-once: never replaces an existing object
            except FileExistsError:
                pass
        finally:
            os.unlink(tmp)

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")


# --- exec ---------------------------------------------------------------------------------------


class ExecNotStarted(Exception):
    pass


class ExecLost(Exception):
    pass


@dataclass
class ExecResult:
    rc: int | None
    stdout: bytes
    stderr: str
    truncated: bool
    timed_out: bool = False


def _ws_call(configuration, _method, url, **kwargs):
    # capture_all=False: the client otherwise keeps a second, unbounded copy of all output.
    kwargs["capture_all"] = False
    return ws_client.websocket_call(configuration, _method, url, **kwargs)


_stream_nocapture = functools.partial(_websocket_request, _ws_call, None)


def kube_exec(
    core,
    ns: str,
    name: str,
    cmd: list[str],
    *,
    stdin: bytes | BinaryIO | None = None,
    max_output: int = MAX_OUTPUT,
    deadline: float = EXEC_DEADLINE,
    on_stdout: Callable[[bytes], None] | None = None,
    heartbeat: Callable[[], None] | None = None,
    stream_fn=None,
) -> ExecResult:
    """Exec in the agent container. Output beyond max_output, or the deadline, ends the exec."""
    try:
        ws = (stream_fn or _stream_nocapture)(
            core.connect_get_namespaced_pod_exec,
            name,
            ns,
            command=cmd,
            container="agent",
            stdin=stdin is not None,
            stdout=True,
            stderr=True,
            tty=False,
            binary=True,
            _preload_content=False,
        )
    except Exception as e:
        raise ExecNotStarted(type(e).__name__) from None
    out, err = bytearray(), bytearray()
    size, truncated, timed_out = 0, False, False

    def take() -> bool:
        nonlocal size, truncated
        if ws.peek_stdout():
            chunk = _bytes(ws.read_stdout())
            size += len(chunk)
            if size > max_output:
                truncated = True
                return False
            if on_stdout:
                on_stdout(chunk)
            else:
                out.extend(chunk)
        if ws.peek_stderr() and len(err) < 4096:
            err.extend(_bytes(ws.read_stderr())[: 4096 - len(err)])
        return True

    try:
        if stdin is not None:
            src = stdin if hasattr(stdin, "read") else None
            data = None if src else bytes(stdin)
            offset = 0
            while True:
                chunk = src.read(STDIN_CHUNK) if src else data[offset : offset + STDIN_CHUNK]
                offset += STDIN_CHUNK
                if not chunk:
                    break
                ws.write_stdin(chunk)
                if heartbeat:
                    heartbeat()
        end = time.monotonic() + deadline
        while ws.is_open():
            if time.monotonic() > end:
                timed_out = True
                break
            ws.update(timeout=1)
            if heartbeat:
                heartbeat()
            if not take():
                break
        else:
            take()  # frames buffered before the close frame
        rc = None
        if not (truncated or timed_out):
            try:
                rc = ws.returncode
            except Exception:
                rc = None  # no or malformed status: unknown, never success
        return ExecResult(rc, bytes(out), err.decode("utf-8", "replace"), truncated, timed_out)
    except Exception as e:
        raise ExecLost(type(e).__name__) from None
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _bytes(chunk) -> bytes:
    return chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")


# --- dependencies ---------------------------------------------------------------------------------


@dataclass
class Deps:
    core: Any
    net: Any
    custom: Any
    aps: Any
    store: ArtifactStore
    exec: Callable[..., ExecResult] | None = None
    temporal: Any = None  # temporalio Client, used by the reconciler only
    serializer: Any = field(default_factory=client.ApiClient)

    def run_exec(self, ns: str, name: str, cmd: list[str], **kw) -> ExecResult:
        return (self.exec or functools.partial(kube_exec, self.core))(ns, name, cmd, **kw)

    @classmethod
    def from_cluster(cls) -> Deps:
        config.load_incluster_config()
        api = client.ApiClient()
        return cls(
            core=client.CoreV1Api(api),
            net=client.NetworkingV1Api(api),
            custom=client.CustomObjectsApi(api),
            aps=APS.from_env(),
            store=FilesystemStore(os.environ.get("ARTIFACT_DIR", "/var/lib/agent-artifacts")),
            serializer=api,
        )


_DEPS: Deps | None = None
_LOCK = threading.Lock()


def configure(d: Deps | None) -> None:
    global _DEPS
    _DEPS = d


def deps() -> Deps:
    global _DEPS
    with _LOCK:
        if _DEPS is None:
            _DEPS = Deps.from_cluster()
        return _DEPS


# --- helpers ----------------------------------------------------------------------------------------


def pod_name(session_id: str, generation: int) -> str:
    return f"s-{hashlib.sha256(session_id.encode()).hexdigest()[:16]}-g{generation}"


def canonical_policy(p, serializer=None) -> str:
    spec = (serializer or client.ApiClient()).sanitize_for_serialization(p.spec)
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def netpol_digest(policies, serializer=None) -> tuple[set[str], str]:
    """The 10.5 algorithm, unchanged: the Registry Controller must publish exactly this value."""
    pols = {p.metadata.name: canonical_policy(p, serializer) for p in policies}
    have = hashlib.sha256(json.dumps(pols, sort_keys=True).encode()).hexdigest()
    return set(pols), f"sha256:{have}"


def _kube_error(e: ApiException, what: str):
    if e.status == 404:
        raise ApplicationError(f"{what} not found", type="NotFound", non_retryable=True) from None
    if e.status in (400, 401, 403, 409, 422):
        raise ApplicationError(
            f"{what}: kubernetes refused with {e.status}", type="Forbidden", non_retryable=True
        ) from None
    raise ApplicationError(f"{what}: kubernetes error {e.status}", type="KubernetesUnavailable")


def _forbidden(message: str) -> ApplicationError:
    return ApplicationError(message, type="Forbidden", non_retryable=True)


# --- session activities ---------------------------------------------------------------------------


def _agent_boundary_limit(d: Deps, inp: SessionInput, status: dict) -> int:
    """The Agent's `spec.runtime.maxBoundaries`, read from the release's policy bundle.

    The bundle is what the controller compiled from the Agent and found equal to the
    release's `policySha256`; its bytes are held to `status.policyDigest` here, so the limit
    is the one the release was approved with and not whatever the ConfigMap says now.
    """
    try:
        cm = d.core.read_namespaced_config_map(f"policy-{inp.release}", inp.tenant)
    except ApiException as e:
        _kube_error(e, "policy bundle")
    raw = (cm.data or {}).get("bundle.json")
    digest = status.get("policyDigest")
    if (
        not isinstance(raw, str)
        or not isinstance(digest, str)
        or "sha256:" + hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest() != digest
    ):
        raise _forbidden("policy bundle differs from the release's policy digest")
    try:
        limit = json.loads(raw)["runtime"]["max_boundaries"]
    except (ValueError, KeyError, TypeError):
        raise _forbidden("policy bundle carries no boundary limit") from None
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_BOUNDARIES:
        raise _forbidden(f"policy bundle boundary limit must be 1..{MAX_BOUNDARIES}")
    return limit


@activity.defn
def preflight(inp: SessionInput) -> int | None:
    """Refuse the session or return the Agent's boundary limit.

    Always an integer. The annotation allows none only so that the workflow can replay
    histories recorded when this returned nothing.
    """
    try:
        inp.validate()
    except InvalidInput as e:
        raise _forbidden(str(e)) from None
    d, ns = deps(), inp.tenant
    try:
        rel = d.custom.get_namespaced_custom_object(
            GROUP, VERSION, ns, "agentreleases", inp.release
        )
        policies = d.net.list_namespaced_network_policy(ns).items
        namespace = d.core.read_namespace(ns)
    except ApiException as e:
        _kube_error(e, "preflight read")
    status = rel.get("status") or {}
    if status.get("ready") is not True:
        raise _forbidden("release not ready")
    if (rel.get("spec") or {}).get("agent") != inp.agent:
        raise _forbidden("release belongs to another agent")
    names, have = netpol_digest(policies, d.serializer)
    want = (namespace.metadata.annotations or {}).get(LABEL + "netpol-digest")
    if names != EXPECTED_POLICIES or want != have:
        # Bounded identifiers from the exact preflight LIST, not a later harness poll.
        # Temporal encrypts failure details with the tenant payload codec. T28 uses
        # this snapshot to distinguish a real concurrent refusal from a repair race.
        evidence = {
            "kind": "effective_network_policy_mismatch",
            "tenant": ns,
            "session_id": inp.session_id,
            "digest": have,
            "published_digest": want,
            "policies": [{"name": p.metadata.name, "uid": p.metadata.uid} for p in policies[:16]],
            "truncated": len(policies) > 16,
        }
        raise ApplicationError(
            "effective NetworkPolicy set differs from the controller's",
            evidence,
            type="Forbidden",
            non_retryable=True,
        )
    if status.get("identityMode") == "on-behalf-of" and status.get("delegationRequired", True):
        grant = d.aps.get(f"/v1/grants/{inp.grant_handle}") if inp.grant_handle else None
        if not grant or grant.get("session_id") != inp.session_id:
            raise _forbidden("delegated session without a grant")
    return _agent_boundary_limit(d, inp, status)


@activity.defn
def set_generation(g: GenIn) -> None:
    deps().aps.put(
        f"/v1/sessions/{g.inp.session_id}/generation",
        {"generation": g.generation, "tenant": g.inp.tenant, "agent": g.inp.agent},
    )


@activity.defn
def create_sandbox(g: GenIn) -> str:
    d = deps()
    ns, name = g.inp.tenant, pod_name(g.inp.session_id, g.generation)
    try:
        cm = d.core.read_namespaced_config_map(f"podtemplate-{g.inp.release}", ns)
    except ApiException as e:
        _kube_error(e, "pod template")
    pod = yaml.safe_load((cm.data or {}).get("pod.yaml", ""))
    if not isinstance(pod, dict) or pod.get("kind") != "Pod":
        raise _forbidden("pod template is not a Pod")
    meta = pod.setdefault("metadata", {})
    meta["name"], meta["namespace"] = name, ns
    labels = meta.setdefault("labels", {})
    labels[LABEL + "session"] = g.inp.session_id
    labels[LABEL + "generation"] = str(g.generation)
    for c in pod["spec"]["containers"]:
        env = [e for e in c.get("env", []) if e.get("name") != "AGENT_SESSION_ID"]
        c["env"] = env + [{"name": "AGENT_SESSION_ID", "value": g.inp.session_id}]
    try:
        created = d.core.create_namespaced_pod(ns, pod)
    except ApiException as e:
        if e.status != 409:
            _kube_error(e, "create sandbox")  # an admission refusal is final, not transient
        try:
            created = d.core.read_namespaced_pod(name, ns)
        except ApiException as e2:
            _kube_error(e2, "read existing sandbox")
    want = {
        LABEL + "session": g.inp.session_id,
        LABEL + "generation": str(g.generation),
        LABEL + "release": g.inp.release,
        LABEL + "agent": g.inp.agent,
    }
    got = created.metadata.labels or {}
    if {k: got.get(k) for k in want} != want:
        raise ApplicationError(
            "existing pod belongs to another session", type="IdentityConflict", non_retryable=True
        )
    return created.metadata.uid


@activity.defn
def wait_ready(g: GenIn) -> None:
    d = deps()
    ns, name = g.inp.tenant, pod_name(g.inp.session_id, g.generation)
    deadline = time.monotonic() + READY_DEADLINE
    while time.monotonic() < deadline:
        activity.heartbeat()
        try:
            pod = d.core.read_namespaced_pod(name, ns)
        except ApiException as e:
            if e.status == 404:
                raise ApplicationError(
                    "sandbox disappeared", type="SandboxLost", non_retryable=True
                ) from None
            _kube_error(e, "read sandbox")
        phase = pod.status.phase if pod.status else None
        if phase in ("Failed", "Succeeded"):
            raise ApplicationError(
                "sandbox exited before ready (egress guard or image failure)",
                type="SandboxLost",
                non_retryable=True,
            )
        if phase == "Running":  # Running implies every init container exited 0
            if g.restore:
                # The seed stays the restore source until this session commits a snapshot.
                _restore(d, ns, name, g.restore, workspace_only=g.restore == g.inp.seed)
            created = pod.metadata.creation_timestamp
            records.emit(
                "sandbox_created",
                session_id=g.inp.session_id,
                generation=g.generation,
                pod=name,
                node=pod.spec.node_name if pod.spec else None,
                boot_ms=int((datetime.now(UTC) - created).total_seconds() * 1000)
                if created
                else None,
            )
            return
        time.sleep(POLL)
    raise ApplicationError("sandbox not ready", type="SandboxLost")


def _protocol_error(res: ExecResult) -> str | None:
    """Only fixed step diagnostics may enter logs; arbitrary guest output is task content."""
    if res.rc != 5:
        return None
    for reason in (
        "bad length header",
        "length out of range",
        "short body",
        "invalid JSON",
        "request is not an object with a task",
        "bad boundary",
    ):
        if f"agent_runtime.step: {reason}" in res.stderr.splitlines():
            return reason
    try:
        out = BoundaryOut.parse_strict(json.loads(res.stdout.decode("utf-8")))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return "unrecognized"
    return "boundary mismatch" if out.summary == "boundary mismatch" else "unrecognized"


@activity.defn
def run_boundary(b: BoundaryIn) -> BoundaryOut:
    memory = MemoryAudit(b)
    memory.sample("start")
    try:
        return _run_boundary(b, memory)
    finally:
        memory.sample("end")


def _run_boundary(b: BoundaryIn, memory: MemoryAudit) -> BoundaryOut:
    d = deps()
    ns, name = b.inp.tenant, pod_name(b.inp.session_id, b.generation)
    payload = json.dumps(
        {"boundary": b.boundary, "task": b.inp.task}, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > MAX_INPUT:
        raise _forbidden("task too large")
    frame = f"{len(payload)}\n".encode() + payload  # the guest never waits for EOF

    def heartbeat():
        activity.heartbeat()
        memory.sample("periodic")

    try:
        res = d.run_exec(
            ns,
            name,
            STEP_CMD,
            stdin=frame,
            max_output=MAX_OUTPUT,
            deadline=EXEC_DEADLINE,
            heartbeat=heartbeat,
        )
    except ExecNotStarted:
        raise ApplicationError("exec could not start", type="SandboxLost") from None
    except ExecLost:
        raise ApplicationError(
            "exec lost before the outcome was known", type="ExecFailed"
        ) from None
    if res.truncated or res.timed_out or res.rc not in (0, 2, 3, 4) or not res.stdout.strip():
        protocol_error = _protocol_error(res)
        records.emit(
            "boundary_failed",
            session_id=b.inp.session_id,
            generation=b.generation,
            boundary=b.boundary,
            rc=res.rc,
            truncated=res.truncated,
            timed_out=res.timed_out,
            protocol_error=protocol_error,
        )
        raise ApplicationError(
            f"rc={res.rc} truncated={res.truncated} timed_out={res.timed_out}"
            f" protocol_error={protocol_error}",
            type="OutcomeUnknown",
        )
    try:
        out = BoundaryOut.parse_strict(json.loads(res.stdout.decode("utf-8")))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ApplicationError("malformed boundary output", type="OutcomeUnknown") from None
    if out.boundary != b.boundary or out.done != (res.rc == 2):
        raise ApplicationError("inconsistent boundary output", type="OutcomeUnknown")
    if res.rc == 3:
        # The summary is task content: it travels as encoded details, never in the message.
        raise ApplicationError(
            "agent reported an error", out.summary, type="AgentError", non_retryable=True
        )
    if res.rc == 4:
        raise ApplicationError(
            "guest reported budget exceeded", type="BudgetExceeded", non_retryable=True
        )
    return out


@activity.defn
def snapshot_workspace(s: SnapshotIn) -> str:
    d = deps()
    ns, name = s.inp.tenant, pod_name(s.inp.session_id, s.generation)
    details = activity.info().heartbeat_details
    expected_uid = None
    if details:
        if (
            len(details) != 1
            or not isinstance(details[0], dict)
            or set(details[0]) != {"snapshot_pod_uid"}
            or not isinstance(details[0]["snapshot_pod_uid"], str)
            or not details[0]["snapshot_pod_uid"]
        ):
            raise ApplicationError(
                "snapshot retry identity is invalid", type="SnapshotFailed", non_retryable=True
            )
        expected_uid = details[0]["snapshot_pod_uid"]
    uid = _snapshot_pod_uid(d, ns, name, expected_uid)

    def heartbeat():
        # A retried upload may never adopt a replacement Pod with the same name.
        activity.heartbeat({"snapshot_pod_uid": uid})

    heartbeat()
    digest = hashlib.sha256()
    with tempfile.SpooledTemporaryFile(max_size=8 << 20) as buf:

        def sink(chunk: bytes) -> None:
            digest.update(chunk)
            buf.write(chunk)

        try:
            res = d.run_exec(
                ns,
                name,
                SNAPSHOT_CMD,
                max_output=MAX_SNAPSHOT,
                deadline=SNAPSHOT_DEADLINE,
                on_stdout=sink,
                heartbeat=heartbeat,
            )
        except (ExecNotStarted, ExecLost) as e:
            raise ApplicationError(f"snapshot exec failed: {e}", type="SnapshotFailed") from None
        if res.truncated:
            raise ApplicationError(
                "workspace exceeds 64 MiB", type="SnapshotFailed", non_retryable=True
            )
        if res.rc != 0 or res.timed_out:
            raise ApplicationError(f"snapshot rc={res.rc}", type="SnapshotFailed")
        key = digest.hexdigest()
        buf.seek(0)
        try:
            d.store.put(key, buf)
        except OSError:
            raise ApplicationError("snapshot store write failed", type="SnapshotFailed") from None
    # The archive was spooled before store.put. Pod loss during that copy must
    # remain an uncommitted boundary, even if a candidate artifact is durable.
    _snapshot_pod_uid(d, ns, name, uid)
    return key


def _snapshot_pod_uid(d: Deps, ns: str, name: str, expected: str | None) -> str:
    try:
        pod = d.core.read_namespaced_pod(name, ns)
    except ApiException as exc:
        if exc.status != 404:
            _kube_error(exc, "snapshot pod")
        raise ApplicationError(
            "snapshot pod disappeared", type="SnapshotFailed", non_retryable=True
        ) from None
    uid = pod.metadata.uid
    if (
        not uid
        or (expected is not None and uid != expected)
        or pod.metadata.deletion_timestamp is not None
        or pod.status is None
        or pod.status.phase != "Running"
    ):
        raise ApplicationError(
            "snapshot pod identity or liveness changed", type="SnapshotFailed", non_retryable=True
        )
    return uid


def _restore(d: Deps, ns: str, name: str, key: str, *, workspace_only: bool = False) -> None:
    digest, size = hashlib.sha256(), 0
    with tempfile.SpooledTemporaryFile(max_size=8 << 20) as buf:
        try:
            src = d.store.open(key)
        except (FileNotFoundError, KeyError):
            raise ApplicationError(
                "snapshot missing from the store", type="RestoreFailed", non_retryable=True
            ) from None
        with src:
            while chunk := src.read(STDIN_CHUNK):
                size += len(chunk)
                if size > MAX_SNAPSHOT:
                    break
                digest.update(chunk)
                buf.write(chunk)
        if size > MAX_SNAPSHOT or digest.hexdigest() != key:
            raise ApplicationError(
                "snapshot does not match its key", type="RestoreFailed", non_retryable=True
            )
        buf.seek(0)
        try:
            res = d.run_exec(
                ns,
                name,
                (SEED_RESTORE_CMD if workspace_only else RESTORE_CMD) + [str(size)],
                stdin=buf,
                max_output=4096,
                deadline=SNAPSHOT_DEADLINE,
                heartbeat=activity.heartbeat,
            )
        except (ExecNotStarted, ExecLost) as e:
            raise ApplicationError(f"restore exec failed: {e}", type="RestoreFailed") from None
    if res.rc != 0:
        raise ApplicationError(f"restore rc={res.rc}", type="RestoreFailed")


def _delete_pod(d: Deps, ns: str, name: str) -> str:
    try:
        d.core.delete_namespaced_pod(name, ns, grace_period_seconds=30)
    except ApiException as e:
        if e.status != 404:
            _kube_error(e, "delete sandbox")
    deadline = time.monotonic() + DELETE_DEADLINE
    while time.monotonic() < deadline:
        activity.heartbeat()
        try:
            d.core.read_namespaced_pod(name, ns)
        except ApiException as e:
            if e.status == 404:
                return "DELETED"
            _kube_error(e, "read sandbox")
        time.sleep(POLL)
    return "NOT_CONFIRMED"


@activity.defn
def delete_sandbox(g: GenIn) -> str:
    name = pod_name(g.inp.session_id, g.generation)
    result = _delete_pod(deps(), g.inp.tenant, name)
    records.emit(
        "cleanup_result",
        session_id=g.inp.session_id,
        generation=g.generation,
        result=result,
        source="runner",
        pod=name,
    )
    return result


@activity.defn
def stop_session(inp: SessionInput) -> str:
    deps().aps.post(f"/v1/sessions/{inp.session_id}/stop", {})
    return "OK"


def _grant_delete_path(handle: str, session_id: str) -> str:
    """The grant's DELETE path, naming the session that is being cleaned up.

    APS removes the grant only when it is bound to that session and answers 403 otherwise.
    An APS older than the parameter ignores it and checks the tenant alone.
    """
    return f"/v1/grants/{quote(str(handle), safe='')}?session_id={quote(str(session_id), safe='')}"


@activity.defn
def delete_grant(inp: SessionInput) -> str:
    if not inp.grant_handle:
        return "OK"
    aps = deps().aps
    # Cleanup also runs for a refused session, and preflight reads the grant only for a
    # delegated release, so the submitted handle is not known to be this session's. A grant
    # APS shows as bound to another session is left alone and reported as a failed step.
    # One it does not show (already deleted, or stop removed its tokens first) goes to APS
    # with this session named, and APS refuses it there if it is another session's.
    grant = aps.get(f"/v1/grants/{quote(str(inp.grant_handle), safe='')}", missing_ok=True)
    if grant is not None and grant.get("session_id") != inp.session_id:
        raise _forbidden("grant is bound to another session")
    aps.delete(_grant_delete_path(inp.grant_handle, inp.session_id))  # 404 is success
    return "OK"


# --- cleanup reconciler (10.7) ------------------------------------------------------------------------


@activity.defn
def list_reconcile_inputs(tenant: str) -> ReconcileInputs:
    d = deps()
    try:
        items = d.core.list_namespaced_pod(tenant, label_selector=LABEL + "sandbox=true").items
    except ApiException as e:
        _kube_error(e, "list sandboxes")
    pods = []
    for p in items:
        labels = p.metadata.labels or {}
        try:
            generation = int(labels.get(LABEL + "generation", ""))
        except ValueError:
            continue
        if labels.get(LABEL + "session") and labels.get(LABEL + "agent"):
            since = p.metadata.deletion_timestamp
            pods.append(
                SandboxPod(
                    p.metadata.name,
                    labels[LABEL + "agent"],
                    labels[LABEL + "session"],
                    generation,
                    since.isoformat() if since else None,
                )
            )
    # A terminated workflow never runs its own cleanup. Waiting for a session to
    # become fifteen minutes old would miss T24's ten-minute convergence bound.
    # The reconciler still checks Temporal closure before stopping any session.
    listed = d.aps.get("/v1/sessions") or []
    if isinstance(listed, dict):
        listed = listed.get("sessions") or []
    sessions = [
        ApsSession(s["session_id"], s.get("agent", ""), s.get("generation"), s.get("grant_handle"))
        for s in listed
        if isinstance(s, dict) and s.get("session_id") and s.get("tenant", tenant) == tenant
    ]
    return ReconcileInputs(pods, sessions)


@activity.defn
async def closed_workflows(workflow_ids: list[str]) -> list[str]:
    """IDs whose session workflow is closed or no longer exists."""
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError, RPCStatusCode

    client_ = deps().temporal
    closed = []
    for wid in workflow_ids:
        try:
            desc = await client_.get_workflow_handle(wid).describe()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                closed.append(wid)
                continue
            raise
        if desc.status != WorkflowExecutionStatus.RUNNING:
            closed.append(wid)
    return closed


@activity.defn
def delete_orphan(o: OrphanPod) -> str:
    result = _delete_pod(deps(), o.tenant, o.pod.name)
    records.emit(
        "cleanup_result",
        session_id=o.pod.session_id,
        generation=o.pod.generation,
        result=result,
        source="reconciler",
        pod=o.pod.name,
    )
    return result


@activity.defn
def close_session(c: ClosedSession) -> str:
    d = deps()
    steps = {"stop": lambda: d.aps.post(f"/v1/sessions/{c.session.session_id}/stop", {})}
    if c.session.grant_handle:
        # APS listed this handle beside this session; naming the session keeps it that way.
        steps["grant"] = lambda: d.aps.delete(
            _grant_delete_path(c.session.grant_handle, c.session.session_id)
        )
    failures = {}
    # Neither a slow stop nor a failed stop prevents the independent grant delete.
    # Every step is idempotent, including retries of a partially completed activity.
    with ThreadPoolExecutor(max_workers=len(steps)) as executor:
        futures = {name: executor.submit(call) for name, call in steps.items()}
        for name, future in futures.items():
            result = "OK"
            try:
                future.result()
            except Exception as exc:  # every independent step must be attempted
                failures[name] = type(exc).__name__
                result = "FAILED:" + type(exc).__name__
            records.emit(
                "cleanup_result",
                session_id=c.session.session_id,
                source="reconciler",
                step=name,
                result=result,
            )
    if failures:
        raise ApplicationError(
            "reconciler session cleanup incomplete", failures, type="CleanupFailed"
        )
    return "OK"


SESSION_ACTIVITIES = [
    preflight,
    set_generation,
    create_sandbox,
    wait_ready,
    run_boundary,
    snapshot_workspace,
    delete_sandbox,
    stop_session,
    delete_grant,
]
RECONCILER_ACTIVITIES = [list_reconcile_inputs, closed_workflows, delete_orphan, close_session]
