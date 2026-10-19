"""A standard Temporal Worker with disposable per-execution transport caches."""

import asyncio
import hashlib
import hmac
from collections import OrderedDict
from collections.abc import Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from ._runtime.activities import Activities
from ._runtime.artifacts import Tape
from ._runtime.openshell import OpenShellSession, policy, vllm_policy
from ._runtime.records import canonical_bytes
from ._runtime.temporal import CleanupWorkflow, SessionWorkflow
from .manifest import Release
from .session import SessionHandle, execution_session, load_execution


def policy_for(release):
    selected = vllm_policy() if release.agent.policy == "vllm-chat" else policy()
    if (
        hashlib.sha256(selected.SerializeToString(deterministic=True)).hexdigest()
        != release.policy_sha256
    ):
        raise ValueError("release policy digest does not match qualified policy")
    return selected


def execution_key(key, handle, release_id):
    return hmac.digest(
        key,
        b"zetra/session-capability/v1\0"
        + canonical_bytes(
            {
                "execution": handle.model_dump(),
                "release": release_id,
            }
        ),
        "sha256",
    )


@dataclass
class Context:
    start: dict
    activities: Activities
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


async def settled_provisioning(client, handle):
    """No recorded unacknowledged/retired create attempt may justify deletion proof.

    An RPC in an old process can outlive its Temporal Activity. Until an external
    reconciler fences that request, cleaning observed instances is not proof that
    a new instance cannot appear later.
    """
    history = await client.get_workflow_handle(
        handle.workflow_id, run_id=handle.run_id
    ).fetch_history()
    scheduled, completed = set(), set()
    uncertain = False
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            attrs = event.activity_task_scheduled_event_attributes
            if attrs.activity_type.name in ("provision_session", "rebind_session"):
                scheduled.add(event.event_id)
        elif event.HasField("activity_task_started_event_attributes"):
            attrs = event.activity_task_started_event_attributes
            if attrs.scheduled_event_id in scheduled and attrs.attempt > 1:
                uncertain = True
        elif event.HasField("activity_task_completed_event_attributes"):
            attrs = event.activity_task_completed_event_attributes
            if attrs.scheduled_event_id in scheduled:
                completed.add(attrs.scheduled_event_id)
    return not uncertain and scheduled == completed


