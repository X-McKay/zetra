"""External session owner. Agent imports and tool bodies never run here."""

import hashlib
import json
import os
import re
import secrets
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .manifest import Release


def session_directory(state, session):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", session):
        raise ValueError("session ID must contain 1..80 letters, digits, underscore or hyphen")
    root = Path(state).resolve() / session
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("session directory cannot be a symlink")
    root.chmod(0o700)
    return root


@contextmanager
def session_lock(root):
    import fcntl

    with (root / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("session already has a local worker") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def policy_for(release):
    import hashlib

    from ._runtime.openshell import policy, vllm_policy

    selected = vllm_policy() if release.agent.policy == "vllm-chat" else policy()
    actual = hashlib.sha256(selected.SerializeToString(deterministic=True)).hexdigest()
    if actual != release.policy_sha256:
        raise ValueError("release policy digest does not match qualified policy")
    return selected


def gateway_identity():
    tls = Path(os.environ["OPENSHELL_TLS_DIR"])
    return {
        "address": os.environ["OPENSHELL_ADDRESS"],
        "workspace": "default",
        "ca_sha256": hashlib.sha256((tls / "ca.crt").read_bytes()).hexdigest(),
        "client_sha256": hashlib.sha256((tls / "tls.crt").read_bytes()).hexdigest(),
    }


def open_gateway():
    from openshell import SandboxClient, TlsConfig

    tls = Path(os.environ["OPENSHELL_TLS_DIR"])
    return SandboxClient(
        os.environ["OPENSHELL_ADDRESS"],
        tls=TlsConfig(ca_path=tls / "ca.crt", cert_path=tls / "tls.crt", key_path=tls / "tls.key"),
    )


class SessionClient:
    """One local external worker per session; persistent state is required to resume.

    This is a single-host SDK runner, not a distributed lifecycle lease service.
    """

    def __init__(self, *, state: Path, address: str, namespace="default"):
        self.state, self.address, self.namespace = Path(state), address, namespace

    async def connect(self):
        # Remote production connections require a separately qualified TLS/identity configuration.
        from urllib.parse import urlsplit

        from temporalio.client import Client

        if urlsplit("//" + self.address).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("this qualified runner requires a loopback Temporal endpoint")
        return await Client.connect(self.address, namespace=self.namespace)

    async def run(self, release: Release, *, session: str, message: str, workspace=None):
        from temporalio.client import WorkflowFailureError
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError
        from temporalio.worker import Worker

        from ._runtime.activities import Activities
        from ._runtime.artifacts import FileStore, Tape
        from ._runtime.openshell import OpenShellSession
        from ._runtime.temporal import SessionWorkflow
        from ._runtime.workspace import Workspace

        release = Release.model_validate(release.model_dump())
        release.agent.validate_policy()
        selected_policy = policy_for(release)
        if not isinstance(message, str) or not 0 < len(message.encode()) <= 32_000:
            raise ValueError("message must contain 1..32000 UTF-8 bytes")
        gateway_binding = gateway_identity()
        root = session_directory(self.state, session)
        with session_lock(root):
            record_path = root / "session.json"
            store = FileStore(root / "objects")
            tape = Tape(store, manifest=release.identity)
            if record_path.is_symlink():
                raise ValueError("session record cannot be a symlink")
            if record_path.exists():
                record = json.loads(record_path.read_text())
                if (
                    record.get("backend") != [self.address, self.namespace]
                    or record.get("gateway") != gateway_binding
                    or record["release"] != release.model_dump()
                    or record["start"]["message"] != message
                ):
                    raise ValueError(
                        "session is pinned to another backend, gateway, release or input"
                    )
                if workspace is not None:
                    raise ValueError("workspace cannot be resupplied when resuming")
            else:
                base = Path(workspace) if workspace is not None else root / "initial"
                if workspace is None:
                    base.mkdir(exist_ok=True)
                position = tape.empty(Workspace(base, store).capture())
                record = {
                    "backend": [self.address, self.namespace],
                    "gateway": gateway_binding,
                    "release": release.model_dump(),
                    "key": secrets.token_hex(32),
                    "start": {
                        "session": session,
                        "message": message,
                        "position": position.model_dump(),
                        "release_id": release.identity,
                    },
                }
                record["start"]["owner_key_id"] = hashlib.sha256(
                    bytes.fromhex(record["key"])
                ).hexdigest()
                fd = os.open(record_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as handle:
                    json.dump(record, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
            client = await self.connect()
            gateway = open_gateway()
            remote = OpenShellSession(
                gateway,
                store,
                session=session,
                manifest=release.identity,
                key=bytes.fromhex(record["key"]),
                image=release.image,
                sandbox_policy=selected_policy,
                provider="vllm" if release.agent.model == "vllm" else "scripted",
                agent_spec=release.agent.model_dump(),
            )
            activities = Activities(remote, remote, tape)
            try:
                try:
                    handle = await client.start_workflow(
                        SessionWorkflow.run,
                        record["start"],
                        id=session,
                        task_queue="zetra-" + session,
                        execution_timeout=timedelta(minutes=30),
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    handle = client.get_workflow_handle(session)
                    history = await handle.fetch_history()
                    payloads = history.events[
                        0
                    ].workflow_execution_started_event_attributes.input.payloads
                    original = (await client.data_converter.decode(payloads, [dict]))[0]
                    verify_start(original, record["start"])
                async with Worker(
                    client,
                    task_queue="zetra-" + session,
                    workflows=[SessionWorkflow],
                    activities=activities.registered(),
                ):
                    try:
                        result = normalized_result(await handle.result())
                    except WorkflowFailureError:
                        result = await terminal_result(handle, remote, client)
                    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
                    return result
            finally:
                gateway.close()

    async def status(self, session):
        from temporalio.client import WorkflowQueryFailedError

        root = session_directory(self.state, session)
        client = await self.connect()
        handle = client.get_workflow_handle(session)
        description = await handle.describe()
        result = {"session": session, "workflow_status": description.status.name}
        result["progress"] = None
        if description.status.name == "RUNNING":
            from temporalio.service import RPCError

            try:
                result["progress"] = await handle.query(
                    "progress", rpc_timeout=timedelta(seconds=3)
                )
            except (WorkflowQueryFailedError, RPCError):
                result["progress_available"] = False
        if description.status.name == "COMPLETED":
            result["result"] = normalized_result(await handle.result())
        elif description.status.name != "RUNNING":
            saved = root / "result.json"
            record = root / "session.json"
            if (
                saved.exists()
                and record.exists()
                and json.loads(record.read_text()).get("backend") == [self.address, self.namespace]
            ):
                result["result"] = json.loads(saved.read_text())
            else:
                result["result"] = {
                    "status": description.status.name,
                    "cleanup": "NOT_CONFIRMED",
                    "effect_status": "OUTCOME_UNKNOWN",
                }
        return result

    async def read_files(self, session):
        """Read hash-validated final workspace bytes after confirmed successful cleanup."""
        from ._runtime.artifacts import FileStore, Ref
        from ._runtime.workspace import validate_snapshot

        root = session_directory(self.state, session)
        record = json.loads((root / "session.json").read_text())
        if record.get("backend") != [self.address, self.namespace]:
            raise ValueError("session backend mismatch")
        result = (await self.status(session)).get("result", {})
        if result.get("status") != "SUCCEEDED" or result.get("cleanup") != "DELETED":
            raise ValueError("final files require successful execution and confirmed deletion")
        _, files = validate_snapshot(
            FileStore(root / "objects").get(Ref.model_validate(result["position"]["workspace"]))
        )
        return {path.as_posix(): data for path, data in files}

    async def cancel(self, session):
        session_directory(self.state, session)
        client = await self.connect()
        await client.get_workflow_handle(session).cancel()
        return {"session": session, "status": "CANCELLATION_REQUESTED", "cleanup": "NOT_CONFIRMED"}


def normalized_result(result):
    if result.get("status") == "SUCCEEDED" and result.get("cleanup") != "DELETED":
        return {**result, "status": "REMEDIATION_REQUIRED", "execution_status": "SUCCEEDED"}
    return result


def verify_start(original, expected):
    if original != expected:
        raise ValueError("existing Workflow belongs to another session owner or release")


async def terminal_result(handle, remote, client):
    """Reconcile only an observed closed Workflow; never infer effect success."""
    status = (await handle.describe()).status.name
    if status == "RUNNING":
        raise RuntimeError("cannot reconcile a running Workflow")
    result = {"status": status, "cleanup": "NOT_CONFIRMED", "effect_status": "OUTCOME_UNKNOWN"}
    try:
        result.update(await remote.reconcile_closed(client))
    except Exception as exc:  # noqa: BLE001 - preserve an explicit failure report across provider errors
        result["cleanup_error_type"] = type(exc).__name__
    if result["cleanup"] != "DELETED":
        result.update(status="REMEDIATION_REQUIRED", execution_status=status)
    return result
