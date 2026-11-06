"""CAS guarded promotion on disjoint governed queues; legacy routes stay untouched."""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Callable

from temporalio.api.deployment.v1 import WorkerDeploymentVersion
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice import v1 as api

from runner.types import TENANT_RE, worker_queue, worker_deployment

RPC_TIMEOUT = timedelta(seconds=10)
UNVERSIONED = "__unversioned__"
BUILD_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
IMAGE_RE = re.compile(
    r"registry\.almckay\.io/platform/session-runner(?::[A-Za-z0-9._-]+)?@sha256:[a-f0-9]{64}\Z"
)


class GuardError(ValueError):
    """A required precondition was not proved; no mutation is authorized."""


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class VersioningOperator:
    def __init__(
        self, service, inventory, *, tenant: str, now=None, record: Callable | None = None
    ):
        if not TENANT_RE.fullmatch(tenant):
            raise GuardError("invalid tenant")
        self.service = service
        self.inventory = inventory
        self.tenant = tenant
        self.namespace = f"agents-{tenant}"
        self.now = now or (lambda: datetime.now(UTC))
        self.record = record or (lambda _: None)

    def role(self, deployment: str) -> str:
        for role in ("sessions", "cleanup"):
            if deployment == worker_deployment(self.tenant, role, "governed-v1"):
                return role
        raise GuardError("deployment outside the governed tenant role allowlist")

    def queue(self, deployment: str) -> str:
        return worker_queue(self.tenant, self.role(deployment), "governed-v1")

    def fresh_pollers(self, response, pod_names, *, deployment=None, build_id=None):
        matches = []
        for poller in response.pollers:
            options = poller.deployment_options
            if deployment is None:
                if (
                    options.deployment_name
                    or options.build_id
                    or options.worker_versioning_mode not in (0, 1)
                ):
                    continue
            elif (options.deployment_name, options.build_id, options.worker_versioning_mode) != (
                deployment,
                build_id,
                2,
            ):
                continue
            identity = poller.identity.partition("@")
            age = (self.now() - poller.last_access_time.ToDatetime(tzinfo=UTC)).total_seconds()
            if not identity[0].isdigit() or identity[2] not in pod_names or not 0 <= age <= 120:
                continue
            matches.append(
                {
                    "identity": poller.identity,
                    "last_access_time": poller.last_access_time.ToJsonString(),
                }
            )
        if not matches:
            raise GuardError("missing exact fresh Workflow or Activity poller")
        return matches

    async def legacy(self, deployment):
        role = self.role(deployment)
        workers = await self.inventory.legacy(role)
        names = {p["name"] for p in workers["pods"]}
        queue = worker_queue(self.tenant, role)
        observed = []
        for kind in (1, 2):
            value = await self.call(
                "describe_task_queue",
                api.DescribeTaskQueueRequest(
                    namespace=self.namespace,
                    task_queue=TaskQueue(name=queue),
                    task_queue_type=kind,
                ),
            )
            route = value.versioning_info
            if (
                route.current_version not in ("", UNVERSIONED)
                or route.current_deployment_version.build_id
                or route.current_deployment_version.deployment_name
            ):
                raise GuardError("legacy queue no longer unversioned")
            observed.append(
                {"queue": queue, "type": kind, "pollers": self.fresh_pollers(value, names)}
            )
        return {"workers": workers, "pollers": observed}

    async def call(self, name, request):
        return await getattr(self.service, name)(request, timeout=RPC_TIMEOUT)

    async def routing(self, deployment: str):
        self.queue(deployment)
        value = await self.call(
            "describe_worker_deployment",
            api.DescribeWorkerDeploymentRequest(
                namespace=self.namespace,
                deployment_name=deployment,
            ),
        )
        info, route = value.worker_deployment_info, value.worker_deployment_info.routing_config
        if info.name != deployment or not value.conflict_token:
            raise GuardError("missing deployment identity or routing conflict token")
        version = route.current_deployment_version
        if version.build_id:
            if version.deployment_name != deployment:
                raise GuardError("current version belongs to another deployment")
            current = version.build_id
            if route.current_version not in ("", deployment + "." + current):
                raise GuardError("inconsistent current version representations")
        elif route.current_version in ("", UNVERSIONED):
            current = UNVERSIONED
        elif route.current_version.startswith(deployment + "."):
            current = route.current_version[len(deployment) + 1 :]
        else:
            raise GuardError("unrecognized current version")
        if current != UNVERSIONED and not BUILD_RE.fullmatch(current):
            raise GuardError("current routing build ID is malformed")
        if (
            route.ramping_version
            or route.ramping_deployment_version.build_id
            or route.ramping_version_percentage
        ):
            raise GuardError("unexpected ramping configuration")
        return current, bytes(value.conflict_token)

    async def prepare(self, deployment: str, build_id: str, image: str) -> dict:
        queue = self.queue(deployment)
        if not BUILD_RE.fullmatch(build_id) or not IMAGE_RE.fullmatch(image):
            raise GuardError("invalid build ID or unpinned runner image")
        current, token = await self.routing(deployment)
        workers = await self.inventory.candidate(deployment, build_id, image)
        version = await self.call(
            "describe_worker_deployment_version",
            api.DescribeWorkerDeploymentVersionRequest(
                namespace=self.namespace,
                deployment_version=WorkerDeploymentVersion(
                    deployment_name=deployment, build_id=build_id
                ),
                report_task_queue_stats=True,
            ),
        )
        info = version.worker_deployment_version_info
        if info.deployment_version != WorkerDeploymentVersion(
            deployment_name=deployment, build_id=build_id
        ):
            raise GuardError("candidate version identity mismatch")
        if info.version not in ("", deployment + "." + build_id):
            raise GuardError("candidate version string mismatch")
        queues = [(q.name, q.type) for q in info.task_queue_infos]
        if len(queues) != 2 or set(queues) != {(queue, 1), (queue, 2)}:
            raise GuardError(
                "candidate must register exactly the role Workflow and Activity queues"
            )
        pod_names = {p["name"] for p in workers["pods"]}
        if not pod_names:
            raise GuardError("candidate has no Ready Pods")
        observed = []
        for kind in (1, 2):
            response = await self.call(
                "describe_task_queue",
                api.DescribeTaskQueueRequest(
                    namespace=self.namespace,
                    task_queue=TaskQueue(name=queue),
                    task_queue_type=kind,
                ),
            )
            matches = self.fresh_pollers(
                response, pod_names, deployment=deployment, build_id=build_id
            )
            observed.append({"queue": queue, "type": kind, "pollers": matches})
        return {
            "schema": "worker-versioning-operation-v1",
            "namespace": self.namespace,
            "deployment": deployment,
            "build_id": build_id,
            "image": image,
            "current": current,
            "conflict_sha256": sha256(token),
            "workers": workers,
            "pollers": observed,
            "observed_at": self.now().isoformat(),
            "legacy": await self.legacy(deployment),
        }

    async def promote(
        self,
        deployment: str,
        build_id: str,
        image: str,
        *,
        expected_current: str,
        expected_conflict_sha256: str,
    ) -> dict:
        current, token = await self.routing(deployment)
        if current != expected_current or sha256(token) != expected_conflict_sha256:
            raise GuardError("routing changed since operator preparation")
        prepared = await self.prepare(deployment, build_id, image)
        current, token = await self.routing(deployment)
        if current != expected_current or sha256(token) != expected_conflict_sha256:
            raise GuardError("routing changed during candidate verification")
        if prepared["current"] != current or prepared["conflict_sha256"] != sha256(token):
            raise GuardError("candidate was observed under different routing")
        pending = {**prepared, "status": "mutation_pending", "requested_at": self.now().isoformat()}
        self.record(copy.deepcopy(pending))
        try:
            await self.call(
                "set_worker_deployment_current_version",
                api.SetWorkerDeploymentCurrentVersionRequest(
                    namespace=self.namespace,
                    deployment_name=deployment,
                    build_id=build_id,
                    conflict_token=token,
                    identity="agent-platform-worker-versioning",
                    ignore_missing_task_queues=False,
                    allow_no_pollers=False,
                ),
            )
            observed, _ = await self.routing(deployment)
            if observed != build_id:
                raise GuardError("promotion readback did not match target")
        except Exception as exc:
            result = {**pending, "status": "uncertain", "error_type": type(exc).__name__}
            try:
                observed, _ = await self.routing(deployment)
                result["observed_current"] = observed
            except Exception as read_error:
                result["readback_error_type"] = type(read_error).__name__
            self.record(copy.deepcopy(result))
            return result
        result = {
            **pending,
            "status": "confirmed",
            "current": observed,
            "completed_at": self.now().isoformat(),
        }
        self.record(copy.deepcopy(result))
        return result