class ActivityRouter:
    def __init__(
        self,
        temporal,
        *,
        task_queue,
        runtime_id,
        sandbox_client,
        artifacts,
        key,
        approved_releases: Collection[str] | None = None,
    ):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("worker key must contain at least 32 bytes")
        if not runtime_id or not task_queue:
            raise ValueError("runtime_id and task_queue are required")
        if approved_releases is not None and (
            isinstance(approved_releases, (str, bytes))
            or not isinstance(approved_releases, Collection)
            or any(not isinstance(item, str) or not item.strip() for item in approved_releases)
        ):
            raise ValueError(
                "approved_releases must be a collection of nonempty release identities"
            )
        self.approved_releases = None if approved_releases is None else frozenset(approved_releases)
        self.temporal = temporal
        self.task_queue, self.runtime_id = task_queue, runtime_id
        self.sandbox_client, self.artifacts, self.key = sandbox_client, artifacts, key
        self.cache = OrderedDict()
        self.cache_lock = asyncio.Lock()

    def current_handle(self):
        info = activity.info()
        return SessionHandle(
            namespace=info.workflow_namespace,
            workflow_id=info.workflow_id,
            run_id=info.workflow_run_id,
            runtime_id=self.runtime_id,
        )

    async def build(self, handle):
        start = await load_execution(self.temporal, handle)
        if start["task_queue"] != self.task_queue:
            raise ValueError("worker task queue mismatch")
        release = Release.model_validate(start["release"])
        remote = OpenShellSession(
            self.sandbox_client,
            self.artifacts,
            session=execution_session(handle),
            manifest=release.identity,
            key=execution_key(self.key, handle, release.identity),
            image=release.image,
            sandbox_policy=policy_for(release),
            provider="vllm" if release.agent.model == "vllm" else "scripted",
            agent_spec=release.agent.model_dump(),
        )
        return Context(
            start, Activities(remote, remote, Tape(self.artifacts, manifest=release.identity))
        )

    def trim_cache(self):
        for identity, candidate in list(self.cache.items()):
            if len(self.cache) <= 128:
                break
            if not candidate.users and not candidate.activities.lifecycle.creations:
                self.cache.pop(identity)

    @asynccontextmanager
    async def context(self, handle):
        identity = (handle.namespace, handle.workflow_id, handle.run_id, handle.runtime_id)
        try:
            # Initialization only reads history. Serialize cache misses rather than
            # retaining background initializer tasks after Activity cancellation.
            async with self.cache_lock:
                if identity not in self.cache:
                    self.cache[identity] = await self.build(handle)
                context = self.cache[identity]
                self.cache.move_to_end(identity)
                context.users += 1
                self.trim_cache()
            try:
                async with context.lock:
                    yield context
            finally:
                context.users -= 1
                self.trim_cache()
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc

    async def invoke(self, name, command):
        handle = self.current_handle()
        async with self.context(handle) as context:
            # Context construction is shared with cleanup, which must remain
            # possible after release approval is withdrawn. Recheck cached
            # contexts at every operation that can run agent code or allocate.
            if name != "close_session" and self.approved_releases is not None:
                release_id = Release.model_validate(context.start["release"]).identity
                if release_id not in self.approved_releases:
                    raise ApplicationError("release is not approved", non_retryable=True)
            if name == "provision_session":
                if command != context.start:
                    raise ApplicationError("start mismatch", non_retryable=True)
                command = {**command, "session": execution_session(handle)}
            elif name in ("rebind_session", "close_session"):
                if command["start"] != context.start:
                    raise ApplicationError("start mismatch", non_retryable=True)
                command = {
                    **command,
                    "start": {**context.start, "session": execution_session(handle)},
                }
            result = await getattr(context.activities, name)(command)
            if name == "close_session" and not await settled_provisioning(self.temporal, handle):
                return {"cleanup": "NOT_CONFIRMED", "reason": "unsettled provisioning"}
            return result

    @activity.defn
    async def provision_session(self, start: dict) -> dict:
        return await self.invoke("provision_session", start)

    @activity.defn
    async def rebind_session(self, command: dict) -> dict:
        return await self.invoke("rebind_session", command)

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

    @activity.defn
    async def reconcile_session(self, target: dict) -> dict:
        try:
            handle = SessionHandle.model_validate(target)
            if handle.runtime_id != self.runtime_id:
                raise ValueError("runtime identity mismatch")
            async with self.context(handle) as context:
                # Fetch exact terminal state immediately before lifecycle calls.
                status = (
                    await self.temporal.get_workflow_handle(
                        handle.workflow_id, run_id=handle.run_id
                    ).describe()
                ).status.name
                if status == "RUNNING":
                    raise ValueError("cannot reconcile a running Workflow")
                result = await context.activities.close_session(
                    {"start": {**context.start, "session": execution_session(handle)}}
                )
                if not await settled_provisioning(self.temporal, handle):
                    return {"cleanup": "NOT_CONFIRMED", "reason": "unsettled provisioning"}
                return result
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc

    def registered(self):
        return [
            self.provision_session,
            self.rebind_session,
            self.advance_runner,
            self.execute_model,
            self.execute_tool,
            self.close_session,
            self.reconcile_session,
        ]


def create_worker(
    temporal,
    *,
    task_queue,
    runtime_id,
    sandbox_client,
    artifacts,
    key,
    approved_releases: Collection[str] | None = None,
    **worker_options,
):
    """Return an ordinary Worker; the caller owns connections, secrets and storage.

    All workers polling this queue must share runtime identity, gateway, artifact
    backing and derivation key. Give different runtime configurations different
    queues. Neither construction nor shutdown closes caller-supplied resources.

    approved_releases snapshots caller-approved Release.identity values. An empty
    collection denies execution; None trusts authorized Temporal namespace
    submitters. Revocation prevents new execution but never blocks cleanup.
    Release approval does not establish runtime compatibility: callers must keep
    the worker, bridge and release compatible across upgrades.
    """
    router = ActivityRouter(
        temporal,
        task_queue=task_queue,
        runtime_id=runtime_id,
        sandbox_client=sandbox_client,
        artifacts=artifacts,
        key=key,
        approved_releases=approved_releases,
    )
    return Worker(
        temporal,
        task_queue=task_queue,
        workflows=[SessionWorkflow, CleanupWorkflow],
        activities=router.registered(),
        **worker_options,
    )
