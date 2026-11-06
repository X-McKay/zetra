"""Exact Deployment -> ReplicaSet -> Ready Pod ownership for version operations."""

from __future__ import annotations

import asyncio

from runner.types import worker_deployment
from runner.worker_versioning import GuardError

LABEL = "agentplatform.io/"


class KubernetesInventory:
    def __init__(self, apps, core, *, tenant, namespace="agent-platform"):
        worker_deployment(tenant, "sessions")
        if namespace != "agent-platform":
            raise GuardError("worker namespace is fixed")
        self.apps, self.core = apps, core
        self.tenant, self.namespace = tenant, namespace

    async def candidate(self, deployment, build_id, image):
        roles = [
            r
            for r in ("sessions", "cleanup")
            if worker_deployment(self.tenant, r, "governed-v1") == deployment
        ]
        if len(roles) != 1:
            raise GuardError("candidate outside governed role allowlist")
        return await asyncio.to_thread(self._inspect, roles[0], "governed-v1", build_id, image)

    async def legacy(self, role):
        return await asyncio.to_thread(self._inspect, role, "legacy", None, None)

    @staticmethod
    def _owner(metadata, kind, name, uid):
        owners = [o for o in metadata.owner_references or [] if o.controller is True]
        if len(owners) != 1 or (owners[0].kind, owners[0].name, owners[0].uid) != (kind, name, uid):
            raise GuardError("worker owner chain differs")

    def _inspect(self, role, pool, build_id, image):
        temporal_name = worker_deployment(self.tenant, role, pool)
        name = temporal_name if pool == "legacy" else temporal_name + "-" + build_id
        app = "session-runner" if role == "sessions" else "cleanup-reconciler"
        if pool != "legacy":
            app += "-governed"
        labels = {"app.kubernetes.io/name": app, LABEL + "tenant": self.tenant}
        if pool != "legacy":
            labels.update(
                {
                    LABEL + "worker-role": role,
                    LABEL + "worker-pool": pool,
                    LABEL + "build-id": build_id,
                }
            )
        deployment = self.apps.read_namespaced_deployment(name, self.namespace, _request_timeout=10)
        md, spec, status = deployment.metadata, deployment.spec, deployment.status
        if (
            md.name != name
            or not md.uid
            or md.deletion_timestamp
            or status.observed_generation != md.generation
        ):
            raise GuardError("worker Deployment identity or generation differs")
        if spec.selector.match_expressions or spec.selector.match_labels != labels:
            raise GuardError("worker Deployment selector is not exact and disjoint")
        if (
            not spec.replicas
            or status.available_replicas != spec.replicas
            or status.updated_replicas != spec.replicas
            or status.replicas != spec.replicas
        ):
            raise GuardError("worker Deployment rollout is not fully available")
        pods = self.core.list_namespaced_pod(
            self.namespace,
            label_selector=",".join(f"{k}={v}" for k, v in sorted(labels.items())),
            _request_timeout=10,
        ).items
        if len(pods) != spec.replicas:
            raise GuardError("worker Pod count differs from available Deployment")
        receipts = []
        expected_env = {
            "WORKER_POOL": pool,
            "WORKER_BUILD_ID": build_id,
            "WORKER_DEPLOYMENT": temporal_name,
        }
        for pod in pods:
            pm = pod.metadata
            if (
                pm.deletion_timestamp
                or not pm.uid
                or any((pm.labels or {}).get(k) != v for k, v in labels.items())
            ):
                raise GuardError("worker Pod identity or labels differ")
            owners = [o for o in pm.owner_references or [] if o.controller is True]
            if len(owners) != 1 or owners[0].kind != "ReplicaSet":
                raise GuardError("worker Pod lacks a unique ReplicaSet owner")
            owner = owners[0]
            rs = self.apps.read_namespaced_replica_set(
                owner.name, self.namespace, _request_timeout=10
            )
            if rs.metadata.uid != owner.uid or rs.metadata.deletion_timestamp:
                raise GuardError("worker ReplicaSet was replaced or deleting")
            self._owner(rs.metadata, "Deployment", name, md.uid)
            if not any(
                c.type == "Ready" and c.status == "True" for c in pod.status.conditions or []
            ):
                raise GuardError("worker Pod is not Ready")
            if len(pod.spec.containers) != 1 or len(pod.status.container_statuses or []) != 1:
                raise GuardError("worker requires one exact runner container")
            container, state = pod.spec.containers[0], pod.status.container_statuses[0]
            if (
                state.name != container.name
                or not state.ready
                or not state.state.running
                or state.restart_count != 0
            ):
                raise GuardError("worker container is not stable and running")
            env = {e.name: e.value for e in container.env or []}
            if pool == "legacy":
                if (
                    env.get("WORKER_POOL", "legacy") != "legacy"
                    or env.get("WORKER_BUILD_ID")
                    or env.get("WORKER_DEPLOYMENT")
                ):
                    raise GuardError("legacy worker unexpectedly versioned")
            elif any(env.get(k) != v for k, v in expected_env.items()):
                raise GuardError("candidate versioning environment differs")
            actual = container.image
            expected = image or actual
            if (
                "@sha256:" not in expected
                or actual != expected
                or state.image_id.split("@sha256:")[-1] != expected.split("@sha256:")[-1]
            ):
                raise GuardError("worker installed image differs from the pinned target")
            receipts.append(
                {
                    "name": pm.name,
                    "uid": pm.uid,
                    "replicaset_uid": rs.metadata.uid,
                    "image": actual,
                    "image_id": state.image_id,
                }
            )
        return {
            "deployment": name,
            "deployment_uid": md.uid,
            "resource_version": md.resource_version,
            "build_id": build_id,
            "pool": pool,
            "pods": receipts,
        }
